"""Product Workflow Worker：唯一可调用 Engine MCP 写工具的产品进程。"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from davinci_app.execution.compiler import TestCutCompiler
from davinci_app.execution.engine_client import EngineMcpClient, EngineMcpError
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
        compiler: TestCutCompiler | None = None,
        evidence_builder: EvidenceBuilder | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.project_service = project_service
        self.queue = queue
        self.compiler = compiler or TestCutCompiler()
        self.evidence_builder = evidence_builder or EvidenceBuilder()
        self.worker_id = worker_id or new_worker_id()

    def run_once(self) -> bool:
        task = self.queue.claim(self.worker_id)
        if not task:
            return False
        self.project_service.mark_run_running(task.run_id)
        self.queue.record_step(task.id, "claim_task", "succeeded", {"worker_id": self.worker_id})
        try:
            if task.task_type != "build_candidate":
                raise RuntimeError(f"不支持的任务类型：{task.task_type}")
            self._build_candidate(task)
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

    def _build_candidate(self, task: ClaimedTask) -> None:
        run = self.project_service.get_run(task.run_id)
        project_paths = self.project_service.project_paths(run["project_id"])
        assets = run["input_snapshot"].get("assets") or []
        self.queue.record_step(task.id, "build_evidence", "running")
        evidence = []
        for asset in assets:
            evidence.append(self.evidence_builder.build(asset, project_paths["evidence"]))
        self.queue.record_step(
            task.id,
            "build_evidence",
            "succeeded",
            {"asset_count": len(evidence), "manifests": [item["manifest_path"] for item in evidence]},
        )

        self.queue.record_step(task.id, "compile_execution_plan", "running")
        compilation = self.compiler.compile(run, project_paths)
        plan = compilation["plan"]
        self.queue.record_step(
            task.id,
            "compile_execution_plan",
            "succeeded",
            {"plan_path": compilation["plan_path"], "local_compiler_digest": compilation["plan_digest"]},
        )

        with EngineMcpClient(self.project_service.config) as engine:
            self._ensure_engine_ready(task, engine)
            validation = engine.call("validate_execution_plan", {"plan": plan})
            if validation.get("state") != "succeeded" or not validation.get("validation", {}).get("valid"):
                raise RuntimeError(f"执行计划未通过校验：{validation}")
            self.queue.record_step(task.id, "validate_execution_plan", "succeeded", validation)
            preview = engine.call("preview_execution_plan", {"plan": plan})
            if preview.get("state") != "succeeded":
                raise RuntimeError(f"执行计划预览失败：{preview}")
            plan_digest = preview["plan_digest"]
            self.queue.record_step(task.id, "preview_execution_plan", "succeeded", preview)

            self.queue.acquire_resolve_writer(self.worker_id)
            try:
                execution = engine.call(
                    "execute_execution_plan",
                    {
                        "plan": plan,
                        "operation_id": f"execute-{run['id']}",
                        "execution_permit": plan_digest,
                    },
                    timeout_seconds=600,
                )
                self._require_known_success(execution)
                self.queue.record_step(task.id, "execute_execution_plan", "succeeded", execution)
                rendering = engine.call(
                    "render_version",
                    {
                        "plan": plan,
                        "operation_id": f"render-{run['id']}",
                        "execution_permit": plan_digest,
                    },
                    timeout_seconds=2400,
                )
                self._require_known_success(rendering)
                self.queue.record_step(task.id, "render_version", "succeeded", rendering)
            finally:
                self.queue.release_resolve_writer(self.worker_id)

        output_path = Path(rendering["output_path"])
        version = self.project_service.publish_video_version(run["id"], output_path, plan_digest)
        self.queue.record_step(
            task.id,
            "publish_video_version",
            "succeeded",
            {"version_id": version["id"], "output_path": version["output_path"]},
        )

    def _ensure_engine_ready(self, task: ClaimedTask, engine: EngineMcpClient) -> None:
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
