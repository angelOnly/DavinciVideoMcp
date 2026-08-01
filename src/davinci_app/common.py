"""跨模块的确定性小工具，禁止在这里接入外部服务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    """返回可排序的 UTC 时间，所有持久化记录统一使用它。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    """稳定序列化用于摘要哈希，避免字典顺序影响执行许可。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """以固定块大小计算媒体内容身份，不把文件内容写入数据库。"""
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def safe_filename(name: str, fallback: str = "asset") -> str:
    """保留常见 Unicode 文件名，同时排除 Windows 不允许的字符。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or fallback


def ensure_within(path: Path, root: Path) -> Path:
    """解析后确认路径没有逃离受管根目录。"""
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"路径不在允许目录内：{resolved_path}") from exc
    return resolved_path


def json_loads_or_default(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    return json.loads(raw)


def run_command(
    command: Iterable[str], *, timeout_seconds: float, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """统一执行外部媒体工具，并保留尾部错误供用户看到可操作原因。"""
    return subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def is_windows_placeholder(path: Path) -> bool:
    """拒绝 OneDrive/Nextcloud 等尚未本地化的 Windows 占位文件。"""
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    offline = getattr(stat_module(), "FILE_ATTRIBUTE_OFFLINE", 0x1000)
    recall_on_access = getattr(stat_module(), "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0x400000)
    return bool(attributes & (offline | recall_on_access))


def stat_module() -> Any:
    # 延迟导入让非 Windows 合同测试保持简单。
    import stat

    return stat


def redacted_environment_summary() -> dict[str, str | None]:
    """仅报告环境身份，绝不返回 API Key、Token 或密码。"""
    return {
        "conda_environment": os.environ.get("CONDA_DEFAULT_ENV"),
        "python_executable": os.path.abspath(os.sys.executable),
    }

