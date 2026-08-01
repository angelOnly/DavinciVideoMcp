"""数据库驱动的最小任务队列，不引入额外消息服务。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from davinci_app.common import json_loads_or_default, utc_now
from davinci_app.persistence import ProductDatabase


class ResolveWriterLeaseUnavailable(RuntimeError):
    """同一 Resolve 实例已有活动写入者。"""


@dataclass(frozen=True)
class ClaimedTask:
    id: str
    run_id: str
    task_type: str
    payload: dict[str, Any]
    attempt_count: int


class TaskQueue:
    def __init__(self, database: ProductDatabase) -> None:
        self.database = database

    def claim(self, worker_id: str, *, lease_seconds: int = 45) -> ClaimedTask | None:
        now = _now()
        expires = _format_time(now + timedelta(seconds=lease_seconds))
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY created_at
                LIMIT 1
                """,
                (_format_time(now),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE tasks
                SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, expires, utc_now(), row["id"]),
            )
            updated = connection.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return ClaimedTask(
            id=updated["id"],
            run_id=updated["run_id"],
            task_type=updated["task_type"],
            payload=json_loads_or_default(updated["payload_json"], {}),
            attempt_count=updated["attempt_count"],
        )

    def heartbeat(self, task_id: str, worker_id: str, *, lease_seconds: int = 45) -> bool:
        expires = _format_time(_now() + timedelta(seconds=lease_seconds))
        with self.database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE tasks SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (expires, utc_now(), task_id, worker_id),
            ).rowcount
        return updated == 1

    def complete(self, task_id: str, worker_id: str) -> None:
        self._finish(task_id, worker_id, "succeeded", None)

    def requeue(self, task_id: str, worker_id: str, reason: dict[str, Any]) -> None:
        with self.database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE tasks SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    last_error_json = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (json.dumps(reason, ensure_ascii=False), utc_now(), task_id, worker_id),
            ).rowcount
        if updated != 1:
            raise RuntimeError("任务租约已失效，不能重新排队。")

    def fail(self, task_id: str, worker_id: str, error: dict[str, Any], *, outcome_unknown: bool = False) -> None:
        self._finish(task_id, worker_id, "outcome_unknown" if outcome_unknown else "failed", error)

    def record_step(self, task_id: str, step_name: str, status: str, detail: dict[str, Any] | None = None) -> None:
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO task_steps(task_id, step_name, status, detail_json, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step_name,
                    status,
                    json.dumps(detail or {}, ensure_ascii=False),
                    now,
                    now if status in {"succeeded", "failed", "outcome_unknown"} else None,
                ),
            )

    def acquire_resolve_writer(self, worker_id: str, *, lease_seconds: int = 60) -> None:
        now = _now()
        now_text = _format_time(now)
        expires = _format_time(now + timedelta(seconds=lease_seconds))
        with self.database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT owner, expires_at FROM resolve_writer_lease WHERE lease_key = 'resolve'"
            ).fetchone()
            if current and current["owner"] != worker_id and current["expires_at"] >= now_text:
                raise ResolveWriterLeaseUnavailable("另一名 Worker 正在写入 Resolve，当前任务将等待下一次领取。")
            connection.execute(
                """
                INSERT INTO resolve_writer_lease(lease_key, owner, expires_at, updated_at)
                VALUES ('resolve', ?, ?, ?)
                ON CONFLICT(lease_key) DO UPDATE SET owner = excluded.owner,
                    expires_at = excluded.expires_at, updated_at = excluded.updated_at
                """,
                (worker_id, expires, utc_now()),
            )

    def release_resolve_writer(self, worker_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM resolve_writer_lease WHERE lease_key = 'resolve' AND owner = ?", (worker_id,)
            )

    def _finish(self, task_id: str, worker_id: str, status: str, error: dict[str, Any] | None) -> None:
        with self.database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL, last_error_json = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (status, json.dumps(error, ensure_ascii=False) if error else None, utc_now(), task_id, worker_id),
            ).rowcount
        if updated != 1:
            raise RuntimeError("任务租约已失效，不能写入完成状态。")


def new_worker_id() -> str:
    return f"worker-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
