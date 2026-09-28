from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dq.metric_cardinality import (
    SCHEMA_VERSION,
    ArtifactError,
    CardinalityPolicy,
    MetricPolicy,
    audit_metric_cardinality,
    load_json,
    policy_from_mapping,
)

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)


def policy() -> CardinalityPolicy:
    return CardinalityPolicy(
        metrics=(
            MetricPolicy(
                name="dq_check_total",
                kind="counter",
                allowed_labels=("dataset", "check", "status"),
                required_labels=("dataset", "check", "status"),
                max_series=12,
                value_limits=(("dataset", 2), ("check", 3), ("status", 2)),
            ),
            MetricPolicy(
                name="dq_duration_seconds_bucket",
                kind="histogram",
                allowed_labels=("dataset", "le"),
                required_labels=("dataset", "le"),
                max_series=8,
                value_limits=(("dataset", 2), ("le", 4)),
            ),
        ),
        max_total_series=30,
    )


def raw_policy() -> dict[str, object]:
    value = policy()
    return {
        "metrics": [
            {
                "name": item.name,
                "kind": item.kind,
                "allowed_labels": list(item.allowed_labels),
                "required_labels": list(item.required_labels),
                "max_series": item.max_series,
                "value_limits": dict(item.value_limits),
            }
            for item in value.metrics
        ],
        "max_total_series": value.max_total_series,
        "max_labels_per_series": value.max_labels_per_series,
        "max_label_value_bytes": value.max_label_value_bytes,
        "max_snapshot_age_seconds": value.max_snapshot_age_seconds,
        "max_future_skew_seconds": value.max_future_skew_seconds,
        "max_reported_findings": value.max_reported_findings,
        "forbidden_labels": list(value.forbidden_labels),
    }


def series(metric: str, labels: dict[str, str], kind: str = "counter") -> dict[str, object]:
    return {"metric": metric, "kind": kind, "labels": labels}


def clean_series() -> list[dict[str, object]]:
    return [
        series(
            "dq_check_total",
            {"dataset": dataset, "check": check, "status": status},
        )
        for dataset in ("transactions", "accounts")
        for check in ("freshness", "null_rate")
        for status in ("pass", "fail")
    ]


def artifact(values: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2026-09-28T16:59:00Z",
        "collector_id": "otel-collector/prod-a",
        "resource_revision": "data-quality:v0.2.0@sha256:abc",
        "series": values,
    }


def test_accepts_bounded_declared_series() -> None:
    report = audit_metric_cardinality(artifact(clean_series()), policy(), now=NOW)
    assert report.accepted
    assert report.total_series == 8
    assert report.metric_count == 1
    assert dict(report.metrics[0].label_cardinalities) == {
        "check": 2,
        "dataset": 2,
        "status": 2,
    }


def test_detects_label_value_cardinality_breach() -> None:
    values = clean_series()
    values.append(
        series(
            "dq_check_total",
            {"dataset": "loans", "check": "freshness", "status": "pass"},
        )
    )
    report = audit_metric_cardinality(artifact(values), policy(), now=NOW)
    assert not report.accepted
    assert "LABEL_VALUE_LIMIT" in report.reason_codes
    finding = next(item for item in report.findings if item.code == "LABEL_VALUE_LIMIT")
    assert (finding.label, finding.observed, finding.limit) == ("dataset", 3, 2)


def test_detects_per_metric_series_limit() -> None:
    constrained = CardinalityPolicy(
        metrics=(
            MetricPolicy(
                name="dq_check_total",
                kind="counter",
                allowed_labels=("dataset", "check", "status"),
                required_labels=("dataset",),
                max_series=3,
            ),
        )
    )
    report = audit_metric_cardinality(artifact(clean_series()), constrained, now=NOW)
    assert "METRIC_SERIES_LIMIT" in report.reason_codes


def test_detects_undeclared_metric() -> None:
    values = clean_series() + [series("unexpected_metric", {"dataset": "transactions"})]
    report = audit_metric_cardinality(artifact(values), policy(), now=NOW)
    assert "UNDECLARED_METRIC" in report.reason_codes


def test_detects_metric_kind_change() -> None:
    values = clean_series()
    values[0]["kind"] = "gauge"
    report = audit_metric_cardinality(artifact(values), policy(), now=NOW)
    assert "METRIC_KIND_MISMATCH" in report.reason_codes


def test_detects_unexpected_and_missing_labels() -> None:
    values = [
        series(
            "dq_check_total",
            {"dataset": "transactions", "check": "freshness", "region": "eu"},
        )
    ]
    report = audit_metric_cardinality(artifact(values), policy(), now=NOW)
    assert {"UNEXPECTED_LABEL", "MISSING_REQUIRED_LABEL"}.issubset(report.reason_codes)


@pytest.mark.parametrize("label", ["user_id", "request_id", "trace_id", "email"])
def test_detects_forbidden_high_cardinality_identity_labels(label: str) -> None:
    values = clean_series()
    values[0]["labels"][label] = "sensitive-value"  # type: ignore[index]
    report = audit_metric_cardinality(artifact(values), policy(), now=NOW)
    assert "FORBIDDEN_HIGH_CARDINALITY_LABEL" in report.reason_codes


def test_report_never_emits_label_values_or_collector_identity() -> None:
    report = audit_metric_cardinality(artifact(clean_series()), policy(), now=NOW)
    rendered = json.dumps(report.as_dict())
    assert "transactions" not in rendered
    assert "otel-collector/prod-a" not in rendered


