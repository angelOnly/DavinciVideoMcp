"""无需第三方 SDK 的 stdio JSON-RPC MCP Server。"""

from __future__ import annotations

import json
import sys
from typing import Any

from davinci_engine.service import EngineService


TOOLS = [
    ("engine_status", "检查 Resolve、FFmpeg 与 Engine 运行状态"),
    ("analyze_media", "读取确定性媒体技术信息"),
    ("inspect_resolve", "只读检查 Resolve 项目、时间线和轨道"),
    ("list_installed_capabilities", "读取本机已部署创意能力库存"),
    ("validate_execution_plan", "校验结构化 ResolveExecutionPlan"),
    ("preview_execution_plan", "预览计划摘要和将发生的变更"),
    ("execute_execution_plan", "执行已许可的计划并做写后读回"),
    ("reconcile_operation", "只读对账结果未知的写操作"),
    ("render_version", "渲染已执行的工作时间线"),
    ("inspect_render", "检查渲染输出是否存在"),
    ("verify_render", "验证渲染文件的基础技术状态"),
]


def run() -> None:
    service = EngineService()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = _handle(service, request)
        except BaseException as exc:
            response = _error(None, -32603, f"Engine MCP 内部错误：{type(exc).__name__}: {exc}")
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _handle(service: EngineService, request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "davinci-engine-mcp", "version": "0.1.0"},
            },
        )
    if method == "notifications/initialized":
        return _result(request_id, {}) if request_id is not None else {"jsonrpc": "2.0", "result": {}}
    if method == "tools/list":
        return _result(
            request_id,
            {
                "tools": [
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": {"type": "object", "additionalProperties": True},
                    }
                    for name, description in TOOLS
                ]
            },
        )
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(request_id, -32602, "tools/call 需要 name 和对象类型 arguments")
        structured = service.call(name, arguments)
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
                "structuredContent": structured,
                "isError": structured.get("state") == "failed",
            },
        )
    return _error(request_id, -32601, f"未实现的 MCP 方法：{method}")


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

