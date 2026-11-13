from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from dq.slo_burn_rate import (
    BurnProfile,
    QualitySLOError,
    QualitySLOPolicy,
    audit_quality_slo,
    load_evidence,
    main,
)


def _evidence(
    *,
    bucket_count: int = 72,
    eligible: int = 100,
    failed_by_index: dict[int, int] | None = None,
    age_periods: int = 0,
) -> dict[str, object]:
    period = 300
    as_of = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    newest_end = as_of - timedelta(seconds=period * age_periods)
    failed_by_index = failed_by_index or {}
    buckets = []
    for index in range(bucket_count):
        end = newest_end - timedelta(seconds=period * (bucket_count - index - 1))
        buckets.append(
            {
                "end": end.isoformat().replace("+00:00", "Z"),
                "eligible_evaluations": eligible,
                "failed_evaluations": failed_by_index.get(index, 0),
            }
        )
    return {
        "schema_version": 1,
        "dataset": "transactions",
        "period_seconds": period,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "buckets": buckets,
    }


def test_healthy_windows_pass_with_canonical_digests():
    first = audit_quality_slo(_evidence())
    reordered = _evidence()
    reordered = {
        "buckets": reordered["buckets"],
        "as_of": reordered["as_of"],
        "period_seconds": reordered["period_seconds"],
        "dataset": reordered["dataset"],
        "schema_version": reordered["schema_version"],
    }
    second = audit_quality_slo(reordered)

    assert first["passed"] is True
    assert first["summary"]["triggered_profile_count"] == 0
    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first["decision_sha256"] == second["decision_sha256"]


def test_small_single_bucket_spike_does_not_page():
    report = audit_quality_slo(_evidence(failed_by_index={71: 5}))

    assert report["passed"] is True
    fast = next(profile for profile in report["profiles"] if profile["name"] == "fast")
    assert fast["short"]["burn_rate"] == pytest.approx(10.0)
    assert fast["long_breached"] is False


def test_sustained_fast_burn_triggers_page_profile():
    failures = {index: 10 for index in range(60, 72)}
    report = audit_quality_slo(_evidence(failed_by_index=failures))

    fast = next(profile for profile in report["profiles"] if profile["name"] == "fast")
    assert report["passed"] is False
    assert fast["triggered"] is True
    assert fast["severity"] == "page"
    assert any(
        finding["code"] == "multi_window_burn_exceeded" and finding["profile"] == "fast"
        for finding in report["findings"]
    )


def test_slow_burn_triggers_ticket_profile():
    failures = {index: 4 for index in range(72)}
    report = audit_quality_slo(_evidence(failed_by_index=failures))

    slow = next(profile for profile in report["profiles"] if profile["name"] == "slow")
    assert slow["triggered"] is True
    assert slow["short"]["burn_rate"] == pytest.approx(8.0)
    assert slow["long"]["burn_rate"] == pytest.approx(8.0)


def test_stale_evidence_fails_closed():
    report = audit_quality_slo(_evidence(age_periods=2))

    assert report["passed"] is False
    assert {finding["code"] for finding in report["findings"]} == {"stale_evidence"}


def test_insufficient_history_and_evaluations_are_reported():
    report = audit_quality_slo(_evidence(bucket_count=10, eligible=1))

    codes = {finding["code"] for finding in report["findings"]}
    assert report["passed"] is False
    assert codes == {"insufficient_buckets", "insufficient_evaluations"}


@pytest.mark.parametrize(
    "mutate, match",
    [
        (
            lambda item: item["buckets"][1].update(end=item["buckets"][0]["end"]),
            "strictly ordered",
        ),
        (
            lambda item: item["buckets"][1].update(end="2026-11-13T06:10:00.500000Z"),
            "exactly 300 seconds apart",
        ),
        (
            lambda item: item["buckets"][0].update(failed_evaluations=101),
            "between 0 and 100",
        ),
        (
            lambda item: item["buckets"][0].update(eligible_evaluations=True),
            "must be an integer",
        ),
        (lambda item: item.update(as_of="2026-11-13T15:00:00+03:00"), "must use UTC"),
        (lambda item: item.update(dataset=" transactions"), "dataset must be"),
    ],
)
def test_malformed_evidence_is_rejected(mutate, match):
    evidence = _evidence()
    mutate(evidence)
    with pytest.raises(QualitySLOError, match=match):
        audit_quality_slo(evidence)


def test_loader_rejects_duplicate_json_keys(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    with pytest.raises(QualitySLOError, match="duplicate JSON key"):
        load_evidence(evidence)


def test_cli_writes_rejection_report(tmp_path):
    evidence = tmp_path / "evidence.json"
    output = tmp_path / "report.json"
    failures = {index: 10 for index in range(60, 72)}
    evidence.write_text(json.dumps(_evidence(failed_by_index=failures)), encoding="utf-8")

    exit_code = main([str(evidence), "--output", str(output)])

    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert len(payload["decision_sha256"]) == 64


def test_policy_rejects_duplicate_profile_names():
    profile = BurnProfile("same", 1, 12, 4.0, "page")
    with pytest.raises(ValueError, match="unique"):
        QualitySLOPolicy(profiles=(profile, profile))
