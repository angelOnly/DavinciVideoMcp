"""认证创意能力的可重建目录、检索与 CapabilityBinding。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from davinci_app.common import digest_json, sha256_file
from davinci_app.creative.localization import AssetLocalizationError, localize_verified_asset


CERTIFICATION_STEPS = ("discover", "deploy", "execute", "readback", "render")
CAPABILITY_STATES = {"testing", "certified", "manual_only", "unsupported"}


class CapabilityBindingUnavailable(RuntimeError):
    """计划请求的能力未认证、无法本地化或不满足五步合同。"""


class CapabilityRegistrationError(ValueError):
    """管理员登记的 Catalog 元数据不满足受管能力边界。"""


class CreativeCatalog:
    """SQLite + FTS5 认证目录。

    数据库只保存元数据、认证证据和文件引用；文件始终留在认证素材库或受管缓存中。
    """

    def __init__(self, database_path: Path, certified_root: Path, cache_root: Path) -> None:
        self.database_path = database_path.resolve()
        self.certified_root = certified_root.resolve()
        self.cache_root = cache_root.resolve()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        """初始化并向后兼容旧的极简 Catalog 表。"""

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capabilities (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    state TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    mechanism TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    preview_path TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    constraints_json TEXT NOT NULL,
                    certification_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_capabilities_state_category
                    ON capabilities(state, category);
                CREATE INDEX IF NOT EXISTS idx_capabilities_hash
                    ON capabilities(content_hash);
                CREATE VIRTUAL TABLE IF NOT EXISTS capability_search
                    USING fts5(capability_id UNINDEXED, display_name, category, description);
                """
            )
            # 早期本地数据库可能已创建；只补当前切片实际需要的列，不重建或删除用户目录。
            self._ensure_column(connection, "capabilities", "mechanism", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "capabilities", "description", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "capabilities", "certification_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "capabilities", "updated_at", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """
                INSERT INTO catalog_meta(key, value) VALUES('schema_version', '2')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """
            )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def register(
        self,
        *,
        capability_id: str,
        category: str,
        mechanism: str,
        display_name: str,
        source_path: Path,
        content_hash: str,
        state: str = "testing",
        preview_path: Path | None = None,
        description: str = "",
        constraints: dict[str, Any] | None = None,
        certification: dict[str, Any] | None = None,
    ) -> None:
        """登记一个原子能力；`certified` 必须携带完整五步认证证据。"""

        self.initialize()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", capability_id):
            raise CapabilityRegistrationError("capability_id 只能使用小写字母、数字、点、下划线和连字符。")
        if state not in CAPABILITY_STATES:
            raise CapabilityRegistrationError(f"未知认证状态：{state}")
        if not category.strip() or not mechanism.strip() or not display_name.strip():
            raise CapabilityRegistrationError("类别、机制和展示名称不能为空。")
        source = self._require_path_in_certified_root(source_path, "能力源文件")
        if not source.is_file():
            raise CapabilityRegistrationError("能力源文件不存在。")
        if sha256_file(source) != content_hash:
            raise CapabilityRegistrationError("登记的内容哈希与能力源文件不一致。")
        preview = self._require_path_in_certified_root(preview_path, "预览文件") if preview_path else None
        resolved_constraints = dict(constraints or {})
        resolved_certification = dict(certification or {})
        if state == "certified" and not self._has_complete_certification(resolved_certification):
            raise CapabilityRegistrationError("认证能力必须保存发现、部署、执行、读回和渲染证据。")
        timestamp = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO capabilities(
                    id, category, state, display_name, mechanism, content_hash, source_path,
                    preview_path, description, constraints_json, certification_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    category=excluded.category,
                    state=excluded.state,
                    display_name=excluded.display_name,
                    mechanism=excluded.mechanism,
                    content_hash=excluded.content_hash,
                    source_path=excluded.source_path,
                    preview_path=excluded.preview_path,
                    description=excluded.description,
                    constraints_json=excluded.constraints_json,
                    certification_json=excluded.certification_json,
                    updated_at=excluded.updated_at
                """,
                (
                    capability_id,
                    category,
                    state,
                    display_name,
                    mechanism,
                    content_hash,
                    str(source),
                    str(preview) if preview else None,
                    description,
                    json.dumps(resolved_constraints, ensure_ascii=False, sort_keys=True),
                    json.dumps(resolved_certification, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute("DELETE FROM capability_search WHERE capability_id = ?", (capability_id,))
            connection.execute(
                """
                INSERT INTO capability_search(capability_id, display_name, category, description)
                VALUES(?, ?, ?, ?)
                """,
                (capability_id, display_name, category, f"{mechanism} {description}"),
            )

    def record_certification(self, capability_id: str, certification: dict[str, Any]) -> None:
        """仅在五步实机合同完整成功后把已登记能力提升为 `certified`。"""

        self.initialize()
        if not self._has_complete_certification(certification):
            raise CapabilityRegistrationError("认证证据不完整，不能把能力提升为 certified。")
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE capabilities
                SET state='certified', certification_json=?, updated_at=?
                WHERE id=?
                """,
                (json.dumps(certification, ensure_ascii=False, sort_keys=True), _utc_now(), capability_id),
            )
        if result.rowcount != 1:
            raise CapabilityRegistrationError("不存在待认证的能力，无法记录认证结果。")

    def certified_count(self) -> int:
        self.initialize()
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM capabilities WHERE state = 'certified'").fetchone()[0])

    def search(
        self,
        query: str = "",
        *,
        category: str | None = None,
        limit: int = 10,
        certified_only: bool = True,
    ) -> list[dict[str, Any]]:
        """先按状态和类别硬过滤，再以安全词元做 FTS5 召回。"""

        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        if certified_only:
            clauses.append("c.state = 'certified'")
        if category:
            clauses.append("c.category = ?")
            parameters.append(category)
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)[:32]
        with self._connect() as connection:
            if tokens:
                statement = (
                    "SELECT c.* FROM capability_search "
                    "JOIN capabilities c ON c.id=capability_search.capability_id "
                    "WHERE capability_search MATCH ?"
                )
                parameters = [" ".join(f'\"{token}\"' for token in tokens), *parameters]
            else:
                statement = "SELECT c.* FROM capabilities c"
            if clauses:
                statement += (" AND " if " WHERE " in statement else " WHERE ") + " AND ".join(clauses)
            statement += " ORDER BY c.category, c.display_name LIMIT ?"
            rows = connection.execute(statement, [*parameters, max(1, min(limit, 10))]).fetchall()
        return [self._public_record(row) for row in rows]

    def bind(self, edit_plan: dict[str, Any], *, edit_plan_digest: str) -> dict[str, Any]:
        """冻结选中能力的本地缓存身份，不根据文件名猜测或替代资源。"""

        requests = edit_plan.get("capability_requests") or []
        if not isinstance(requests, list):
            raise CapabilityBindingUnavailable("EditPlan 的 capability_requests 必须是数组。")
        bindings = []
        for request in requests:
            if not isinstance(request, dict) or not isinstance(request.get("capability_id"), str):
                raise CapabilityBindingUnavailable("每项创意能力请求都必须包含已选择的 capability_id。")
            bindings.append(self._bind_one(request))
        result = {"edit_plan_digest": edit_plan_digest, "bindings": bindings}
        result["digest"] = digest_json(result)
        return result

    def _bind_one(self, request: dict[str, Any]) -> dict[str, Any]:
        capability_id = str(request["capability_id"])
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capabilities WHERE id = ? AND state = 'certified'", (capability_id,)
            ).fetchone()
        if row is None:
            raise CapabilityBindingUnavailable(f"能力 {capability_id} 未处于 certified 状态，不能进入无人工作流。")
        try:
            certification = json.loads(str(row["certification_json"]))
            constraints = json.loads(str(row["constraints_json"]))
        except json.JSONDecodeError as exc:
            raise CapabilityBindingUnavailable(f"能力 {capability_id} 的目录元数据损坏。") from exc
        if not isinstance(certification, dict) or not self._has_complete_certification(certification):
            raise CapabilityBindingUnavailable(f"能力 {capability_id} 缺少完整五步认证证据。")
        try:
            localized = localize_verified_asset(
                Path(str(row["source_path"])), str(row["content_hash"]), self.cache_root
            )
        except AssetLocalizationError as exc:
            raise CapabilityBindingUnavailable(f"认证能力 {capability_id} 无法安全本地化：{exc}") from exc
        if not isinstance(constraints, dict):
            raise CapabilityBindingUnavailable(f"能力 {capability_id} 的约束不是对象。")
        return {
            "capability_id": capability_id,
            "category": str(row["category"]),
            "mechanism": str(row["mechanism"]),
            "display_name": str(row["display_name"]),
            "content_hash": localized.content_hash,
            "cache_path": str(localized.local_path),
            "constraints": constraints,
            "certification": certification,
            "localization": localized.to_evidence(),
            "parameters": request.get("parameters") or {},
            "purpose": str(request.get("purpose") or ""),
        }

    def _require_path_in_certified_root(self, value: Path, label: str) -> Path:
        resolved = value.resolve()
        try:
            resolved.relative_to(self.certified_root)
        except ValueError as exc:
            raise CapabilityRegistrationError(f"{label}必须位于认证素材库中。") from exc
        return resolved

    @staticmethod
    def _has_complete_certification(value: dict[str, Any]) -> bool:
        steps = value.get("steps")
        return isinstance(steps, dict) and all(steps.get(step) is True for step in CERTIFICATION_STEPS)

    @staticmethod
    def _public_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "capability_id": str(row["id"]),
            "category": str(row["category"]),
            "state": str(row["state"]),
            "display_name": str(row["display_name"]),
            "mechanism": str(row["mechanism"]),
            "preview_path": row["preview_path"],
            "description": str(row["description"]),
            "constraints": json.loads(str(row["constraints_json"])),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
