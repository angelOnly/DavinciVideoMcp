"""Project Application Service：冻结输入和发布版本只能经过本服务。"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from davinci_app.common import ensure_within, json_loads_or_default, safe_filename, sha256_file, utc_now
from davinci_app.config import AppConfig
from davinci_app.media.validation import UploadValidator
from davinci_app.persistence import ProductDatabase


class ProjectStateError(RuntimeError):
    """用户输入或当前项目状态不满足业务门禁。"""


class ProjectService:
    def __init__(self, config: AppConfig, database: ProductDatabase, validator: UploadValidator) -> None:
        self.config = config
        self.database = database
        self.validator = validator

    def create_project(self, title: str, brief: dict[str, Any] | None = None) -> dict[str, Any]:
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ProjectStateError("项目名称不能为空。")
        project_id = uuid.uuid4().hex
        now = utc_now()
        self._project_directories(project_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO projects(id, title, brief_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, cleaned_title, json.dumps(brief or {}, ensure_ascii=False), "draft", now, now),
            )
        return self.get_project(project_id)

    def update_brief(self, project_id: str, brief: dict[str, Any]) -> dict[str, Any]:
        self._require_project(project_id)
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE projects SET brief_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(brief, ensure_ascii=False), now, project_id),
            )
        return self.get_project(project_id)

    def import_local_file(self, project_id: str, source: Path, *, role: str = "primary") -> dict[str, Any]:
        """测试和本地单用户导入路径，同样先进入 staging 再做服务端验证。"""
        if not source.exists() or not source.is_file():
            raise ProjectStateError(f"待导入文件不存在：{source}")
        with source.open("rb") as stream:
            return self.receive_upload(project_id, source.name, stream, role=role)

    def receive_upload(
        self, project_id: str, original_name: str, content: BinaryIO, *, role: str = "primary"
    ) -> dict[str, Any]:
        self._require_project(project_id)
        asset_id = uuid.uuid4().hex
        safe_name = safe_filename(original_name)
        directories = self._project_directories(project_id)
        suffix = Path(safe_name).suffix or ".bin"
        staging_path = directories["staging"] / f"{asset_id}{suffix}.upload"
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO assets(id, project_id, original_name, role, state, staging_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'uploading', ?, ?, ?)
                """,
                (asset_id, project_id, safe_name, role, str(staging_path), now, now),
            )
        try:
            with staging_path.open("wb") as target:
                shutil.copyfileobj(content, target, length=1024 * 1024)
        except OSError as exc:
            self._mark_asset_invalid(asset_id, [{"code": "upload_write_failed", "message": f"无法保存上传文件：{exc}"}])
            return self.get_asset(asset_id)
        return self._validate_staged_asset(asset_id)

    def _validate_staged_asset(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        if asset["state"] != "uploading":
            raise ProjectStateError("只有刚完成上传的素材可以进入校验。")
        project_id = asset["project_id"]
        staging_path = Path(asset["staging_path"])
        directories = self._project_directories(project_id)
        suffix = Path(asset["original_name"]).suffix or ".mp4"
        working_path = directories["working"] / f"{asset_id}.working{suffix if suffix.lower() == '.mp4' else '.mp4'}"
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE assets SET state = 'validating', updated_at = ? WHERE id = ?",
                (utc_now(), asset_id),
            )
        result = self.validator.validate(
            staging_path,
            role=asset["role"],
            working_copy_path=working_path,
            full_scan=False,
        )
        if not result.valid:
            self._mark_asset_invalid(asset_id, result.errors, result.probe, result.warnings)
            return self.get_asset(asset_id)

        source_path = directories["source"] / f"{asset_id}{suffix}"
        try:
            staging_path.replace(source_path)
        except OSError as exc:
            self._mark_asset_invalid(
                asset_id,
                [{"code": "source_finalize_failed", "message": f"无法固化已校验源文件：{exc}"}],
                result.probe,
                result.warnings,
            )
            return self.get_asset(asset_id)
        active_path = working_path if result.working_copy_created else source_path
        active_hash = sha256_file(active_path)
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE assets
                SET state = 'ready', staging_path = NULL, source_path = ?, working_path = ?,
                    content_hash = ?, working_hash = ?, probe_json = ?, warnings_json = ?, error_json = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(source_path),
                    str(active_path),
                    result.content_hash,
                    active_hash,
                    json.dumps(result.probe.to_dict() if result.probe else {}, ensure_ascii=False),
                    json.dumps(result.warnings, ensure_ascii=False),
                    now,
                    asset_id,
                ),
            )
        return self.get_asset(asset_id)

    def remove_asset(self, asset_id: str) -> None:
        asset = self.get_asset(asset_id)
        project_root = self._project_directories(asset["project_id"])["root"]
        for key in ("staging_path", "source_path", "working_path"):
            raw_path = asset.get(key)
            if raw_path:
                candidate = ensure_within(Path(raw_path), project_root)
                candidate.unlink(missing_ok=True)
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))

    def freeze_run(
        self, project_id: str, *, asset_ids: Iterable[str] | None = None, kind: str = "initial_edit"
    ) -> dict[str, Any]:
        project = self._require_project(project_id)
        all_assets = self.list_assets(project_id)
        selected = all_assets if asset_ids is None else self._select_assets(all_assets, asset_ids)
        if not selected:
            raise ProjectStateError("至少需要选择一个已验证的主素材。")
        blocking_states = {"uploading", "validating", "invalid"}
        blocked = [asset for asset in selected if asset["state"] in blocking_states]
        if blocked:
            names = "、".join(asset["original_name"] for asset in blocked)
            raise ProjectStateError(f"以下已选素材尚不可提交：{names}")
        if not any(asset["role"] in {"primary", "main", "interview"} for asset in selected):
            raise ProjectStateError("至少需要一个有效主素材或采访素材。")
        for asset in selected:
            source_path = Path(asset["source_path"])
            if not source_path.exists() or sha256_file(source_path) != asset["content_hash"]:
                raise ProjectStateError(f"素材在校验后发生变化或被删除：{asset['original_name']}")
            working_path = Path(asset["working_path"])
            if not working_path.exists() or sha256_file(working_path) != asset["working_hash"]:
                raise ProjectStateError(f"稳定工作副本不可访问：{asset['original_name']}")

        run_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        now = utc_now()
        snapshot = {
            "project_id": project_id,
            "brief": project["brief"],
            "assets": [self._freeze_asset(asset) for asset in selected],
        }
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO runs(id, project_id, input_snapshot_json, status, kind, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (run_id, project_id, json.dumps(snapshot, ensure_ascii=False), kind, now, now),
            )
            connection.execute(
                """
                INSERT INTO tasks(id, run_id, task_type, payload_json, status, created_at, updated_at)
                VALUES (?, ?, 'build_candidate', ?, 'queued', ?, ?)
                """,
                (task_id, run_id, json.dumps({"run_id": run_id}, ensure_ascii=False), now, now),
            )
            connection.execute(
                "UPDATE projects SET status = 'running', updated_at = ? WHERE id = ?",
                (now, project_id),
            )
        return self.get_run(run_id)

    def publish_video_version(self, run_id: str, render_path: Path, plan_digest: str) -> dict[str, Any]:
        """发布操作只复制新文件，绝不覆盖已经可见的候选版本。"""
        run = self.get_run(run_id)
        project_id = run["project_id"]
        project_directories = self._project_directories(project_id)
        render_path = ensure_within(render_path, project_directories["root"])
        if not render_path.exists() or render_path.stat().st_size == 0:
            raise ProjectStateError("渲染文件不存在或为空，不能发布版本。")
        with self.database.transaction(immediate=True) as connection:
            next_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 FROM video_versions WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )
            version_id = uuid.uuid4().hex
            candidate_path = project_directories["renders"] / f"candidate-v{next_number}.mp4"
            if candidate_path.exists():
                raise ProjectStateError("目标版本文件已存在，拒绝覆盖。")
            temporary_path = candidate_path.with_suffix(".mp4.partial")
            shutil.copy2(render_path, temporary_path)
            temporary_path.replace(candidate_path)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO video_versions(
                    id, project_id, run_id, version_number, state, output_path, output_hash, plan_digest, created_at
                ) VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    run_id,
                    next_number,
                    str(candidate_path),
                    sha256_file(candidate_path),
                    plan_digest,
                    now,
                ),
            )
            connection.execute(
                "UPDATE runs SET status = 'succeeded', updated_at = ? WHERE id = ?",
                (now, run_id),
            )
            connection.execute(
                "UPDATE projects SET status = 'ready_for_review', updated_at = ? WHERE id = ?",
                (now, project_id),
            )
        return self.get_video_version(version_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ProjectStateError("项目不存在。")
        result = dict(row)
        result["brief"] = json_loads_or_default(result.pop("brief_json"), {})
        result["assets"] = self.list_assets(project_id)
        result["versions"] = self.list_video_versions(project_id)
        return result

    def project_paths(self, project_id: str) -> dict[str, Path]:
        """返回已创建项目的受管目录，供 Workflow 写入证据、计划和渲染。"""
        self._require_project(project_id)
        return self._project_directories(project_id)

    def mark_run_running(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                (utc_now(), run_id),
            )

    def list_projects(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [self._project_row(row) for row in rows]

    def list_assets(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM assets WHERE project_id = ? ORDER BY created_at", (project_id,)
            ).fetchall()
        return [self._asset_row(row) for row in rows]

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise ProjectStateError("素材不存在。")
        return self._asset_row(row)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ProjectStateError("运行记录不存在。")
        result = dict(row)
        result["input_snapshot"] = json_loads_or_default(result.pop("input_snapshot_json"), {})
        result["failure"] = json_loads_or_default(result.pop("failure_json"), None)
        return result

    def get_video_version(self, version_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM video_versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise ProjectStateError("视频版本不存在。")
        return dict(row)

    def list_video_versions(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM video_versions WHERE project_id = ? ORDER BY version_number", (project_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_run_failure(self, run_id: str, failure: dict[str, Any], *, outcome_unknown: bool = False) -> None:
        status = "outcome_unknown" if outcome_unknown else "failed"
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET status = ?, failure_json = ?, updated_at = ? WHERE id = ?",
                (status, json.dumps(failure, ensure_ascii=False), utc_now(), run_id),
            )

    def _require_project(self, project_id: str) -> dict[str, Any]:
        return self.get_project(project_id)

    def _project_directories(self, project_id: str) -> dict[str, Path]:
        root = self.config.projects_root / project_id
        directories = {
            "root": root,
            "staging": root / "staging",
            "source": root / "source",
            "working": root / "working",
            "proxy": root / "proxy",
            "evidence": root / "evidence",
            "plans": root / "plans",
            "renders": root / "renders",
            "delivery": root / "delivery",
        }
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        return directories

    @staticmethod
    def _select_assets(all_assets: list[dict[str, Any]], asset_ids: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(asset_ids)
        selected = [asset for asset in all_assets if asset["id"] in wanted]
        if selected and len(selected) != len(wanted):
            raise ProjectStateError("提交中包含不存在或不属于该项目的素材。")
        return selected

    @staticmethod
    def _freeze_asset(asset: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": asset["id"],
            "role": asset["role"],
            "original_name": asset["original_name"],
            "content_hash": asset["content_hash"],
            "working_hash": asset["working_hash"],
            "source_path": asset["source_path"],
            "working_path": asset["working_path"],
            "probe": asset["probe"],
        }

    def _mark_asset_invalid(
        self,
        asset_id: str,
        errors: list[dict[str, Any]],
        probe: Any = None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE assets SET state = 'invalid', probe_json = ?, warnings_json = ?, error_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(probe.to_dict() if probe else {}, ensure_ascii=False),
                    json.dumps(warnings or [], ensure_ascii=False),
                    json.dumps(errors, ensure_ascii=False),
                    utc_now(),
                    asset_id,
                ),
            )

    @staticmethod
    def _asset_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["probe"] = json_loads_or_default(result.pop("probe_json"), {})
        result["warnings"] = json_loads_or_default(result.pop("warnings_json"), [])
        result["errors"] = json_loads_or_default(result.pop("error_json"), [])
        return result

    @staticmethod
    def _project_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["brief"] = json_loads_or_default(result.pop("brief_json"), {})
        return result
