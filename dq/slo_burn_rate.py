from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_BUCKETS = 10_000


class QualitySLOError(ValueError):
    """Raised when quality-SLO evidence is malformed or internally inconsistent."""


@dataclass(frozen=True)
class BurnProfile:
    name: str
    short_buckets: int
    long_buckets: int
    burn_threshold: float
    severity: str

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 64:
            raise ValueError("profile name must contain at most 64 characters")
        if not 1 <= self.short_buckets <= self.long_buckets:
            raise ValueError("profile windows must satisfy 1 <= short <= long")
        if self.burn_threshold <= 0.0 or not math.isfinite(self.burn_threshold):
            raise ValueError("profile burn_threshold must be finite and positive")
        if self.severity not in {"page", "ticket"}:
            raise ValueError("profile severity must be page or ticket")


DEFAULT_PROFILES = (
    BurnProfile("fast", short_buckets=1, long_buckets=12, burn_threshold=14.4, severity="page"),
    BurnProfile("slow", short_buckets=6, long_buckets=72, burn_threshold=6.0, severity="ticket"),
)


@dataclass(frozen=True)
class QualitySLOPolicy:
    objective: float = 0.995
    min_long_window_evaluations: int = 100
    freshness_periods: int = 1
    profiles: tuple[BurnProfile, ...] = DEFAULT_PROFILES

    def __post_init__(self) -> None:
        if not 0.0 < self.objective < 1.0:
            raise ValueError("objective must be strictly between 0 and 1")
        if self.min_long_window_evaluations < 1:
            raise ValueError("min_long_window_evaluations must be positive")
        if self.freshness_periods < 0:
            raise ValueError("freshness_periods must be non-negative")
        if not self.profiles or len(self.profiles) > 8:
            raise ValueError("between 1 and 8 burn profiles are required")
        names = [profile.name for profile in self.profiles]
        if len(set(names)) != len(names):
            raise ValueError("burn profile names must be unique")


@dataclass(frozen=True, order=True)
class SLOFinding:
    code: str
    profile: str | None
    detail: str


@dataclass(frozen=True)
class _Bucket:
    end: datetime
    eligible_evaluations: int
    failed_evaluations: int


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualitySLOError(f"{context} must be an object")
    return value