def test_input_order_and_label_order_do_not_change_evidence() -> None:
    values = clean_series()
    reordered = []
    for item in reversed(values):
        labels = dict(reversed(list(item["labels"].items())))  # type: ignore[union-attr]
        reordered.append(series(str(item["metric"]), labels))
    first = audit_metric_cardinality(artifact(values), policy(), now=NOW)
    second = audit_metric_cardinality(artifact(reordered), policy(), now=NOW)
    assert first.evidence_digest == second.evidence_digest
    assert first.as_dict() == second.as_dict()


def test_policy_change_changes_evidence_identity() -> None:
    first = audit_metric_cardinality(artifact(clean_series()), policy(), now=NOW)
    changed = CardinalityPolicy(metrics=policy().metrics, max_total_series=31)
    second = audit_metric_cardinality(artifact(clean_series()), changed, now=NOW)
    assert first.policy_digest != second.policy_digest
    assert first.evidence_digest != second.evidence_digest


def test_findings_are_deduplicated_and_bounded() -> None:
    values = [
        series("dq_check_total", {"dataset": f"d-{index}", "check": "freshness"})
        for index in range(10)
    ]
    constrained = CardinalityPolicy(
        metrics=policy().metrics,
        max_total_series=30,
        max_reported_findings=1,
    )
    report = audit_metric_cardinality(artifact(values), constrained, now=NOW)
    assert report.finding_count > len(report.findings)
    assert len(report.findings) == 1


def test_rejects_duplicate_series() -> None:
    values = clean_series()
    values.append(dict(values[0]))
    with pytest.raises(ArtifactError, match="duplicate metric series"):
        audit_metric_cardinality(artifact(values), policy(), now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("metric", "bad metric", "metric name"),
        ("kind", "set", "metric kind"),
        ("labels", {"bad-label": "value"}, "label name"),
        ("labels", {"dataset": 42}, "label values"),
    ],
)
def test_rejects_malformed_series(field: str, value: object, message: str) -> None:
    item = clean_series()[0]
    item[field] = value
    with pytest.raises(ArtifactError, match=message):
        audit_metric_cardinality(artifact([item]), policy(), now=NOW)


def test_rejects_label_count_and_value_byte_budgets() -> None:
    strict = CardinalityPolicy(
        metrics=policy().metrics,
        max_labels_per_series=2,
        max_label_value_bytes=4,
    )
    with pytest.raises(ArtifactError, match="labels-per-series"):
        audit_metric_cardinality(artifact(clean_series()[:1]), strict, now=NOW)
    item = series("dq_check_total", {"dataset": "long-value"})
    with pytest.raises(ArtifactError, match="value byte"):
        audit_metric_cardinality(artifact([item]), strict, now=NOW)


def test_rejects_total_series_hard_budget() -> None:
    strict = CardinalityPolicy(metrics=policy().metrics, max_total_series=3)
    with pytest.raises(ArtifactError, match="total series"):
        audit_metric_cardinality(artifact(clean_series()), strict, now=NOW)


def test_rejects_stale_future_and_naive_timestamps() -> None:
    stale = artifact(clean_series())
    stale["generated_at"] = "2026-09-28T16:00:00Z"
    with pytest.raises(ArtifactError, match="stale"):
        audit_metric_cardinality(stale, policy(), now=NOW)
    future = artifact(clean_series())
    future["generated_at"] = "2026-09-28T17:10:00Z"
    with pytest.raises(ArtifactError, match="future-dated"):
        audit_metric_cardinality(future, policy(), now=NOW)
    naive = artifact(clean_series())
    naive["generated_at"] = "2026-09-28T16:59:00"
    with pytest.raises(ArtifactError, match="timezone"):
        audit_metric_cardinality(naive, policy(), now=NOW)


def test_rejects_unknown_fields_and_invalid_policies() -> None:
    value = artifact(clean_series())
    value["unknown"] = True
    with pytest.raises(ArtifactError, match="artifact fields"):
        audit_metric_cardinality(value, policy(), now=NOW)
    with pytest.raises(ArtifactError, match="unique"):
        CardinalityPolicy(metrics=(policy().metrics[0], policy().metrics[0]))
    with pytest.raises(ArtifactError, match="required labels"):
        MetricPolicy(
            name="metric",
            kind="counter",
            allowed_labels=("allowed",),
            required_labels=("missing",),
        )


def test_policy_mapping_round_trip_and_strict_shape() -> None:
    parsed = policy_from_mapping(raw_policy())
    assert parsed == policy()
    invalid = raw_policy()
    invalid["extra"] = True
    with pytest.raises(ArtifactError, match="policy fields"):
        policy_from_mapping(invalid)


def test_loader_rejects_duplicate_keys_and_byte_overflow(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text('{"key":1,"key":2}', encoding="utf-8")
    with pytest.raises(ArtifactError, match="duplicate JSON"):
        load_json(path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactError, match="byte budget"):
        load_json(path, max_bytes=1)


def test_cli_exit_codes_and_atomic_output(tmp_path: Path) -> None:
    generated_at = datetime.now(UTC)
    value = artifact(clean_series())
    value["generated_at"] = generated_at.isoformat()
    snapshot_path = tmp_path / "snapshot.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "report.json"
    snapshot_path.write_text(json.dumps(value), encoding="utf-8")
    policy_path.write_text(json.dumps(raw_policy()), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "dq.metric_cardinality",
        str(snapshot_path),
        "--policy",
        str(policy_path),
        "--output",
        str(output_path),
    ]
    accepted = subprocess.run(command, check=False, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(output_path.read_text())["accepted"] is True

    value["series"].append(series("unknown", {}))  # type: ignore[union-attr]
    snapshot_path.write_text(json.dumps(value), encoding="utf-8")
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode == 2
    assert json.loads(output_path.read_text())["accepted"] is False

    value["generated_at"] = (generated_at - timedelta(hours=1)).isoformat()
    snapshot_path.write_text(json.dumps(value), encoding="utf-8")
    malformed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert malformed.returncode == 3
