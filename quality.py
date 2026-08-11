from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import pandas as pd


@dataclass
class CheckResult:
    name: str
    passed: bool
    observed: float
    threshold: float


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


def freshness(df: pd.DataFrame, timestamp_column: str, now: pd.Timestamp, max_age_minutes: float) -> CheckResult:
    newest = pd.to_datetime(df[timestamp_column], utc=True).max()
    age = float((now - newest).total_seconds() / 60)
    return CheckResult(f"freshness:{timestamp_column}", age <= max_age_minutes, age, max_age_minutes)


def report(results: list[CheckResult]) -> str:
    payload = {"passed": all(r.passed for r in results), "checks": [asdict(r) for r in results]}
    return json.dumps(payload, indent=2)


if __name__ == "__main__":
    now = pd.Timestamp.now(tz="UTC")
    frame = pd.DataFrame({
        "transaction_id": ["a", "b", "c"],
        "amount": [10.0, 22.5, 7.0],
        "event_time": [now - pd.Timedelta(minutes=2)] * 3,
    })
    checks = [null_rate(frame, "amount"), unique_rate(frame, "transaction_id"), range_check(frame, "amount", 0, 100000), freshness(frame, "event_time", now, 10)]
    print(report(checks))
