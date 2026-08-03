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
from davinci_app.project.candidate_gate import CandidatePublishGateError, verify_candidate_publishable


VIDEO_ARTIFACT_TYPES = {"technical_preview", "work_preview", "candidate_render"}


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
        if kind not in {"initial_edit", "engine_smoke"}:
            raise ProjectStateError("本次只支持 initial_edit 或 engine_smoke 运行类型。")
        project = self._require_project(project_id)
        testing_preset = (project.get("brief") or {}).get("testing_preset")
        if kind == "engine_smoke" and not testing_preset:
            raise ProjectStateError("Engine 冒烟测试必须使用明确的 testing_preset。")
        if kind == "initial_edit" and testing_preset:
            raise ProjectStateError("带 testing_preset 的项目只能运行 Engine 冒烟测试，不能进入正式候选链路。")
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
        task_type = "engine_smoke" if kind == "engine_smoke" else "build_candidate"
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
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (task_id, run_id, task_type, json.dumps({"run_id": run_id}, ensure_ascii=False), now, now),
            )
            connection.execute(
                "UPDATE projects SET status = 'running', updated_at = ? WHERE id = ?",
                (now, project_id),
            )
        return self.get_run(run_id)

    def publish_video_version(self, run_id: str, candidate_render_id: str) -> dict[str, Any]:
        """只有完整专业证据链通过后，才把候选渲染升级为用户可见版本。"""
        run = self.get_run(run_id)
        if run["kind"] != "initial_edit":
            raise ProjectStateError("Engine 冒烟测试和内部技术预览绝不能发布为成片候选。")
        project_id = run["project_id"]
        project_directories = self._project_directories(project_id)
        artifacts = self.list_video_artifacts(project_id, run_id=run_id)
        try:
            proof = verify_candidate_publishable(self.list_run_artifacts(run_id), artifacts, candidate_render_id)
        except CandidatePublishGateError as exc:
            raise ProjectStateError(f"候选发布被门禁阻止：{exc}") from exc
        candidate_render = next(item for item in artifacts if item["id"] == candidate_render_id)
        work_preview = next(item for item in artifacts if item["artifact_type"] == "work_preview")
        work_path = ensure_within(Path(work_preview["output_path"]), project_directories["root"])
        if (
            not work_path.exists()
            or work_path.stat().st_size == 0
            or sha256_file(work_path) != work_preview["output_hash"]
        ):
            raise ProjectStateError("内部工作版在复核后发生变化或丢失，不能发布候选。")
        render_path = ensure_within(Path(candidate_render["output_path"]), project_directories["root"])
        if not render_path.exists() or render_path.stat().st_size == 0:
            raise ProjectStateError("候选渲染文件不存在或为空，不能发布版本。")
        if sha256_file(render_path) != candidate_render["output_hash"]:
            raise ProjectStateError("候选渲染在验证后发生变化，不能发布版本。")
        with self.database.transaction(immediate=True) as connection:
            next_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 FROM video_versions WHERE project_id = ? AND state = 'candidate'",
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
            if sha256_file(candidate_path) != candidate_render["output_hash"]:
                candidate_path.unlink(missing_ok=True)
                raise ProjectStateError("候选文件复制后的内容哈希不一致，已拒绝发布。")
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
                    proof.plan_digest,
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
        result["artifacts"] = self.list_video_artifacts(project_id)
        return result

    def project_paths(self, project_id: str) -> dict[str, Path]:
        """返回已创建项目的受管目录，供 Workflow 写入证据、计划和渲染。"""
        self._require_project(project_id)
        return self._project_directories(project_id)

    def get_codex_thread_id(self, project_id: str) -> str | None:
        """Codex Thread 只是一项项目引用，绝不替代项目、运行或版本业务状态。"""
        with self.database.connection() as connection:
            row = connection.execute("SELECT codex_thread_id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ProjectStateError("项目不存在。")
        return str(row["codex_thread_id"]) if row["codex_thread_id"] else None

    def record_codex_thread_id(self, project_id: str, proposed_thread_id: str) -> str:
        """并发情况下保留第一个成功写入的 Thread，调用方应恢复其返回值。"""
        if not proposed_thread_id:
            raise ProjectStateError("Codex Thread ID 不能为空。")
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT codex_thread_id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise ProjectStateError("项目不存在。")
            existing = row["codex_thread_id"]
            if existing:
                return str(existing)
            connection.execute(
                "UPDATE projects SET codex_thread_id = ?, updated_at = ? WHERE id = ?",
                (proposed_thread_id, utc_now(), project_id),
            )
        return proposed_thread_id

    def mark_run_running(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                (utc_now(), run_id),
            )

    def mark_run_waiting(self, run_id: str, detail: dict[str, Any]) -> None:
        """外部专业能力缺失时保留输入与检查点，不能伪造降级候选。"""
        run = self.get_run(run_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE runs SET status = 'waiting_user', failure_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(detail, ensure_ascii=False), utc_now(), run_id),
            )
            connection.execute(
                "UPDATE projects SET status = 'waiting_user', updated_at = ? WHERE id = ?",
                (utc_now(), run["project_id"]),
            )

    def mark_engine_smoke_succeeded(self, run_id: str) -> None:
        """技术预览成功只更新内部状态，绝不创建 VideoVersion。"""
        run = self.get_run(run_id)
        if run["kind"] != "engine_smoke":
            raise ProjectStateError("只有 engine_smoke 运行可以标记为技术预览完成。")
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            artifact = connection.execute(
                "SELECT 1 FROM video_artifacts WHERE run_id = ? AND artifact_type = 'technical_preview' AND state = 'verified'",
                (run_id,),
            ).fetchone()
            if artifact is None:
                raise ProjectStateError("没有已验证的技术预览，不能完成 Engine 冒烟运行。")
            connection.execute("UPDATE runs SET status = 'succeeded', updated_at = ? WHERE id = ?", (now, run_id))
            connection.execute(
                "UPDATE projects SET status = 'technical_preview_available', updated_at = ? WHERE id = ?",
                (now, run["project_id"]),
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

    def record_run_artifact(
        self,
        run_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        *,
        state: str = "succeeded",
        artifact_path: Path | None = None,
    ) -> dict[str, Any]:
        """记录专业链路的小型结构化产物；媒体始终只保留受管路径。"""
        run = self.get_run(run_id)
        project_root = self._project_directories(run["project_id"])["root"]
        stored_path = None
        content_hash = None
        if artifact_path is not None:
            safe_path = ensure_within(artifact_path, project_root)
            if not safe_path.exists() or not safe_path.is_file():
                raise ProjectStateError("专业产物文件不存在，不能记录为完成。")
            stored_path = str(safe_path)
            content_hash = sha256_file(safe_path)
        now = utc_now()
        artifact_id = uuid.uuid4().hex
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO run_artifacts(
                    id, run_id, artifact_type, state, payload_json, artifact_path, content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, artifact_type) DO UPDATE SET
                    state = excluded.state, payload_json = excluded.payload_json, artifact_path = excluded.artifact_path,
                    content_hash = excluded.content_hash, updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    run_id,
                    artifact_type,
                    state,
                    json.dumps(payload, ensure_ascii=False),
                    stored_path,
                    content_hash,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM run_artifacts WHERE run_id = ? AND artifact_type = ?", (run_id, artifact_type)
            ).fetchone()
        assert row is not None
        return self._run_artifact_row(row)

    def list_run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM run_artifacts WHERE run_id = ? ORDER BY created_at", (run_id,)
            ).fetchall()
        return [self._run_artifact_row(row) for row in rows]

    def record_video_artifact(
        self,
        run_id: str,
        artifact_type: str,
        render_path: Path,
        *,
        plan_digest: str | None,
        verification: dict[str, Any],
        finishing_digest: str | None = None,
    ) -> dict[str, Any]:
        """记录非用户可见的受管渲染，不能借此绕过候选发布门禁。"""
        if artifact_type not in VIDEO_ARTIFACT_TYPES:
            raise ProjectStateError(f"不支持的视频产物类型：{artifact_type}")
        run = self.get_run(run_id)
        project_id = run["project_id"]
        if run["kind"] == "engine_smoke" and artifact_type != "technical_preview":
            raise ProjectStateError("Engine 冒烟运行只能记录 technical_preview。")
        if run["kind"] == "initial_edit" and artifact_type == "technical_preview":
            raise ProjectStateError("正式剪辑运行不能把渲染降格或伪装为技术预览。")
        if artifact_type == "work_preview" and not plan_digest:
            raise ProjectStateError("内部工作版必须绑定 EditPlan 摘要。")
        if artifact_type == "candidate_render" and (not plan_digest or not finishing_digest):
            raise ProjectStateError("候选渲染必须同时绑定 EditPlan 与收尾方案摘要。")
        project_root = self._project_directories(project_id)["root"]
        safe_path = ensure_within(render_path, project_root)
        if not safe_path.exists() or safe_path.stat().st_size == 0:
            raise ProjectStateError("内部渲染文件不存在或为空。")
        state = "verified" if verification.get("valid") is True else "invalid"
        artifact_id = uuid.uuid4().hex
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM video_artifacts WHERE run_id = ? AND artifact_type = ?", (run_id, artifact_type)
            ).fetchone()
            if existing:
                raise ProjectStateError(f"该运行已经存在 {artifact_type}，拒绝覆盖。")
            connection.execute(
                """
                INSERT INTO video_artifacts(
                    id, project_id, run_id, artifact_type, state, output_path, output_hash,
                    plan_digest, finishing_digest, verification_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    run_id,
                    artifact_type,
                    state,
                    str(safe_path),
                    sha256_file(safe_path),
                    plan_digest,
                    finishing_digest,
                    json.dumps(verification, ensure_ascii=False),
                    now,
                ),
            )
        return self.get_video_artifact(artifact_id)

    def get_video_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM video_artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise ProjectStateError("内部视频产物不存在。")
        return self._video_artifact_row(row)

    def list_video_artifacts(self, project_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            if run_id:
                rows = connection.execute(
                    "SELECT * FROM video_artifacts WHERE project_id = ? AND run_id = ? ORDER BY created_at",
                    (project_id, run_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM video_artifacts WHERE project_id = ? ORDER BY created_at", (project_id,)
                ).fetchall()
        return [self._video_artifact_row(row) for row in rows]

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
            row = connection.execute(
                "SELECT * FROM video_versions WHERE id = ? AND state = 'candidate'", (version_id,)
            ).fetchone()
        if row is None:
            raise ProjectStateError("视频版本不存在。")
        return dict(row)

    def list_video_versions(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM video_versions WHERE project_id = ? AND state = 'candidate' ORDER BY version_number",
                (project_id,),
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
    def _run_artifact_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json_loads_or_default(result.pop("payload_json"), {})
        return result

    @staticmethod
    def _video_artifact_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["verification"] = json_loads_or_default(result.pop("verification_json"), {})
        return result

    @staticmethod
    def _project_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["brief"] = json_loads_or_default(result.pop("brief_json"), {})
        return result
