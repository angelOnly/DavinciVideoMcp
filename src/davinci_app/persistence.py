"""产品业务事实的 SQLite 仓储；媒体文件始终只保存路径和哈希。"""

from __future__ import annotations

import json
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
                    codex_thread_id TEXT,
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

                -- 用户可见 VideoVersion 之外的受管渲染，例如 Engine 冒烟预览、
                -- 内部工作版和候选渲染准备文件，绝不自动出现在审核页。
                CREATE TABLE IF NOT EXISTS video_artifacts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    output_path TEXT NOT NULL UNIQUE,
                    output_hash TEXT NOT NULL,
                    plan_digest TEXT,
                    finishing_digest TEXT,
                    verification_json TEXT NOT NULL,
                    source_video_version_id TEXT REFERENCES video_versions(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, artifact_type)
                );
                CREATE INDEX IF NOT EXISTS idx_video_artifacts_project ON video_artifacts(project_id, created_at);

                -- 专业工作流的每个小型、可审计产物只保存结构化事实和文件引用；
                -- 大型媒体仍只保存在项目目录中。
                CREATE TABLE IF NOT EXISTS run_artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    artifact_path TEXT,
                    content_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, artifact_type)
                );
                CREATE INDEX IF NOT EXISTS idx_run_artifacts_run ON run_artifacts(run_id, artifact_type);

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
            project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
            if "codex_thread_id" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN codex_thread_id TEXT")
            self._migrate_legacy_test_candidates(connection)

    @staticmethod
    def _migrate_legacy_test_candidates(connection: sqlite3.Connection) -> None:
        """保留历史文件与记录，但撤销错误的“成片候选”业务身份。"""
        rows = connection.execute(
            """
            SELECT versions.*, runs.input_snapshot_json
            FROM video_versions AS versions
            JOIN runs ON runs.id = versions.run_id
            WHERE versions.state = 'candidate'
            """
        ).fetchall()
        for row in rows:
            try:
                snapshot = json.loads(row["input_snapshot_json"])
                preset = (snapshot.get("brief") or {}).get("testing_preset")
            except (TypeError, ValueError, json.JSONDecodeError):
                preset = None
            if not preset:
                continue
            now = row["created_at"]
            connection.execute(
                """
                INSERT OR IGNORE INTO video_artifacts(
                    id, project_id, run_id, artifact_type, state, output_path, output_hash,
                    plan_digest, finishing_digest, verification_json, source_video_version_id, created_at
                ) VALUES (?, ?, ?, 'technical_preview', 'verified', ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    f"legacy-preview-{row['id']}",
                    row["project_id"],
                    row["run_id"],
                    row["output_path"],
                    row["output_hash"],
                    row["plan_digest"],
                    json.dumps(
                        {
                            "migration": "历史 TestCutCompiler 渲染迁移为内部技术预览；未经过专业候选门禁。"
                        },
                        ensure_ascii=False,
                    ),
                    row["id"],
                    now,
                ),
            )
            connection.execute(
                # 保留历史行以便审计，但腾出 v1 等用户可见候选序号。
                "UPDATE video_versions SET state = 'technical_preview_migrated', version_number = ? WHERE id = ?",
                (-int(row["version_number"]), row["id"]),
            )
            connection.execute("UPDATE runs SET kind = 'engine_smoke' WHERE id = ?", (row["run_id"],))
            has_candidate = connection.execute(
                "SELECT 1 FROM video_versions WHERE project_id = ? AND state = 'candidate' LIMIT 1",
                (row["project_id"],),
            ).fetchone()
            if not has_candidate:
                connection.execute(
                    "UPDATE projects SET status = 'technical_preview_available', updated_at = ? WHERE id = ?",
                    (now, row["project_id"]),
                )

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
