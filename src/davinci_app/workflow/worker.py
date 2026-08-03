"""Product Workflow Worker：唯一可调用 Engine MCP 写工具的产品进程。"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, Callable

from davinci_app.creative.catalog import CreativeCatalog
from davinci_app.editorial.pipeline import (
    ProfessionalPipeline,
    ProfessionalPipelineBlocked,
    UnavailableProfessionalEvidenceRuntime,
    UnavailableProfessionalSkillRuntime,
)
from davinci_app.execution.compiler import EngineSmokeCompiler
from davinci_app.execution.engine_client import EngineMcpClient, EngineMcpError
from davinci_app.execution.professional_compiler import ProfessionalExecutionCompiler
from davinci_app.media.evidence import EvidenceBuilder
from davinci_app.project.service import ProjectService
from davinci_app.workflow.queue import ClaimedTask, ResolveWriterLeaseUnavailable, TaskQueue, new_worker_id


class OutcomeUnknownError(RuntimeError):
    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(detail.get("message", "Resolve 写入结果未知。"))
        self.detail = detail


class WorkflowWorker:
    def __init__(
        self,
        project_service: ProjectService,
        queue: TaskQueue,
        smoke_compiler: EngineSmokeCompiler | None = None,
        professional_pipeline: ProfessionalPipeline | None = None,
        professional_compiler: ProfessionalExecutionCompiler | None = None,
        evidence_builder: EvidenceBuilder | None = None,
        engine_client_factory: Callable[[], Any] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.project_service = project_service
        self.queue = queue
        self.smoke_compiler = smoke_compiler or EngineSmokeCompiler(project_service.config.resolve_workspace_project)
        self.professional_compiler = professional_compiler or ProfessionalExecutionCompiler(project_service.config)
        if professional_pipeline is None:
            catalog = CreativeCatalog(
                project_service.config.creative_catalog_database,
                project_service.config.creative_certified_root,
                project_service.config.creative_cache_root,
            )
            catalog.initialize()
            professional_pipeline = ProfessionalPipeline(
                UnavailableProfessionalEvidenceRuntime(project_service.config),
                UnavailableProfessionalSkillRuntime(),
                catalog,
            )
        self.professional_pipeline = professional_pipeline
        self.evidence_builder = evidence_builder or EvidenceBuilder()
        self.engine_client_factory = engine_client_factory or (lambda: EngineMcpClient(project_service.config))
        self.worker_id = worker_id or new_worker_id()

    def run_once(self) -> bool:
        task = self.queue.claim(self.worker_id)
        if not task:
            return False
        self.project_service.mark_run_running(task.run_id)
        self.queue.record_step(task.id, "claim_task", "succeeded", {"worker_id": self.worker_id})
        try:
            if task.task_type == "engine_smoke":
                self._run_engine_smoke(task)
            elif task.task_type == "build_candidate":
                self._build_candidate(task)
            else:
                raise RuntimeError(f"不支持的任务类型：{task.task_type}")
        except ProfessionalPipelineBlocked as exc:
            detail = exc.to_detail()
            self.project_service.mark_run_waiting(task.run_id, detail)
            self.queue.record_step(task.id, exc.stage, "waiting_user", detail)
            self.queue.wait(task.id, self.worker_id, detail)
            return True
        except ResolveWriterLeaseUnavailable as exc:
            self.queue.record_step(task.id, "acquire_resolve_writer", "failed", {"message": str(exc)})
            self.queue.requeue(task.id, self.worker_id, {"code": "resolve_writer_busy", "message": str(exc)})
            return True
        except OutcomeUnknownError as exc:
            self.project_service.set_run_failure(task.run_id, exc.detail, outcome_unknown=True)
            self.queue.record_step(task.id, "resolve_operation", "outcome_unknown", exc.detail)
            self.queue.fail(task.id, self.worker_id, exc.detail, outcome_unknown=True)
            return True
        except BaseException as exc:
            detail = {
                "code": "workflow_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            }
            self.project_service.set_run_failure(task.run_id, detail)
            self.queue.record_step(task.id, "workflow", "failed", detail)
            self.queue.fail(task.id, self.worker_id, detail)
            return True
        self.queue.complete(task.id, self.worker_id)
        return True

    def _run_engine_smoke(self, task: ClaimedTask) -> None:
        """Engine 冒烟只证明受管 Resolve 写入与渲染，绝不产生正式剪辑产物。"""
        run = self.project_service.get_run(task.run_id)
        if run["kind"] != "engine_smoke":
            raise RuntimeError("engine_smoke 任务与运行类型不一致。")
        project_paths = self.project_service.project_paths(run["project_id"])
        self.queue.record_step(task.id, "compile_engine_smoke", "running")
        compilation = self.smoke_compiler.compile(run, project_paths)
        self.project_service.record_run_artifact(
            run["id"],
            "engine_smoke_plan",
            {"compiler": "EngineSmokeCompiler", "digest": compilation["plan_digest"]},
            artifact_path=Path(compilation["plan_path"]),
        )
        self.queue.record_step(
            task.id,
            "compile_engine_smoke",
            "succeeded",
            {"plan_path": compilation["plan_path"], "plan_digest": compilation["plan_digest"]},
        )
        output_path, verification = self._execute_and_render(task, run, compilation["plan"], stage="technical_preview")
        preview = self.project_service.record_video_artifact(
            run["id"],
            "technical_preview",
            output_path,
            plan_digest=None,
            verification={**verification, "artifact_classification": "technical_preview"},
        )
        self.project_service.mark_engine_smoke_succeeded(run["id"])
        self.queue.record_step(
            task.id,
            "record_technical_preview",
            "succeeded",
            {"artifact_id": preview["id"], "output_path": preview["output_path"]},
        )

    def _build_candidate(self, task: ClaimedTask) -> None:
        """候选路径必须经过完整专业链路；任何缺口都在进入 Resolve 前停止。"""
        run = self.project_service.get_run(task.run_id)
        if run["kind"] != "initial_edit":
            raise RuntimeError("正式候选任务与运行类型不一致。")
        project_paths = self.project_service.project_paths(run["project_id"])
        deterministic_evidence = self._build_deterministic_evidence(task, run, project_paths)

        def record(artifact_type: str, payload: dict[str, Any]) -> None:
            self.project_service.record_run_artifact(run["id"], artifact_type, payload)

        self.queue.record_step(task.id, "professional_preproduction", "running")
        preproduction = self.professional_pipeline.build_preproduction(
            run, deterministic_evidence, project_paths, record
        )
        self.queue.record_step(
            task.id,
            "professional_preproduction",
            "succeeded",
            {"edit_plan_digest": preproduction.edit_plan_digest},
        )

        work_compilation = self.professional_compiler.compile_work_preview(run, project_paths, preproduction)
        self.project_service.record_run_artifact(
            run["id"],
            "work_preview_execution_plan",
            {"digest": work_compilation["plan_digest"], "edit_plan_digest": preproduction.edit_plan_digest},
            artifact_path=Path(work_compilation["plan_path"]),
        )
        self.queue.record_step(
            task.id,
            "compile_work_preview",
            "succeeded",
            {"plan_path": work_compilation["plan_path"], "plan_digest": work_compilation["plan_digest"]},
        )
        work_path, work_verification = self._execute_and_render(
            task, run, work_compilation["plan"], stage="work_preview"
        )
        work_preview = self.project_service.record_video_artifact(
            run["id"],
            "work_preview",
            work_path,
            plan_digest=preproduction.edit_plan_digest,
            verification=work_verification,
        )
        self.queue.record_step(
            task.id,
            "record_work_preview",
            "succeeded",
            {"artifact_id": work_preview["id"], "output_path": work_preview["output_path"]},
        )

        finishing = self.professional_pipeline.review_work_preview(run, preproduction, work_preview, record)
        candidate_compilation = self.professional_compiler.compile_candidate(
            run, project_paths, preproduction, finishing
        )
        self.project_service.record_run_artifact(
            run["id"],
            "candidate_execution_plan",
            {"digest": candidate_compilation["plan_digest"], "finishing_digest": finishing["digest"]},
            artifact_path=Path(candidate_compilation["plan_path"]),
        )
        self.queue.record_step(
            task.id,
            "compile_candidate",
            "succeeded",
            {"plan_path": candidate_compilation["plan_path"], "plan_digest": candidate_compilation["plan_digest"]},
        )
        candidate_path, candidate_verification = self._execute_and_render(
            task, run, candidate_compilation["plan"], stage="candidate_render"
        )
        candidate_render = self.project_service.record_video_artifact(
            run["id"],
            "candidate_render",
            candidate_path,
            plan_digest=preproduction.edit_plan_digest,
            finishing_digest=finishing["digest"],
            verification=candidate_verification,
        )
        self.queue.record_step(
            task.id,
            "record_candidate_render",
            "succeeded",
            {"artifact_id": candidate_render["id"], "output_path": candidate_render["output_path"]},
        )
        self.professional_pipeline.validate_candidate(candidate_render, candidate_verification, record)
        version = self.project_service.publish_video_version(run["id"], candidate_render["id"])
        self.queue.record_step(
            task.id,
            "publish_video_version",
            "succeeded",
            {"version_id": version["id"], "output_path": version["output_path"]},
        )

    def _build_deterministic_evidence(
        self, task: ClaimedTask, run: dict[str, Any], project_paths: dict[str, Path]
    ) -> list[dict[str, Any]]:
        assets = run["input_snapshot"].get("assets") or []
        self.queue.record_step(task.id, "build_deterministic_evidence", "running")
        evidence = [
            self.evidence_builder.build(asset, project_paths["evidence"], proxy_root=project_paths["proxy"])
            for asset in assets
        ]
        self.project_service.record_run_artifact(
            run["id"],
            "deterministic_evidence",
            {"asset_count": len(evidence), "manifests": [item["manifest_path"] for item in evidence]},
        )
        self.queue.record_step(
            task.id,
            "build_deterministic_evidence",
            "succeeded",
            {"asset_count": len(evidence), "manifests": [item["manifest_path"] for item in evidence]},
        )
        return evidence

    def _execute_and_render(
        self, task: ClaimedTask, run: dict[str, Any], plan: dict[str, Any], *, stage: str
    ) -> tuple[Path, dict[str, Any]]:
        """所有 Resolve 写入走同一受控通道，并在渲染后进行文件级验证。"""
        with self.engine_client_factory() as engine:
            self._ensure_engine_ready(task, engine)
            validation = engine.call("validate_execution_plan", {"plan": plan})
            if validation.get("state") != "succeeded" or not validation.get("validation", {}).get("valid"):
                raise RuntimeError(f"执行计划未通过校验：{validation}")
            self.queue.record_step(task.id, f"{stage}_validate_execution_plan", "succeeded", validation)
            preview = engine.call("preview_execution_plan", {"plan": plan})
            if preview.get("state") != "succeeded":
                raise RuntimeError(f"执行计划预览失败：{preview}")
            plan_digest = preview["plan_digest"]
            self.queue.record_step(task.id, f"{stage}_preview_execution_plan", "succeeded", preview)

            self.queue.acquire_resolve_writer(self.worker_id)
            try:
                execution = engine.call(
                    "execute_execution_plan",
                    {
                        "plan": plan,
                        "operation_id": f"execute-{stage}-{run['id']}",
                        "execution_permit": plan_digest,
                    },
                    timeout_seconds=600,
                )
                self._require_known_success(execution)
                self.queue.record_step(task.id, f"{stage}_execute_execution_plan", "succeeded", execution)
                rendering = engine.call(
                    "render_version",
                    {
                        "plan": plan,
                        "operation_id": f"render-{stage}-{run['id']}",
                        "execution_permit": plan_digest,
                    },
                    timeout_seconds=2400,
                )
                self._require_known_success(rendering)
                self.queue.record_step(task.id, f"{stage}_render_version", "succeeded", rendering)
            finally:
                self.queue.release_resolve_writer(self.worker_id)

            output_path = Path(rendering["output_path"])
            verified = engine.call(
                "verify_render",
                {
                    "path": str(output_path),
                    "expected_duration_seconds": validation["validation"].get("expected_duration_seconds"),
                },
            )
            self._require_known_success(verified)
            verification = verified.get("verification")
            if not isinstance(verification, dict) or verification.get("valid") is not True:
                raise RuntimeError(f"渲染文件没有通过技术验证：{verified}")
            self.queue.record_step(task.id, f"{stage}_verify_render", "succeeded", verified)
        return output_path, verification

    def _ensure_engine_ready(self, task: ClaimedTask, engine: Any) -> None:
        try:
            status = engine.call("engine_status", {}, timeout_seconds=120)
        except EngineMcpError as exc:
            raise RuntimeError(f"无法启动 davinci-engine-mcp：{exc}") from exc
        resolve = status.get("resolve") or {}
        ffmpeg = status.get("ffmpeg") or {}
        if (
            status.get("state") != "succeeded"
            or not ffmpeg.get("available")
            or not resolve.get("connected")
            or not resolve.get("ready_for_execution")
        ):
            raise RuntimeError(f"剪辑任务所需 Engine 能力未就绪：{status}")
        self.queue.record_step(task.id, "engine_status", "succeeded", status)

    @staticmethod
    def _require_known_success(result: dict[str, Any]) -> None:
        if result.get("state") == "outcome_unknown":
            raise OutcomeUnknownError(result)
        if result.get("state") != "succeeded":
            raise RuntimeError(f"Engine 操作失败：{result}")
