"""本地单用户 HTTP API；API 进程只处理产品状态，不导入 Resolve 原生模块。"""

from __future__ import annotations

import io
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from davinci_app.bootstrap import ApplicationContainer
from davinci_app.common import ensure_within
from davinci_app.project.service import ProjectStateError
from davinci_app.system_health import check_system_health


class _LimitedReader:
    def __init__(self, source: Any, remaining: int) -> None:
        self.source = source
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = self.source.read(size)
        self.remaining -= len(data)
        return data


def serve(container: ApplicationContainer, host: str, port: int) -> None:
    handler = _handler_factory(container)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"DavinciMcp API 已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _handler_factory(container: ApplicationContainer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DavinciMcp/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/health":
                    return self._json(HTTPStatus.OK, check_system_health(container.config))
                if parsed.path == "/api/projects":
                    return self._json(HTTPStatus.OK, {"projects": container.projects.list_projects()})
                match = re.fullmatch(r"/api/projects/([a-f0-9]+)", parsed.path)
                if match:
                    return self._json(HTTPStatus.OK, container.projects.get_project(match.group(1)))
                match = re.fullmatch(r"/api/runs/([a-f0-9]+)", parsed.path)
                if match:
                    return self._json(HTTPStatus.OK, container.projects.get_run(match.group(1)))
                match = re.fullmatch(r"/api/versions/([a-f0-9]+)/media", parsed.path)
                if match:
                    return self._video(container.projects.get_video_version(match.group(1)))
                return self._static(parsed.path)
            except ProjectStateError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": str(exc)}})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_request", "message": str(exc)}})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/projects":
                    payload = self._json_body()
                    project = container.projects.create_project(str(payload.get("title", "")), payload.get("brief") or {})
                    return self._json(HTTPStatus.CREATED, project)
                match = re.fullmatch(r"/api/projects/([a-f0-9]+)/assets/upload", parsed.path)
                if match:
                    filename = _first(query, "filename")
                    if not filename:
                        raise ValueError("上传请求必须包含 filename 查询参数。")
                    role = _first(query, "role") or "primary"
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0:
                        raise ValueError("上传文件为空。")
                    asset = container.projects.receive_upload(
                        match.group(1), filename, _LimitedReader(self.rfile, length), role=role
                    )
                    return self._json(HTTPStatus.CREATED, asset)
                match = re.fullmatch(r"/api/projects/([a-f0-9]+)/runs", parsed.path)
                if match:
                    payload = self._json_body()
                    asset_ids = payload.get("asset_ids")
                    if asset_ids is not None and not isinstance(asset_ids, list):
                        raise ValueError("asset_ids 必须是数组。")
                    run = container.projects.freeze_run(match.group(1), asset_ids=asset_ids)
                    return self._json(HTTPStatus.CREATED, run)
                match = re.fullmatch(r"/api/assets/([a-f0-9]+)/remove", parsed.path)
                if match:
                    container.projects.remove_asset(match.group(1))
                    return self._json(HTTPStatus.OK, {"removed": True})
                return self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "接口不存在。"}})
            except ProjectStateError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": {"code": "state_error", "message": str(exc)}})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_request", "message": str(exc)}})
            except OSError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"code": "storage_error", "message": str(exc)}})

        def _json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("请求 JSON 必须是对象。")
            return value

        def _json(self, status: HTTPStatus, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _video(self, version: dict[str, Any]) -> None:
            path = ensure_within(Path(version["output_path"]), container.config.projects_root)
            if not path.exists():
                return self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "render_missing", "message": "版本视频文件不存在。"}})
            file_size = path.stat().st_size
            start, end = _range(self.headers.get("Range"), file_size)
            length = end - start + 1
            status = HTTPStatus.PARTIAL_CONTENT if self.headers.get("Range") else HTTPStatus.OK
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

        def _static(self, request_path: str) -> None:
            dist_root = container.config.repository_root / "web" / "dist"
            relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
            candidate = ensure_within(dist_root / relative, dist_root)
            if not candidate.exists() or not candidate.is_file():
                candidate = dist_root / "index.html"
            if not candidate.exists():
                body = "前端尚未构建。请在 web 目录执行 npm install 与 npm run build。".encode("utf-8")
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # 本地 API 不记录请求体，避免文件名之外的用户数据或密钥进入控制台日志。
            return

    return Handler


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _range(header: str | None, size: int) -> tuple[int, int]:
    if not header:
        return 0, max(0, size - 1)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match:
        raise ValueError("Range 请求格式不合法。")
    start = int(match.group(1)) if match.group(1) else 0
    end = int(match.group(2)) if match.group(2) else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError("Range 超出视频文件范围。")
    return start, min(end, size - 1)

