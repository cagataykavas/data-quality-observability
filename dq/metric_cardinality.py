"""Bounded admission gate for Prometheus-style metric series snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "metric-cardinality-snapshot/v1"
METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*\Z")
LABEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
VALID_KINDS = frozenset({"counter", "gauge", "histogram", "summary", "info"})


class ArtifactError(ValueError):
    """The snapshot or policy is malformed or exceeds a hard budget."""


@dataclass(frozen=True)
class MetricPolicy:
    name: str
    kind: str
    allowed_labels: tuple[str, ...]
    required_labels: tuple[str, ...] = ()
    max_series: int = 1_000
    value_limits: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        _metric_name(self.name)
        if self.kind not in VALID_KINDS:
            raise ArtifactError("invalid policy metric kind")
        allowed = tuple(_label_name(value) for value in self.allowed_labels)
        required = tuple(_label_name(value) for value in self.required_labels)
        if len(set(allowed)) != len(allowed) or len(set(required)) != len(required):
            raise ArtifactError("metric policy label names must be unique")
        if not set(required).issubset(allowed):
            raise ArtifactError("required labels must also be allowed")
        if (
            not isinstance(self.max_series, int)
            or isinstance(self.max_series, bool)
            or self.max_series <= 0
        ):
            raise ArtifactError("max_series must be a positive integer")
        seen_limits: set[str] = set()
        for label, limit in self.value_limits:
            label = _label_name(label)
            if label not in allowed or label in seen_limits:
                raise ArtifactError("value limit labels must be unique and allowed")
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ArtifactError("label value limits must be positive integers")
            seen_limits.add(label)
        object.__setattr__(self, "allowed_labels", tuple(sorted(allowed)))
        object.__setattr__(self, "required_labels", tuple(sorted(required)))
        object.__setattr__(self, "value_limits", tuple(sorted(self.value_limits)))


@dataclass(frozen=True)
class CardinalityPolicy:
    metrics: tuple[MetricPolicy, ...]
    max_total_series: int = 20_000
    max_labels_per_series: int = 16
    max_label_value_bytes: int = 256
    max_snapshot_age_seconds: int = 300
    max_future_skew_seconds: int = 30
    max_reported_findings: int = 100
    forbidden_labels: tuple[str, ...] = (
        "email",
        "request_id",
        "session_id",
        "trace_id",
        "user_id",
    )

    def __post_init__(self) -> None:
        if not self.metrics:
            raise ArtifactError("policy requires at least one metric")
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ArtifactError("metric policies must be unique")
        budgets = (
            self.max_total_series,
            self.max_labels_per_series,
            self.max_label_value_bytes,
            self.max_snapshot_age_seconds,
            self.max_future_skew_seconds,
            self.max_reported_findings,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in budgets
        ):
            raise ArtifactError("cardinality policy budgets must be positive integers")
        forbidden = tuple(_label_name(value) for value in self.forbidden_labels)
        if len(forbidden) != len(set(forbidden)):
            raise ArtifactError("forbidden labels must be unique")


@dataclass(frozen=True)
class Finding:
    code: str
    metric: str
    label: str | None
    observed: int
    limit: int


@dataclass(frozen=True)
class MetricSummary:
    metric: str
    series_count: int
    label_cardinalities: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CardinalityReport:
    schema_version: str
    accepted: bool
    reason_codes: tuple[str, ...]
    total_series: int
    metric_count: int
    finding_count: int
    findings: tuple[Finding, ...]
    metrics: tuple[MetricSummary, ...]
    policy_digest: str
    evidence_digest: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Series:
    metric: str
    kind: str
    labels: tuple[tuple[str, str], ...]


def _metric_name(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 200 or not METRIC_NAME.fullmatch(value):
        raise ArtifactError("invalid metric name")
    return value


def _label_name(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 100 or not LABEL_NAME.fullmatch(value):
        raise ArtifactError("invalid label name")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ArtifactError(f"{field} must be a non-empty bounded string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ArtifactError(f"{field} contains control characters")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ArtifactError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ArtifactError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArtifactError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _series_from_mapping(raw: Any, policy: CardinalityPolicy) -> _Series:
    if not isinstance(raw, Mapping) or set(raw) != {"metric", "kind", "labels"}:
        raise ArtifactError("series fields do not match the schema")
    metric = _metric_name(raw["metric"])
    kind = raw["kind"]
    if kind not in VALID_KINDS:
        raise ArtifactError("invalid metric kind")
    labels = raw["labels"]
    if not isinstance(labels, Mapping):
        raise ArtifactError("series labels must be an object")
    if len(labels) > policy.max_labels_per_series:
        raise ArtifactError("labels-per-series budget exceeded")
    normalized: list[tuple[str, str]] = []
    for raw_name, raw_value in labels.items():
        name = _label_name(raw_name)
        if not isinstance(raw_value, str):
            raise ArtifactError("label values must be strings")
        if len(raw_value.encode("utf-8")) > policy.max_label_value_bytes:
            raise ArtifactError("label value byte budget exceeded")
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_value):
            raise ArtifactError("label value contains control characters")
        normalized.append((name, raw_value))
    return _Series(metric=metric, kind=str(kind), labels=tuple(sorted(normalized)))


def _policy_payload(policy: CardinalityPolicy) -> dict[str, Any]:
    return asdict(policy)


def audit_metric_cardinality(
    artifact: Mapping[str, Any],
    policy: CardinalityPolicy,
    *,
    now: datetime | None = None,
) -> CardinalityReport:
    """Audit one bounded active-series snapshot without exposing label values."""
    expected = {"schema_version", "generated_at", "collector_id", "resource_revision", "series"}
    if not isinstance(artifact, Mapping) or set(artifact) != expected:
        raise ArtifactError("artifact fields do not match the schema")
    if artifact["schema_version"] != SCHEMA_VERSION:
        raise ArtifactError("unsupported schema_version")
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ArtifactError("now must include a timezone")
    generated_at = _timestamp(artifact["generated_at"], "generated_at")
    age = (clock.astimezone(UTC) - generated_at).total_seconds()
    if age < -policy.max_future_skew_seconds:
        raise ArtifactError("snapshot is future-dated")
    if age > policy.max_snapshot_age_seconds:
        raise ArtifactError("snapshot is stale")
    collector_id = _identifier(artifact["collector_id"], "collector_id")
    resource_revision = _identifier(artifact["resource_revision"], "resource_revision")
    raw_series = artifact["series"]
    if not isinstance(raw_series, list) or not raw_series:
        raise ArtifactError("series must be a non-empty array")
    if len(raw_series) > policy.max_total_series:
        raise ArtifactError("total series hard budget exceeded")

    series = [_series_from_mapping(raw, policy) for raw in raw_series]
    identities = [(item.metric, item.kind, item.labels) for item in series]
    if len(identities) != len(set(identities)):
        raise ArtifactError("duplicate metric series")

    policies = {item.name: item for item in policy.metrics}
    per_metric: dict[str, list[_Series]] = {}
    for item in series:
        per_metric.setdefault(item.metric, []).append(item)

    findings: list[Finding] = []
    summaries: list[MetricSummary] = []
    forbidden = set(policy.forbidden_labels)
    for metric in sorted(per_metric):
        members = per_metric[metric]
        metric_policy = policies.get(metric)
        label_values: dict[str, set[str]] = {}
        for member in members:
            for label, value in member.labels:
                label_values.setdefault(label, set()).add(value)

        if metric_policy is None:
            findings.append(Finding("UNDECLARED_METRIC", metric, None, len(members), 0))
        else:
            allowed = set(metric_policy.allowed_labels)
            required = set(metric_policy.required_labels)
            value_limits = dict(metric_policy.value_limits)
            observed_kinds = {member.kind for member in members}
            if observed_kinds != {metric_policy.kind}:
                findings.append(
                    Finding(
                        "METRIC_KIND_MISMATCH",
                        metric,
                        None,
                        len(observed_kinds - {metric_policy.kind}),
                        0,
                    )
                )
            if len(members) > metric_policy.max_series:
                findings.append(
                    Finding(
                        "METRIC_SERIES_LIMIT", metric, None, len(members), metric_policy.max_series
                    )
                )
            for member in members:
                present = {label for label, _ in member.labels}
                for label in sorted(present - allowed):
                    findings.append(Finding("UNEXPECTED_LABEL", metric, label, 1, 0))
                for label in sorted(required - present):
                    findings.append(Finding("MISSING_REQUIRED_LABEL", metric, label, 0, 1))
            for label, limit in sorted(value_limits.items()):
                observed = len(label_values.get(label, set()))
                if observed > limit:
                    findings.append(Finding("LABEL_VALUE_LIMIT", metric, label, observed, limit))

        for label in sorted(forbidden.intersection(label_values)):
            findings.append(
                Finding(
                    "FORBIDDEN_HIGH_CARDINALITY_LABEL", metric, label, len(label_values[label]), 0
                )
            )
        summaries.append(
            MetricSummary(
                metric=metric,
                series_count=len(members),
                label_cardinalities=tuple(
                    (label, len(values)) for label, values in sorted(label_values.items())
                ),
            )
        )

    unique_findings = sorted(
        set(findings),
        key=lambda item: (item.code, item.metric, item.label or "", item.observed, item.limit),
    )
    reason_codes = tuple(sorted({finding.code for finding in unique_findings}))
    policy_digest = _digest(_policy_payload(policy))
    evidence_digest = _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "collector_digest": _digest({"collector_id": collector_id}),
            "resource_revision": resource_revision,
            "policy_digest": policy_digest,
            "series": [
                {"metric": item.metric, "kind": item.kind, "labels": list(item.labels)}
                for item in sorted(
                    series, key=lambda value: (value.metric, value.kind, value.labels)
                )
            ],
        }
    )
    return CardinalityReport(
        schema_version=SCHEMA_VERSION,
        accepted=not unique_findings,
        reason_codes=reason_codes,
        total_series=len(series),
        metric_count=len(per_metric),
        finding_count=len(unique_findings),
        findings=tuple(unique_findings[: policy.max_reported_findings]),
        metrics=tuple(summaries),
        policy_digest=policy_digest,
        evidence_digest=evidence_digest,
    )


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("duplicate JSON key")
        result[key] = value
    return result


def load_json(path: Path, *, max_bytes: int = 10_000_000) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ArtifactError("JSON byte budget exceeded")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("unable to parse JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactError("JSON root must be an object")
    return value


def policy_from_mapping(raw: Mapping[str, Any]) -> CardinalityPolicy:
    expected = {
        "metrics",
        "max_total_series",
        "max_labels_per_series",
        "max_label_value_bytes",
        "max_snapshot_age_seconds",
        "max_future_skew_seconds",
        "max_reported_findings",
        "forbidden_labels",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected or not isinstance(raw["metrics"], list):
        raise ArtifactError("policy fields do not match the schema")
    metrics = []
    for item in raw["metrics"]:
        required = {
            "name",
            "kind",
            "allowed_labels",
            "required_labels",
            "max_series",
            "value_limits",
        }
        if not isinstance(item, Mapping) or set(item) != required:
            raise ArtifactError("metric policy fields do not match the schema")
        if not isinstance(item["value_limits"], Mapping):
            raise ArtifactError("value_limits must be an object")
        metrics.append(
            MetricPolicy(
                name=item["name"],
                kind=item["kind"],
                allowed_labels=tuple(item["allowed_labels"]),
                required_labels=tuple(item["required_labels"]),
                max_series=item["max_series"],
                value_limits=tuple(sorted(item["value_limits"].items())),
            )
        )
    return CardinalityPolicy(
        metrics=tuple(metrics),
        max_total_series=raw["max_total_series"],
        max_labels_per_series=raw["max_labels_per_series"],
        max_label_value_bytes=raw["max_label_value_bytes"],
        max_snapshot_age_seconds=raw["max_snapshot_age_seconds"],
        max_future_skew_seconds=raw["max_future_skew_seconds"],
        max_reported_findings=raw["max_reported_findings"],
        forbidden_labels=tuple(raw["forbidden_labels"]),
    )


def _write_atomic(path: Path, report: CardinalityReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit metric label cardinality")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit_metric_cardinality(
            load_json(args.snapshot), policy_from_mapping(load_json(args.policy))
        )
        _write_atomic(args.output, report)
    except (ArtifactError, OSError) as exc:
        parser.exit(3, f"invalid evidence: {exc}\n")
    return 0 if report.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
