"""Worker 私有的 stdio Engine MCP 客户端。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from davinci_app.config import AppConfig


class EngineMcpError(RuntimeError):
    pass


class EngineMcpClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def __enter__(self) -> "EngineMcpClient":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        engine_source = self.config.repository_root / "davinci-engine-mcp" / "src"
        app_source = self.config.repository_root / "src"
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(engine_source), str(app_source), existing) if value
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "davinci_engine"],
            cwd=self.config.repository_root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "workflow-worker", "version": "0.1"}}, 90)

    def call(self, tool_name: str, arguments: dict[str, Any], *, timeout_seconds: int = 300) -> dict[str, Any]:
        response = self._request("tools/call", {"name": tool_name, "arguments": arguments}, timeout_seconds)
        result = response.get("result")
        if not isinstance(result, dict):
            raise EngineMcpError("Engine MCP 返回中缺少 result。")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content") or []
        if content and isinstance(content[0], dict) and isinstance(content[0].get("text"), str):
            return json.loads(content[0]["text"])
        raise EngineMcpError("Engine MCP 返回中缺少结构化结果。")

    def close(self) -> None:
        process = self.process
        self.process = None
        if not process:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def _request(self, method: str, params: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        self.start_if_needed()
        assert self.process and self.process.stdin and self.process.stdout
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            try:
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except OSError as exc:
                raise EngineMcpError(f"无法向 Engine MCP 发送请求：{exc}") from exc
            line = _read_line_with_timeout(self.process.stdout, timeout_seconds)
            if line is None:
                self.close()
                raise EngineMcpError(f"Engine MCP 在 {timeout_seconds} 秒内未响应。")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EngineMcpError(f"Engine MCP 返回了非 JSON 数据：{line[:300]}") from exc
            if response.get("id") != request_id:
                raise EngineMcpError("Engine MCP 响应 ID 与请求不一致。")
            if "error" in response:
                raise EngineMcpError(f"Engine MCP 协议错误：{response['error']}")
            return response

    def start_if_needed(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.start()


def _read_line_with_timeout(stream: Any, timeout_seconds: int) -> str | None:
    value: list[str | None] = [None]

    def read() -> None:
        value[0] = stream.readline()

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        return None
    return value[0] or None

