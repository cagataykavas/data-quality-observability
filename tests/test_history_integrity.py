from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dq.cli import main
from dq.contracts import ContractResult
from dq.history import HistoryIntegrityError, QualityHistory


def _result(index: int, *, dataset: str = "transactions") -> ContractResult:
    return ContractResult(
        dataset=dataset,
        passed=index % 2 == 0,
        rows=100 + index,
        columns=5,
        checked_at=f"2026-11-12T0{index}:00:00+00:00",
        checks=(),
    )


def test_history_verifies_and_exposes_chain_evidence(tmp_path: Path) -> None:
    history = QualityHistory(tmp_path / "history.db")
    first_id = history.record(_result(1))
    second_id = history.record(_result(2))

    rows = history.recent("transactions")

    assert [row["run_id"] for row in rows] == [second_id, first_id]
    assert rows[0]["previous_chain_sha256"] == rows[1]["chain_sha256"]
    assert all(len(row["report_sha256"]) == 64 for row in rows)
    assert all(len(row["chain_sha256"]) == 64 for row in rows)


def test_history_rejects_mutated_report_bytes(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    history = QualityHistory(database)
    history.record(_result(1))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE quality_runs SET report_json = '{}' WHERE id = 1")

    with pytest.raises(HistoryIntegrityError, match="report hash mismatch at run 1"):
        history.recent("transactions")


def test_history_rejects_deleted_middle_run(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    history = QualityHistory(database)
    for index in range(1, 4):
        history.record(_result(index))
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM quality_runs WHERE id = 2")

    with pytest.raises(HistoryIntegrityError, match="chain gap at run 3"):
        history.recent("transactions")


def test_legacy_history_is_migrated_into_a_verified_chain(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    result = _result(1)
    report_json = json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE quality_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset TEXT NOT NULL,
                passed INTEGER NOT NULL,
                rows INTEGER NOT NULL,
                columns INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO quality_runs(dataset, passed, rows, columns, report_json, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                result.dataset,
                int(result.passed),
                result.rows,
                result.columns,
                report_json,
                result.checked_at,
            ),
        )

    rows = QualityHistory(database).recent("transactions")

    assert len(rows) == 1
    assert rows[0]["previous_chain_sha256"] is None
    assert len(rows[0]["chain_sha256"]) == 64


def test_history_cli_fails_closed_on_corruption(tmp_path: Path, capsys) -> None:
    database = tmp_path / "history.db"
    history = QualityHistory(database)
    history.record(_result(1))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE quality_runs SET chain_sha256 = 'forged' WHERE id = 1")

    code = main(["history", "--database", str(database), "--dataset", "transactions"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is False
    assert "chain mismatch" in payload["error"]
