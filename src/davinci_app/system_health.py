"""系统能力健康检查；只启用实际通过检查的能力。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from davinci_app.config import AppConfig, runtime_report
from davinci_app.media.probe import ffmpeg_health


def check_system_health(config: AppConfig) -> dict[str, Any]:
    funasr = _funasr_health(config.funasr_manifest)
    multimodal = _multimodal_health(config)
    return {
        "runtime": runtime_report(config),
        "ffmpeg": ffmpeg_health(),
        "funasr": funasr,
        "multimodal": multimodal,
        "creative_roots": {
            "raw_available": config.creative_raw_root.exists(),
            "certified_available": config.creative_certified_root.exists(),
            "cache_root": str(config.creative_cache_root),
        },
    }


def _funasr_health(manifest: Path) -> dict[str, Any]:
    if not manifest.exists():
        return {"available": False, "reason": "未找到 models/manifest.yaml；不会自动下载模型。"}
    package_available = importlib.util.find_spec("funasr") is not None
    # 只读取 manifest 路径，不载入模型，避免 API 进程占用模型与 GPU。
    return {
        "available": package_available,
        "manifest": str(manifest),
        "reason": None if package_available else "当前 Conda 环境未安装 FunASR；转写类任务会被阻止。",
    }


def _multimodal_health(config: AppConfig) -> dict[str, Any]:
    has_key = bool(os.environ.get("MULTIMODAL_API_KEY"))
    if not config.multimodal_base_url or not has_key:
        return {
            "available": False,
            "model": config.multimodal_model,
            "reason": "未配置本机多模态端点或密钥；系统不会按模型名称虚报能力。",
            "capabilities": {},
        }
    return {
        "available": False,
        "model": config.multimodal_model,
        "reason": "端点已配置，但尚未完成文本、图片、音频、视频和结构化输出实测。",
        "capabilities": {},
    }

