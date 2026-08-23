from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any

import pandas as pd

from quality import CheckResult, freshness, null_rate, range_check, unique_rate


@dataclass(frozen=True)
class ContractResult:
    dataset: str
    passed: bool
    rows: int
    columns: int
    checked_at: str
    checks: tuple[CheckResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "passed": self.passed,
            "rows": self.rows,
            "columns": self.columns,
            "checked_at": self.checked_at,
            "checks": [asdict(check) for check in self.checks],
        }


class ContractError(ValueError):
    pass


def load_contract(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "dataset" not in payload or "checks" not in payload:
        raise ContractError("contract requires 'dataset' and 'checks'")
    if not isinstance(payload["checks"], list):
        raise ContractError("contract 'checks' must be a list")
    return payload


def _require_column(frame: pd.DataFrame, column: str) -> None:
    if column not in frame.columns:
        raise ContractError(f"required column is missing: {column}")


def run_contract(
    frame: pd.DataFrame,
    contract: dict[str, Any],
    *,
    now: pd.Timestamp | None = None,
) -> ContractResult:
    now = now or pd.Timestamp.now(tz="UTC")
    results: list[CheckResult] = []

    for index, spec in enumerate(contract["checks"]):
        if not isinstance(spec, dict) or "type" not in spec:
            raise ContractError(f"check #{index + 1} requires a type")
        check_type = str(spec["type"])
        column = spec.get("column")

        if check_type == "null_rate":
            _require_column(frame, str(column))
            results.append(null_rate(frame, str(column), float(spec.get("maximum", 0.01))))
        elif check_type == "unique_rate":
            _require_column(frame, str(column))
            results.append(unique_rate(frame, str(column), float(spec.get("minimum", 0.99))))
        elif check_type == "range":
            _require_column(frame, str(column))
            if "low" not in spec or "high" not in spec:
                raise ContractError("range check requires low and high")
            results.append(
                range_check(frame, str(column), float(spec["low"]), float(spec["high"]))
            )
        elif check_type == "freshness":
            _require_column(frame, str(column))
            results.append(
                freshness(
                    frame,
                    str(column),
                    now,
                    float(spec.get("max_age_minutes", 60)),
                )
            )
        elif check_type == "row_count":
            minimum = int(spec.get("minimum", 1))
            observed = len(frame)
            results.append(
                CheckResult(
                    "row_count",
                    observed >= minimum,
                    float(observed),
                    float(minimum),
                )
            )
        elif check_type == "required_columns":
            required = [str(value) for value in spec.get("columns", [])]
            missing = [value for value in required if value not in frame.columns]
            observed = float(len(required) - len(missing)) / max(len(required), 1)
            results.append(
                CheckResult(
                    "required_columns",
                    not missing,
                    observed,
                    1.0,
                )
            )
        else:
            raise ContractError(f"unknown check type: {check_type}")

    return ContractResult(
        dataset=str(contract["dataset"]),
        passed=all(result.passed for result in results),
        rows=len(frame),
        columns=len(frame.columns),
        checked_at=datetime.now(timezone.utc).isoformat(),
        checks=tuple(results),
    )
