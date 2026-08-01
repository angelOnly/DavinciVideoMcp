"""Engine 使用的 FFmpeg 发现、探测和渲染文件级验证。"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any


class FFmpegRuntimeError(RuntimeError):
    pass


def find_binary(name: str) -> str:
    candidate = shutil.which(name)
    if not candidate:
        raise FFmpegRuntimeError(f"未找到 {name}，请检查 PATH。")
    return candidate


def status() -> dict[str, Any]:
    try:
        return {"available": True, "ffmpeg": find_binary("ffmpeg"), "ffprobe": find_binary("ffprobe")}
    except FFmpegRuntimeError as exc:
        return {"available": False, "reason": str(exc)}


def probe(path: Path) -> dict[str, Any]:
    completed = _run(
        [
            find_binary("ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ],
        timeout=60,
    )
    if completed.returncode != 0:
        raise FFmpegRuntimeError(completed.stderr.strip()[-800:] or "ffprobe 执行失败")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegRuntimeError("ffprobe 返回了无法解析的内容") from exc


def duration_seconds(path: Path) -> float:
    payload = probe(path)
    raw = (payload.get("format") or {}).get("duration")
    try:
        result = float(raw)
    except (TypeError, ValueError) as exc:
        raise FFmpegRuntimeError("媒体没有可靠时长") from exc
    if not math.isfinite(result) or result <= 0:
        raise FFmpegRuntimeError("媒体时长不可靠")
    return result


def stream_summary(path: Path) -> dict[str, Any]:
    payload = probe(path)
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    return {
        "duration_seconds": _number((payload.get("format") or {}).get("duration")),
        "has_video": bool(video),
        "has_audio": bool(audio),
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "fps": _frame_rate(video.get("avg_frame_rate")) if video else None,
    }


def verify_render(path: Path, *, expected_duration: float | None = None) -> dict[str, Any]:
    """技术验证只陈述可解码、时长、声画流和黑帧候选，不判断审美。"""
    if not path.exists() or path.stat().st_size == 0:
        return {"valid": False, "errors": [{"code": "missing_render", "message": "渲染文件不存在或为空。"}]}
    summary = stream_summary(path)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not summary["has_video"]:
        errors.append({"code": "missing_video", "message": "渲染文件没有视频流。"})
    if not summary["has_audio"]:
        warnings.append({"code": "missing_audio", "message": "渲染文件没有音频流。"})
    if expected_duration is not None and summary["duration_seconds"] is not None:
        deviation = abs(float(summary["duration_seconds"]) - expected_duration)
        if deviation > 1.5:
            errors.append(
                {
                    "code": "duration_mismatch",
                    "message": f"渲染时长与计划相差 {deviation:.2f} 秒。",
                }
            )
    decode = _run([find_binary("ffmpeg"), "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=300)
    if decode.returncode != 0:
        errors.append(
            {"code": "decode_failed", "message": decode.stderr.strip()[-800:] or "渲染文件无法完整解码。"}
        )
    return {"valid": not errors, "summary": summary, "errors": errors, "warnings": warnings}


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_rate(value: Any) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator_float = float(denominator)
        return float(numerator) / denominator_float if denominator_float else None
    except (ValueError, TypeError):
        return _number(value)

