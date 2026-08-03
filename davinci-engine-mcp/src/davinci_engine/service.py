"""Engine 的高层工具实现；Codex 与 Web 不直接访问这些内部对象。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from davinci_engine.analysis import ffmpeg_runtime
from davinci_engine.common import ensure_within_workspace
from davinci_engine.creative.adapters import default_adapter_registry
from davinci_engine.execution.executor import EngineExecutor
from davinci_engine.execution.journal import EngineJournal
from davinci_engine.execution.plan import ExecutionPlanError, ResolveExecutionPlan
from davinci_engine.execution.validator import validate
from davinci_engine.resolve.connection import ResolveConnection


class EngineService:
    def __init__(self) -> None:
        self.journal = EngineJournal()
        self.journal.initialize()
        self.connection = ResolveConnection()
        self.executor = EngineExecutor(self.connection, self.journal)
        self.adapter_registry = default_adapter_registry()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "engine_status":
                return {
                    "state": "succeeded",
                    "python_executable": sys.executable,
                    "ffmpeg": ffmpeg_runtime.status(),
                    "resolve": self.connection.execution_readiness(),
                    # 仅表示代码存在相应机制 Adapter，不表示任一素材已经认证或部署。
                    "supported_creative_adapter_mechanisms": self.adapter_registry.mechanisms(),
                    "installed_capabilities": [],
                }
            if name == "analyze_media":
                path = ensure_within_workspace(Path(_required(arguments, "path")))
                return {"state": "succeeded", "media": ffmpeg_runtime.stream_summary(path)}
            if name == "inspect_resolve":
                return {"state": "succeeded", "resolve": self.connection.inspect()}
            if name == "list_installed_capabilities":
                return {
                    "state": "succeeded",
                    "capabilities": [],
                    "supported_adapter_mechanisms": self.adapter_registry.mechanisms(),
                }
            if name == "validate_execution_plan":
                plan = _plan(arguments)
                result = validate(plan)
                return {"state": "succeeded" if result["valid"] else "failed", "validation": result}
            if name == "preview_execution_plan":
                return self.executor.preview(_plan(arguments))
            if name == "execute_execution_plan":
                plan = _plan(arguments)
                return self.executor.execute(
                    plan,
                    str(_required(arguments, "operation_id")),
                    str(_required(arguments, "execution_permit")),
                )
            if name == "render_version":
                plan = _plan(arguments)
                return self.executor.render(
                    plan,
                    str(_required(arguments, "operation_id")),
                    str(_required(arguments, "execution_permit")),
                )
            if name == "reconcile_operation":
                return self.executor.reconcile(str(_required(arguments, "operation_id")))
            if name == "inspect_render":
                path = ensure_within_workspace(Path(_required(arguments, "path")))
                return {"state": "succeeded", "exists": path.exists(), "path": str(path)}
            if name == "verify_render":
                path = ensure_within_workspace(Path(_required(arguments, "path")))
                expected = arguments.get("expected_duration_seconds")
                verification = ffmpeg_runtime.verify_render(path, expected_duration=float(expected) if expected else None)
                return {"state": "succeeded" if verification["valid"] else "failed", "verification": verification}
            return {"state": "failed", "error": {"code": "unknown_tool", "message": f"未知 Engine 工具：{name}"}}
        except (ExecutionPlanError, ValueError, KeyError) as exc:
            return {"state": "failed", "error": {"code": "invalid_arguments", "message": str(exc)}}
        except BaseException as exc:
            return {
                "state": "failed",
                "error": {"code": "engine_internal_error", "message": f"{type(exc).__name__}: {exc}"},
            }


def _required(arguments: dict[str, Any], key: str) -> Any:
    if key not in arguments:
        raise KeyError(f"缺少参数 {key}")
    return arguments[key]


def _plan(arguments: dict[str, Any]) -> ResolveExecutionPlan:
    raw = _required(arguments, "plan")
    if not isinstance(raw, dict):
        raise ExecutionPlanError("plan 必须是对象。")
    return ResolveExecutionPlan.from_dict(raw)
