"""系统能力健康检查；只把真实实测通过的能力暴露给工作流。"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from davinci_app.adapters.codex_app_server import CodexAppServerSkillRuntime
from davinci_app.adapters.funasr_transcriber import FunASRTranscriberAdapter
from davinci_app.adapters.openai_multimodal import OpenAICompatibleMultimodalAdapter
from davinci_app.config import AppConfig, runtime_report
from davinci_app.media.probe import ffmpeg_health


_CACHE_SECONDS = 300.0
_health_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def check_system_health(config: AppConfig, *, force: bool = False) -> dict[str, Any]:
    """缓存短时间健康结果，避免 Web 轮询反复加载本地大模型或消耗云端探测配额。"""
    cache_key = f"{config.repository_root}|{config.multimodal_base_url}|{config.multimodal_model}|{config.funasr_manifest}"
    now = time.monotonic()
    cached = _health_cache.get(cache_key)
    if not force and cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    funasr = FunASRTranscriberAdapter(config).health_check()
    multimodal = OpenAICompatibleMultimodalAdapter(config).health_check()
    # health_check 不会触碰 project_service；传入 None 不会形成第二份业务状态。
    codex = CodexAppServerSkillRuntime(config, project_service=None).health_check()
    result = {
        "runtime": runtime_report(config),
        "ffmpeg": ffmpeg_health(),
        "funasr": funasr,
        "multimodal": multimodal,
        "codex_app_server": codex,
        "creative_catalog": _creative_catalog_health(config),
    }
    _health_cache[cache_key] = (now, result)
    return result


def _creative_catalog_health(config: AppConfig) -> dict[str, Any]:
    """目录存在不代表能力已认证；只报告实际认证条目数。"""
    certified_count = 0
    try:
        with sqlite3.connect(config.creative_catalog_database) as connection:
            row = connection.execute("SELECT COUNT(*) FROM capabilities WHERE state = 'certified'").fetchone()
            certified_count = int(row[0]) if row else 0
    except sqlite3.Error:
        # 健康接口不能因目录索引尚未初始化而虚报任何可用能力。
        certified_count = 0
    return {
        "raw_root_exists": config.creative_raw_root.exists(),
        "certified_root_exists": config.creative_certified_root.exists(),
        "certified_count": certified_count,
        "cache_root": str(config.creative_cache_root),
    }
