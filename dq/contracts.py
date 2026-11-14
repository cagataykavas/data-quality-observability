from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quality import CheckResult, freshness, null_rate, range_check, unique_rate

MAX_CONTRACT_BYTES = 1_000_000
MAX_CHECKS = 128
_DATASET_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True)
class CompiledContract:
    dataset: str
    checks: tuple[dict[str, Any], ...]
    sha256: str


@dataclass(frozen=True)
class ContractResult:
    dataset: str
    contract_sha256: str
    passed: bool
    rows: int
    columns: int
    checked_at: str
    checks: tuple[CheckResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "contract_sha256": self.contract_sha256,
            "passed": self.passed,
            "rows": self.rows,
            "columns": self.columns,
            "checked_at": self.checked_at,
            "checks": [asdict(check) for check in self.checks],
        }


class ContractError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number is not allowed: {value}")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return result


def _column(value: Any, label: str = "column") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ContractError(f"{label} must be a non-blank string of at most 256 characters")
    if any(ord(character) < 32 for character in value):
        raise ContractError(f"{label} must not contain control characters")
    return value


def _only_keys(spec: dict[str, Any], allowed: set[str], check_type: str) -> None:
    unexpected = sorted(set(spec) - allowed)
    if unexpected:
        raise ContractError(f"{check_type} check has unexpected keys: {unexpected}")


def _compile_check(spec: Any, index: int) -> dict[str, Any]:
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        raise ContractError(f"check #{index} requires a string type")
    check_type = spec["type"]

    if check_type in {"null_rate", "unique_rate"}:
        threshold_key = "maximum" if check_type == "null_rate" else "minimum"
        default = 0.01 if check_type == "null_rate" else 0.99
        _only_keys(spec, {"type", "column", threshold_key}, check_type)
        threshold = _number(spec.get(threshold_key, default), threshold_key)
        if not 0.0 <= threshold <= 1.0:
            raise ContractError(f"{check_type} {threshold_key} must be within [0, 1]")
        return {
            "type": check_type,
            "column": _column(spec.get("column")),
            threshold_key: threshold,
        }

    if check_type == "range":
        _only_keys(spec, {"type", "column", "low", "high"}, check_type)
        if "low" not in spec or "high" not in spec:
            raise ContractError("range check requires low and high")
        low = _number(spec["low"], "low")
        high = _number(spec["high"], "high")
        if low > high:
            raise ContractError("range low must not exceed high")
        return {
            "type": check_type,
            "column": _column(spec.get("column")),
            "low": low,
            "high": high,
        }

    if check_type == "freshness":
        _only_keys(spec, {"type", "column", "max_age_minutes"}, check_type)
        maximum = _number(spec.get("max_age_minutes", 60), "max_age_minutes")
        if maximum <= 0:
            raise ContractError("freshness max_age_minutes must be greater than zero")
        return {
            "type": check_type,
            "column": _column(spec.get("column")),
            "max_age_minutes": maximum,
        }

    if check_type == "row_count":
        _only_keys(spec, {"type", "minimum"}, check_type)
        minimum = spec.get("minimum", 1)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ContractError("row_count minimum must be a positive integer")
        return {"type": check_type, "minimum": minimum}

    if check_type == "required_columns":
        _only_keys(spec, {"type", "columns"}, check_type)
        columns = spec.get("columns")
        if not isinstance(columns, list) or not columns or len(columns) > 256:
            raise ContractError("required_columns requires 1 to 256 columns")
        normalized = [_column(value, "required column") for value in columns]
        if len(set(normalized)) != len(normalized):
            raise ContractError("required_columns must not contain duplicates")
        return {"type": check_type, "columns": normalized}

    raise ContractError(f"unknown check type: {check_type}")


def compile_contract(contract: dict[str, Any] | CompiledContract) -> CompiledContract:
    if isinstance(contract, CompiledContract):
        return contract
    if not isinstance(contract, dict):
        raise ContractError("contract must be a JSON object")
    if set(contract) != {"dataset", "checks"}:
        raise ContractError("contract must contain only 'dataset' and 'checks'")
    dataset = contract["dataset"]
    if not isinstance(dataset, str) or _DATASET_NAME.fullmatch(dataset) is None:
        raise ContractError("dataset must match [A-Za-z0-9_.-]{1,128}")
    checks = contract["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
        raise ContractError(f"contract requires 1 to {MAX_CHECKS} checks")
    normalized = tuple(_compile_check(spec, index) for index, spec in enumerate(checks, 1))
    identities = [json.dumps(spec, sort_keys=True, separators=(",", ":")) for spec in normalized]
    if len(set(identities)) != len(identities):
        raise ContractError("contract contains duplicate checks")
    payload = {"dataset": dataset, "checks": normalized}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return CompiledContract(
        dataset=dataset,
        checks=normalized,
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def load_contract(path: str | Path) -> CompiledContract:
    source = Path(path)
    if source.stat().st_size > MAX_CONTRACT_BYTES:
        raise ContractError(f"contract exceeds {MAX_CONTRACT_BYTES} bytes")
    payload = json.loads(
        source.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_constant,
    )
    return compile_contract(payload)


def _require_column(frame: pd.DataFrame, column: str) -> None:
    if column not in frame.columns:
        raise ContractError(f"required column is missing: {column}")


def run_contract(
    frame: pd.DataFrame,
    contract: dict[str, Any] | CompiledContract,
    *,
    now: pd.Timestamp | None = None,
) -> ContractResult:
    compiled = compile_contract(contract)
    now = now or pd.Timestamp.now(tz="UTC")
    results: list[CheckResult] = []

    for spec in compiled.checks:
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
            results.append(range_check(frame, str(column), float(spec["low"]), float(spec["high"])))
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
            required = list(spec["columns"])
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
    return ContractResult(
        dataset=compiled.dataset,
        contract_sha256=compiled.sha256,
        passed=all(result.passed for result in results),
        rows=len(frame),
        columns=len(frame.columns),
        checked_at=datetime.now(UTC).isoformat(),
        checks=tuple(results),
    )
