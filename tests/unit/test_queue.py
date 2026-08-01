from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from davinci_app.common import utc_now
from davinci_app.persistence import ProductDatabase
from davinci_app.workflow.queue import TaskQueue


class TaskQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = ProductDatabase(Path(self.temp.name) / "product.db")
        self.database.initialize()
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                ("project", "测试", "{}", "draft", now, now),
            )
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("run", "project", "{}", "queued", "initial_edit", now, now, None),
            )
            connection.execute(
                """
                INSERT INTO tasks(id, run_id, task_type, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                ("task", "run", "build_candidate", json.dumps({"run_id": "run"}), now, now),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_is_persisted_before_worker_claims_it(self) -> None:
        queue = TaskQueue(self.database)
        claimed = queue.claim("worker-a")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual("task", claimed.id)
        queue.heartbeat(claimed.id, "worker-a")
        queue.complete(claimed.id, "worker-a")
        with self.database.connection() as connection:
            row = connection.execute("SELECT status, lease_owner FROM tasks WHERE id = 'task'").fetchone()
        self.assertEqual("succeeded", row["status"])
        self.assertIsNone(row["lease_owner"])

    def test_only_one_resolve_writer_can_hold_active_lease(self) -> None:
        queue = TaskQueue(self.database)
        queue.acquire_resolve_writer("worker-a")
        with self.assertRaisesRegex(Exception, "另一名 Worker"):
            queue.acquire_resolve_writer("worker-b")
        queue.release_resolve_writer("worker-a")
        queue.acquire_resolve_writer("worker-b")

