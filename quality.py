from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any

import pandas as pd


@dataclass
class CheckResult:
    name: str
    passed: bool
    observed: float
    threshold: float
    evidence: dict[str, Any] | None = None


def null_rate(df: pd.DataFrame, column: str, maximum: float = 0.01) -> CheckResult:
    value = float(df[column].isna().mean())
    return CheckResult(f"null_rate:{column}", value <= maximum, value, maximum)


def unique_rate(df: pd.DataFrame, column: str, minimum: float = 0.99) -> CheckResult:
    value = float(df[column].nunique(dropna=True) / max(len(df), 1))
    return CheckResult(f"unique_rate:{column}", value >= minimum, value, minimum)


def range_check(df: pd.DataFrame, column: str, low: float, high: float) -> CheckResult:
    valid = df[column].between(low, high) | df[column].isna()
    value = float(valid.mean())
    return CheckResult(f"range:{column}", value == 1.0, value, 1.0)


def freshness(
    df: pd.DataFrame, timestamp_column: str, now: pd.Timestamp, max_age_minutes: float
) -> CheckResult:
    newest = pd.to_datetime(df[timestamp_column], utc=True).max()
    age = float((now - newest).total_seconds() / 60)
    return CheckResult(
        f"freshness:{timestamp_column}", age <= max_age_minutes, age, max_age_minutes
    )


def temporal_coverage(
    df: pd.DataFrame,
    timestamp_column: str,
    now: pd.Timestamp,
    *,
    window_minutes: int,
    bucket_minutes: int,
    allowed_lateness_minutes: int = 0,
    minimum_rows_per_bucket: int = 1,
    minimum_coverage: float = 1.0,
) -> CheckResult:
    """Measure complete, mature time buckets in a rolling UTC window.

    The newest ``allowed_lateness_minutes`` are excluded from the evaluated
    window so an in-flight partition does not create a false incident.
    """
    integer_policy = {
        "window_minutes": window_minutes,
        "bucket_minutes": bucket_minutes,
        "allowed_lateness_minutes": allowed_lateness_minutes,
        "minimum_rows_per_bucket": minimum_rows_per_bucket,
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in integer_policy.values()
    ):
        raise ValueError("temporal coverage minute and row policies must be integers")
    if window_minutes <= 0 or bucket_minutes <= 0:
        raise ValueError("window_minutes and bucket_minutes must be positive")
    if window_minutes % bucket_minutes:
        raise ValueError("window_minutes must be divisible by bucket_minutes")
    if allowed_lateness_minutes < 0:
        raise ValueError("allowed_lateness_minutes must be non-negative")
    if minimum_rows_per_bucket < 1:
        raise ValueError("minimum_rows_per_bucket must be positive")
    if not isfinite(minimum_coverage) or not 0.0 <= minimum_coverage <= 1.0:
        raise ValueError("minimum_coverage must be finite and between zero and one")

    current = pd.Timestamp(now)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.tz_convert("UTC")
    frequency = f"{bucket_minutes}min"
    mature_end = (current - pd.Timedelta(minutes=allowed_lateness_minutes)).floor(frequency)
    window_start = mature_end - pd.Timedelta(minutes=window_minutes)
    expected = pd.date_range(window_start, mature_end, freq=frequency, inclusive="left")

    timestamps = pd.to_datetime(df[timestamp_column], utc=True, errors="coerce")
    invalid_count = int(timestamps.isna().sum())
    future_count = int((timestamps > current).sum())
    eligible = timestamps[(timestamps >= window_start) & (timestamps < mature_end)]
    counts = eligible.dt.floor(frequency).value_counts()
    underfilled = [
        timestamp
        for timestamp in expected
        if int(counts.get(timestamp, 0)) < minimum_rows_per_bucket
    ]
    covered = len(expected) - len(underfilled)
    coverage = float(covered / len(expected))
    passed = coverage >= minimum_coverage and invalid_count == 0 and future_count == 0
    evidence = {
        "window_start": window_start.isoformat(),
        "window_end_exclusive": mature_end.isoformat(),
        "expected_bucket_count": len(expected),
        "covered_bucket_count": covered,
        "underfilled_bucket_starts": [value.isoformat() for value in underfilled],
        "invalid_timestamp_count": invalid_count,
        "future_timestamp_count": future_count,
        "allowed_lateness_minutes": allowed_lateness_minutes,
        "minimum_rows_per_bucket": minimum_rows_per_bucket,
    }
    return CheckResult(
        f"temporal_coverage:{timestamp_column}",
        passed,
        coverage,
        minimum_coverage,
        evidence,
    )


def report(results: list[CheckResult]) -> str:
    payload = {"passed": all(r.passed for r in results), "checks": [asdict(r) for r in results]}
    return json.dumps(payload, indent=2)


if __name__ == "__main__":
    now = pd.Timestamp.now(tz="UTC")
    frame = pd.DataFrame(
        {
            "transaction_id": ["a", "b", "c"],
            "amount": [10.0, 22.5, 7.0],
            "event_time": [now - pd.Timedelta(minutes=2)] * 3,
        }
    )
    checks = [
        null_rate(frame, "amount"),
        unique_rate(frame, "transaction_id"),
        range_check(frame, "amount", 0, 100000),
        freshness(frame, "event_time", now, 10),
    ]
    print(report(checks))
