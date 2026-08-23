from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import threading

from dq.contracts import ContractResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    passed INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    columns INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_runs_dataset_id
    ON quality_runs(dataset, id DESC);
"""


class QualityHistory:
    def __init__(self, database_path: str | Path = "quality_history.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, result: ContractResult) -> int:
        payload = json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO quality_runs(dataset, passed, rows, columns, report_json, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.dataset,
                    int(result.passed),
                    result.rows,
                    result.columns,
                    payload,
                    result.checked_at,
                ),
            )
            return int(cursor.lastrowid)

    def recent(self, dataset: str, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, report_json
                FROM quality_runs
                WHERE dataset = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (dataset, max(1, min(limit, 200))),
            ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["report_json"])
            payload["run_id"] = int(row["id"])
            result.append(payload)
        return result
