from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from dq.cli import main
from dq.contracts import ContractError, compile_contract, run_contract
from dq.history import QualityHistory


def _contract() -> dict:
    return {
        "dataset": "transactions",
        "checks": [
            {"type": "required_columns", "columns": ["id", "amount"]},
            {"type": "null_rate", "column": "amount", "maximum": 0.0},
        ],
    }


def test_semantically_equivalent_contracts_have_same_sha256() -> None:
    implicit_default = compile_contract(
        {"dataset": "events", "checks": [{"type": "null_rate", "column": "value"}]}
    )
    explicit_default = compile_contract(
        {
            "checks": [{"column": "value", "maximum": 0.01, "type": "null_rate"}],
            "dataset": "events",
        }
    )

    assert implicit_default.sha256 == explicit_default.sha256


@pytest.mark.parametrize(
    "check",
    [
        {"type": "null_rate", "column": "x", "maximum": 1.1},
        {"type": "range", "column": "x", "low": 10, "high": 1},
        {"type": "freshness", "column": "x", "max_age_minutes": 0},
        {"type": "required_columns", "columns": ["x", "x"]},
        {"type": "row_count", "minimum": True},
    ],
)
def test_invalid_check_configuration_fails_at_compile_time(check: dict) -> None:
    with pytest.raises(ContractError):
        compile_contract({"dataset": "events", "checks": [check]})


def test_duplicate_json_keys_fail_before_data_is_read(tmp_path: Path, capsys) -> None:
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        '{"dataset":"first","dataset":"second","checks":[{"type":"row_count"}]}',
        encoding="utf-8",
    )

    code = main(
        [
            "check",
            "--contract",
            str(contract_path),
            "--data",
            str(tmp_path / "does-not-exist.csv"),
        ]
    )

    assert code == 2
    assert "duplicate JSON key" in capsys.readouterr().out


def test_report_carries_contract_identity_and_is_atomically_replaced(tmp_path: Path) -> None:
    contract = _contract()
    frame = pd.DataFrame({"id": ["a", "b"], "amount": [10.0, 20.0]})
    result = run_contract(frame, contract)
    data_path = tmp_path / "data.csv"
    frame.to_csv(data_path, index=False)
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    output = tmp_path / "nested" / "report.json"
    history_path = tmp_path / "history.db"

    assert (
        main(
            [
                "check",
                "--data",
                str(data_path),
                "--contract",
                str(contract_path),
                "--output",
                str(output),
                "--history",
                str(history_path),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["contract_sha256"] == result.contract_sha256
    assert len(payload["contract_sha256"]) == 64
    assert (
        QualityHistory(history_path).recent("transactions")[0]["contract_sha256"]
        == result.contract_sha256
    )
    assert list(output.parent.glob("*.tmp")) == []