def _sequence(value: Any, context: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise QualitySLOError(f"{context} must be an array")
    return value


def _utc_timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QualitySLOError(f"{context} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QualitySLOError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualitySLOError(f"{context} must include a timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise QualitySLOError(f"{context} must use UTC")
    return parsed.astimezone(UTC)


def _bounded_int(value: Any, context: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise QualitySLOError(f"{context} must be an integer between {minimum} and {maximum}")
    return value


def _buckets(evidence: Mapping[str, Any], period_seconds: int) -> tuple[_Bucket, ...]:
    raw_buckets = _sequence(evidence.get("buckets"), "buckets")
    if not raw_buckets or len(raw_buckets) > MAX_BUCKETS:
        raise QualitySLOError(f"buckets must contain between 1 and {MAX_BUCKETS} entries")
    parsed: list[_Bucket] = []
    for index, raw_bucket in enumerate(raw_buckets):
        bucket = _mapping(raw_bucket, f"buckets[{index}]")
        eligible = _bounded_int(
            bucket.get("eligible_evaluations"),
            f"buckets[{index}].eligible_evaluations",
            minimum=0,
            maximum=1_000_000_000,
        )
        failed = _bounded_int(
            bucket.get("failed_evaluations"),
            f"buckets[{index}].failed_evaluations",
            minimum=0,
            maximum=eligible,
        )
        parsed.append(
            _Bucket(
                end=_utc_timestamp(bucket.get("end"), f"buckets[{index}].end"),
                eligible_evaluations=eligible,
                failed_evaluations=failed,
            )
        )
    for previous, current in pairwise(parsed):
        gap = (current.end - previous.end).total_seconds()
        if gap != period_seconds:
            raise QualitySLOError(
                f"bucket endings must be strictly ordered and exactly {period_seconds} seconds apart"
            )
    return tuple(parsed)


def _window_metrics(
    buckets: tuple[_Bucket, ...],
    bucket_count: int,
    error_budget: float,
) -> dict[str, Any]:
    selected = buckets[-bucket_count:]
    eligible = sum(bucket.eligible_evaluations for bucket in selected)
    failed = sum(bucket.failed_evaluations for bucket in selected)
    error_rate = failed / eligible if eligible else 0.0
    return {
        "bucket_count": len(selected),
        "eligible_evaluations": eligible,
        "failed_evaluations": failed,
        "error_rate": error_rate,
        "burn_rate": error_rate / error_budget,
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def audit_quality_slo(
    evidence: Mapping[str, Any],
    policy: QualitySLOPolicy | None = None,
) -> dict[str, Any]:
    """Evaluate multi-window quality error-budget burn from fixed-period counts."""
    policy = policy or QualitySLOPolicy()
    if evidence.get("schema_version") != 1:
        raise QualitySLOError("schema_version must equal 1")
    dataset = evidence.get("dataset")
    if (
        not isinstance(dataset, str)
        or not dataset
        or dataset != dataset.strip()
        or len(dataset) > 128
    ):
        raise QualitySLOError("dataset must be a non-empty string of at most 128 characters")
    period_seconds = _bounded_int(
        evidence.get("period_seconds"), "period_seconds", minimum=60, maximum=86_400
    )
    as_of = _utc_timestamp(evidence.get("as_of"), "as_of")
    buckets = _buckets(evidence, period_seconds)
    if buckets[-1].end > as_of:
        raise QualitySLOError("the newest bucket must not end after as_of")

    findings: list[SLOFinding] = []
    evidence_age = (as_of - buckets[-1].end).total_seconds()
    max_age = period_seconds * policy.freshness_periods
    if evidence_age > max_age:
        findings.append(
            SLOFinding(
                "stale_evidence",
                None,
                f"age_seconds={int(evidence_age)}; maximum={max_age}",
            )
        )

    error_budget = 1.0 - policy.objective
    profile_rows: list[dict[str, Any]] = []
    for profile in policy.profiles:
        if len(buckets) < profile.long_buckets:
            findings.append(
                SLOFinding(
                    "insufficient_buckets",
                    profile.name,
                    f"found={len(buckets)}; required={profile.long_buckets}",
                )
            )
        short = _window_metrics(buckets, profile.short_buckets, error_budget)
        long = _window_metrics(buckets, profile.long_buckets, error_budget)
        if long["eligible_evaluations"] < policy.min_long_window_evaluations:
            findings.append(
                SLOFinding(
                    "insufficient_evaluations",
                    profile.name,
                    (
                        f"found={long['eligible_evaluations']}; "
                        f"minimum={policy.min_long_window_evaluations}"
                    ),
                )
            )
        short_breached = short["burn_rate"] > profile.burn_threshold
        long_breached = long["burn_rate"] > profile.burn_threshold
        triggered = short_breached and long_breached
        if triggered:
            findings.append(
                SLOFinding(
                    "multi_window_burn_exceeded",
                    profile.name,
                    (
                        f"short={short['burn_rate']:.6f}; long={long['burn_rate']:.6f}; "
                        f"threshold={profile.burn_threshold:.6f}"
                    ),
                )
            )
        profile_rows.append(
            {
                **asdict(profile),
                "short": short,
                "long": long,
                "short_breached": short_breached,
                "long_breached": long_breached,
                "triggered": triggered,
            }
        )

    findings.sort()
    normalized_evidence = {
        "schema_version": 1,
        "dataset": dataset,
        "period_seconds": period_seconds,
        "as_of": as_of.isoformat(),
        "buckets": [
            {
                "end": bucket.end.isoformat(),
                "eligible_evaluations": bucket.eligible_evaluations,
                "failed_evaluations": bucket.failed_evaluations,
            }
            for bucket in buckets
        ],
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "dataset": dataset,
        "passed": not findings,
        "evidence_sha256": _digest(normalized_evidence),
        "policy": {
            "objective": policy.objective,
            "error_budget": error_budget,
            "min_long_window_evaluations": policy.min_long_window_evaluations,
            "freshness_periods": policy.freshness_periods,
        },
        "summary": {
            "bucket_count": len(buckets),
            "period_seconds": period_seconds,
            "evidence_age_seconds": int(evidence_age),
            "triggered_profile_count": sum(row["triggered"] for row in profile_rows),
            "finding_count": len(findings),
        },
        "profiles": profile_rows,
        "findings": [asdict(finding) for finding in findings],
    }
    report["decision_sha256"] = _digest(report)
    return report


def _reject_constant(value: str) -> None:
    raise QualitySLOError(f"non-finite JSON number {value!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualitySLOError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_evidence(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QualitySLOError("input must be a regular, non-symlink file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise QualitySLOError(f"input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualitySLOError(f"could not read valid JSON: {exc}") from exc
    return _mapping(parsed, "evidence")


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit multi-window data-quality SLO burn")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path, default=Path("quality_slo_report.json"))
    parser.add_argument("--objective", type=float, default=0.995)
    parser.add_argument("--minimum-evaluations", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        policy = QualitySLOPolicy(
            objective=args.objective,
            min_long_window_evaluations=args.minimum_evaluations,
        )
        report = audit_quality_slo(load_evidence(args.evidence), policy)
        write_report(report, args.output)
    except (OSError, QualitySLOError, ValueError) as exc:
        parser.exit(2, f"quality SLO audit error: {exc}\n")
    print(json.dumps({"passed": report["passed"], **report["summary"]}, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
