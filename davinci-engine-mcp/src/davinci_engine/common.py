"""Engine 内部的确定性基础工具，不依赖 Product Application。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def workspace_root() -> Path:
    return repository_root() / "workspace"


def ensure_within_workspace(path: Path) -> Path:
    resolved = path.resolve()
    root = workspace_root().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Engine 拒绝访问工作区之外的路径：{resolved}") from exc
    return resolved

