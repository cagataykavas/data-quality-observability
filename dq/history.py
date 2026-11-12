from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import deque
from pathlib import Path

from dq.contracts import ContractResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    passed INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    columns INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    previous_chain_sha256 TEXT,
    chain_sha256 TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_runs_dataset_id
    ON quality_runs(dataset, id DESC);
"""

CHAIN_SCHEMA = "quality-history-chain/v1"


class HistoryIntegrityError(RuntimeError):
    """Raised when persisted quality evidence fails integrity verification."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _chain_digest(
    *,
    dataset: str,
    checked_at: str,
    report_sha256: str,
    previous_chain_sha256: str | None,
) -> str:
    payload = json.dumps(
        {
            "checked_at": checked_at,
            "dataset": dataset,
            "previous_chain_sha256": previous_chain_sha256,
            "report_sha256": report_sha256,
            "schema": CHAIN_SCHEMA,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(payload)


class QualityHistory:
    def __init__(self, database_path: str | Path = "quality_history.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_integrity_columns(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate_integrity_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(quality_runs)").fetchall()
        }
        definitions = {
            "report_sha256": "TEXT",
            "previous_chain_sha256": "TEXT",
            "chain_sha256": "TEXT",
        }
        missing = [name for name in definitions if name not in columns]
        if not missing:
            return

        for name in missing:
            connection.execute(f"ALTER TABLE quality_runs ADD COLUMN {name} {definitions[name]}")

        previous_by_dataset: dict[str, str] = {}
        rows = connection.execute(
            "SELECT id, dataset, report_json, checked_at FROM quality_runs ORDER BY id"
        ).fetchall()
        for row in rows:
            report_sha256 = _digest(str(row["report_json"]))
            previous = previous_by_dataset.get(str(row["dataset"]))
            chain_sha256 = _chain_digest(
                dataset=str(row["dataset"]),
                checked_at=str(row["checked_at"]),
                report_sha256=report_sha256,
                previous_chain_sha256=previous,
            )
            connection.execute(
                "UPDATE quality_runs SET report_sha256 = ?, previous_chain_sha256 = ?, "
                "chain_sha256 = ? WHERE id = ?",
                (report_sha256, previous, chain_sha256, row["id"]),
            )
            previous_by_dataset[str(row["dataset"])] = chain_sha256

    def record(self, result: ContractResult) -> int:
        payload = json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":"))
        report_sha256 = _digest(payload)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous_row = connection.execute(
                "SELECT chain_sha256 FROM quality_runs WHERE dataset = ? ORDER BY id DESC LIMIT 1",
                (result.dataset,),
            ).fetchone()
            previous = str(previous_row["chain_sha256"]) if previous_row else None
            chain_sha256 = _chain_digest(
                dataset=result.dataset,
                checked_at=result.checked_at,
                report_sha256=report_sha256,
                previous_chain_sha256=previous,
            )
            cursor = connection.execute(
                """
                INSERT INTO quality_runs(
                    dataset, passed, rows, columns, report_json, report_sha256,
                    previous_chain_sha256, chain_sha256, checked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.dataset,
                    int(result.passed),
                    result.rows,
                    result.columns,
                    payload,
                    report_sha256,
                    previous,
                    chain_sha256,
                    result.checked_at,
                ),
            )
            return int(cursor.lastrowid)

    def recent(self, dataset: str, limit: int = 20) -> list[dict]:
        bounded_limit = max(1, min(limit, 200))
        verified: deque[dict] = deque(maxlen=bounded_limit)
        previous: str | None = None
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, dataset, report_json, report_sha256, previous_chain_sha256,
                       chain_sha256, checked_at
                FROM quality_runs
                WHERE dataset = ?
                ORDER BY id
                """,
                (dataset,),
            )
            for row in rows:
                report_json = str(row["report_json"])
                report_sha256 = _digest(report_json)
                if row["report_sha256"] != report_sha256:
                    raise HistoryIntegrityError(
                        f"quality history report hash mismatch at run {row['id']}"
                    )
                if row["previous_chain_sha256"] != previous:
                    raise HistoryIntegrityError(f"quality history chain gap at run {row['id']}")
                expected_chain = _chain_digest(
                    dataset=str(row["dataset"]),
                    checked_at=str(row["checked_at"]),
                    report_sha256=report_sha256,
                    previous_chain_sha256=previous,
                )
                if row["chain_sha256"] != expected_chain:
                    raise HistoryIntegrityError(
                        f"quality history chain mismatch at run {row['id']}"
                    )
                try:
                    payload = json.loads(report_json)
                except json.JSONDecodeError as exc:
                    raise HistoryIntegrityError(
                        f"quality history report is invalid JSON at run {row['id']}"
                    ) from exc
                payload["run_id"] = int(row["id"])
                payload["report_sha256"] = report_sha256
                payload["previous_chain_sha256"] = previous
                payload["chain_sha256"] = expected_chain
                verified.append(payload)
                previous = expected_chain

        return list(reversed(verified))
