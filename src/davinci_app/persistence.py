"""产品业务事实的 SQLite 仓储；媒体文件始终只保存路径和哈希。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ProductDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    brief_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    staging_path TEXT,
                    source_path TEXT,
                    working_path TEXT,
                    content_hash TEXT,
                    working_hash TEXT,
                    probe_json TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id);

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    input_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    task_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(status, lease_expires_at, created_at);

                CREATE TABLE IF NOT EXISTS task_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id, id);

                CREATE TABLE IF NOT EXISTS resolve_writer_lease (
                    lease_key TEXT PRIMARY KEY CHECK (lease_key = 'resolve'),
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS video_versions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
                    version_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    output_path TEXT NOT NULL UNIQUE,
                    output_hash TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, version_number)
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    video_version_id TEXT NOT NULL REFERENCES video_versions(id) ON DELETE CASCADE,
                    timecode_start REAL,
                    timecode_end REAL,
                    body TEXT NOT NULL,
                    protect_outside_range INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            if "working_hash" not in columns:
                connection.execute("ALTER TABLE assets ADD COLUMN working_hash TEXT")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
