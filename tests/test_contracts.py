from pathlib import Path
import json

import pandas as pd

from dq.cli import main
from dq.contracts import run_contract


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
    frame = pd.DataFrame(
        {"transaction_id": ["a", "b", "c"], "amount": [10.0, 20.0, 30.0]}
    )
    result = run_contract(frame, contract())
    assert result.passed
    assert len(result.checks) == 4


def test_cli_returns_one_for_quality_failure_and_records_history(tmp_path: Path) -> None:
    data = tmp_path / "bad.csv"
    pd.DataFrame(
        {"transaction_id": ["a", "a"], "amount": [10.0, 5000.0]}
    ).to_csv(data, index=False)
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
