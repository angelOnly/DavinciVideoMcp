"""Engine 基础设施 Journal：用于幂等和对账，不存放产品业务状态。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from davinci_engine.common import workspace_root


class EngineJournal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or workspace_root() / "data" / "engine_journal.db"

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    plan_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        result["response"] = json.loads(result.pop("response_json")) if result.get("response_json") else None
        return result

    def create(self, operation_id: str, plan_digest: str, plan: dict[str, Any]) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO operations(operation_id, plan_digest, plan_json, status) VALUES (?, ?, ?, 'sent')",
                (operation_id, plan_digest, json.dumps(plan, ensure_ascii=False, sort_keys=True)),
            )

    def update(self, operation_id: str, status: str, response: dict[str, Any]) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE operations SET status = ?, response_json = ?, updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                (status, json.dumps(response, ensure_ascii=False), operation_id),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

