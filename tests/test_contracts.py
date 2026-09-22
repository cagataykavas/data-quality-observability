import json
from pathlib import Path

import pandas as pd
import pytest

from dq.cli import main
from dq.contracts import run_contract
from quality import temporal_coverage


def contract() -> dict:
    return {
        "dataset": "transactions",
        "checks": [
            {"type": "required_columns", "columns": ["transaction_id", "amount"]},
            {"type": "unique_rate", "column": "transaction_id", "minimum": 1.0},
            {"type": "null_rate", "column": "amount", "maximum": 0.0},
            {"type": "range", "column": "amount", "low": 0, "high": 1000},
        ],
    }


def test_contract_passes_for_clean_frame() -> None:
    frame = pd.DataFrame({"transaction_id": ["a", "b", "c"], "amount": [10.0, 20.0, 30.0]})
    result = run_contract(frame, contract())
    assert result.passed
    assert len(result.checks) == 4


def test_cli_returns_one_for_quality_failure_and_records_history(tmp_path: Path) -> None:
    data = tmp_path / "bad.csv"
    pd.DataFrame({"transaction_id": ["a", "a"], "amount": [10.0, 5000.0]}).to_csv(data, index=False)
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract()), encoding="utf-8")
    history = tmp_path / "history.db"
    output = tmp_path / "report.json"

    code = main(
        [
            "check",
            "--data",
            str(data),
            "--contract",
            str(contract_path),
            "--history",
            str(history),
            "--output",
            str(output),
        ]
    )
    assert code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["run_id"] == 1


def test_cli_returns_two_for_invalid_contract(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    pd.DataFrame({"x": [1]}).to_csv(data, index=False)
    contract_path = tmp_path / "contract.json"
    contract_path.write_text("{}", encoding="utf-8")
    assert main(["check", "--data", str(data), "--contract", str(contract_path)]) == 2


def test_temporal_coverage_accepts_complete_mature_buckets() -> None:
    now = pd.Timestamp("2026-09-22T12:12:00Z")
    frame = pd.DataFrame(
        {
            "event_time": [
                "2026-09-22T09:05:00Z",
                "2026-09-22T10:05:00Z",
                "2026-09-22T11:05:00Z",
                "2026-09-22T12:05:00Z",
            ]
        }
    )
    result = temporal_coverage(
        frame,
        "event_time",
        now,
        window_minutes=180,
        bucket_minutes=60,
        allowed_lateness_minutes=10,
    )
    assert result.passed
    assert result.observed == 1.0
    assert result.evidence == {
        "window_start": "2026-09-22T09:00:00+00:00",
        "window_end_exclusive": "2026-09-22T12:00:00+00:00",
        "expected_bucket_count": 3,
        "covered_bucket_count": 3,
        "underfilled_bucket_starts": [],
        "invalid_timestamp_count": 0,
        "future_timestamp_count": 0,
        "allowed_lateness_minutes": 10,
        "minimum_rows_per_bucket": 1,
    }


def test_temporal_coverage_reports_gap_hidden_by_fresh_record() -> None:
    now = pd.Timestamp("2026-09-22T12:12:00Z")
    frame = pd.DataFrame(
        {
            "event_time": [
                "2026-09-22T09:05:00Z",
                "2026-09-22T11:05:00Z",
                "2026-09-22T12:05:00Z",
            ]
        }
    )
    result = temporal_coverage(
        frame,
        "event_time",
        now,
        window_minutes=180,
        bucket_minutes=60,
        allowed_lateness_minutes=10,
    )
    assert not result.passed
    assert result.observed == pytest.approx(2 / 3)
    assert result.evidence["underfilled_bucket_starts"] == ["2026-09-22T10:00:00+00:00"]


def test_temporal_coverage_respects_row_floor_and_coverage_budget() -> None:
    now = pd.Timestamp("2026-09-22T12:12:00Z")
    frame = pd.DataFrame(
        {
            "event_time": [
                "2026-09-22T09:05:00Z",
                "2026-09-22T09:10:00Z",
                "2026-09-22T10:05:00Z",
                "2026-09-22T11:05:00Z",
                "2026-09-22T11:10:00Z",
            ]
        }
    )
    result = temporal_coverage(
        frame,
        "event_time",
        now,
        window_minutes=180,
        bucket_minutes=60,
        minimum_rows_per_bucket=2,
        minimum_coverage=2 / 3,
    )
    assert result.passed
    assert result.evidence["covered_bucket_count"] == 2


@pytest.mark.parametrize("bad_value", [None, "not-a-time"])
def test_temporal_coverage_fails_closed_on_invalid_timestamps(bad_value: object) -> None:
    result = temporal_coverage(
        pd.DataFrame({"event_time": ["2026-09-22T11:05:00Z", bad_value]}),
        "event_time",
        pd.Timestamp("2026-09-22T12:00:00Z"),
        window_minutes=60,
        bucket_minutes=60,
    )
    assert not result.passed
    assert result.evidence["invalid_timestamp_count"] == 1


def test_temporal_coverage_fails_closed_on_future_timestamp() -> None:
    result = temporal_coverage(
        pd.DataFrame({"event_time": ["2026-09-22T12:01:00Z"]}),
        "event_time",
        pd.Timestamp("2026-09-22T12:00:00Z"),
        window_minutes=60,
        bucket_minutes=60,
    )
    assert not result.passed
    assert result.evidence["future_timestamp_count"] == 1


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"window_minutes": 90, "bucket_minutes": 60}, "divisible"),
        ({"window_minutes": 60.5, "bucket_minutes": 60}, "integers"),
        (
            {
                "window_minutes": 60,
                "bucket_minutes": 60,
                "minimum_coverage": float("nan"),
            },
            "finite",
        ),
    ],
)
def test_temporal_coverage_rejects_invalid_policy(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        temporal_coverage(
            pd.DataFrame({"event_time": []}),
            "event_time",
            pd.Timestamp("2026-09-22T12:00:00Z"),
            **kwargs,
        )


def test_contract_exposes_temporal_coverage_evidence() -> None:
    result = run_contract(
        pd.DataFrame(
            {
                "event_time": [
                    "2026-09-22T09:05:00Z",
                    "2026-09-22T11:05:00Z",
                ]
            }
        ),
        {
            "dataset": "events",
            "checks": [
                {
                    "type": "temporal_coverage",
                    "column": "event_time",
                    "window_minutes": 180,
                    "bucket_minutes": 60,
                }
            ],
        },
        now=pd.Timestamp("2026-09-22T12:00:00Z"),
    )
    payload = result.as_dict()
    assert not payload["passed"]
    assert payload["checks"][0]["evidence"]["underfilled_bucket_starts"] == [
        "2026-09-22T10:00:00+00:00"
    ]
