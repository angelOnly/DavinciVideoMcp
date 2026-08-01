"""FFmpeg/ffprobe 的受控封装，返回结构化媒体事实。"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from davinci_app.common import run_command


class MediaProbeError(RuntimeError):
    """媒体探测或解码失败，错误文本应可直接显示给用户。"""


@dataclass(frozen=True)
class MediaProbe:
    path: str
    duration_seconds: float | None
    format_name: str | None
    video_streams: int
    audio_streams: int
    width: int | None
    height: int | None
    average_frame_rate: float | None
    real_frame_rate: float | None
    video_codec: str | None
    video_profile: str | None
    pixel_format: str | None
    audio_codec: str | None
    sample_rate: int | None
    rotation: int | None

    @property
    def has_video(self) -> bool:
        return self.video_streams > 0

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0

    @property
    def is_variable_frame_rate(self) -> bool:
        if not self.average_frame_rate or not self.real_frame_rate:
            return False
        return not math.isclose(self.average_frame_rate, self.real_frame_rate, rel_tol=0.003)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ffmpeg_path() -> str:
    value = shutil.which("ffmpeg")
    if not value:
        raise MediaProbeError("未找到 FFmpeg；请将 ffmpeg 加入 PATH 后重试。")
    return value


def ffprobe_path() -> str:
    value = shutil.which("ffprobe")
    if not value:
        raise MediaProbeError("未找到 ffprobe；请将 ffprobe 加入 PATH 后重试。")
    return value


def ffmpeg_health() -> dict[str, str | bool]:
    try:
        ffmpeg = ffmpeg_path()
        ffprobe = ffprobe_path()
    except MediaProbeError as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "ffmpeg": ffmpeg, "ffprobe": ffprobe}


def probe_media(path: Path) -> MediaProbe:
    command = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = run_command(command, timeout_seconds=45)
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-600:] or "ffprobe 未返回可用信息"
        raise MediaProbeError(f"无法读取媒体信息：{detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe 返回的媒体信息无法解析。") from exc

    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    raw_duration = (payload.get("format") or {}).get("duration")
    duration = _float_or_none(raw_duration)
    if duration is None and video:
        duration = _float_or_none(video.get("duration"))
    return MediaProbe(
        path=str(path),
        duration_seconds=duration,
        format_name=(payload.get("format") or {}).get("format_name"),
        video_streams=sum(stream.get("codec_type") == "video" for stream in streams),
        audio_streams=sum(stream.get("codec_type") == "audio" for stream in streams),
        width=_int_or_none(video.get("width")) if video else None,
        height=_int_or_none(video.get("height")) if video else None,
        average_frame_rate=_frame_rate(video.get("avg_frame_rate")) if video else None,
        real_frame_rate=_frame_rate(video.get("r_frame_rate")) if video else None,
        video_codec=video.get("codec_name") if video else None,
        video_profile=video.get("profile") if video else None,
        pixel_format=video.get("pix_fmt") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=_int_or_none(audio.get("sample_rate")) if audio else None,
        rotation=_rotation(video) if video else None,
    )


def decode_check(path: Path, probe: MediaProbe, *, full_scan: bool = False) -> list[dict[str, Any]]:
    """验证开头、中间、结尾；短片或显式请求会完整扫描。"""
    errors: list[dict[str, Any]] = []
    duration = probe.duration_seconds or 0.0
    moments = _sample_moments(duration)
    for moment in moments:
        if probe.has_video:
            errors.extend(_decode_at(path, moment, "video"))
        if probe.has_audio:
            errors.extend(_decode_at(path, moment, "audio"))
    if full_scan and duration > 0:
        command = [ffmpeg_path(), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"]
        completed = run_command(command, timeout_seconds=max(180, duration * 2))
        if completed.returncode != 0:
            errors.append(
                {
                    "code": "full_decode_failed",
                    "time_range": None,
                    "message": completed.stderr.strip()[-600:] or "完整解码扫描失败",
                }
            )
    return errors


def extract_audio(path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path(),
        "-y",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    completed = run_command(command, timeout_seconds=180)
    if completed.returncode != 0:
        raise MediaProbeError(completed.stderr.strip()[-600:] or "无法提取分析音频")


def make_working_copy(source: Path, target: Path, probe: MediaProbe) -> None:
    """生成稳定 H.264/AAC 工作副本，永不覆盖用户源文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件仍保留 .mp4 扩展名，FFmpeg 才能按容器正确选择输出格式。
    partial = target.with_name(f"{target.stem}.partial{target.suffix}")
    command = [ffmpeg_path(), "-y", "-v", "error", "-i", str(source)]
    if probe.has_video:
        command.extend(
            [
                "-map",
                "0:v:0",
                "-c:v",
                "libx264",
                "-profile:v",
                "high",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-vsync",
                "cfr",
            ]
        )
    if probe.has_audio:
        command.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"])
    command.extend(["-movflags", "+faststart", str(partial)])
    completed = run_command(command, timeout_seconds=max(300, (probe.duration_seconds or 60) * 4))
    if completed.returncode != 0:
        partial.unlink(missing_ok=True)
        detail = completed.stderr.strip()[-800:] or "FFmpeg 未提供详细错误"
        raise MediaProbeError(f"无法生成稳定工作副本：{detail}")
    partial.replace(target)


def _decode_at(path: Path, seconds: float, stream_type: str) -> list[dict[str, Any]]:
    if stream_type == "video":
        command = [
            ffmpeg_path(),
            "-v",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
    else:
        command = [
            ffmpeg_path(),
            "-v",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-t",
            "0.5",
            "-f",
            "null",
            "-",
        ]
    completed = run_command(command, timeout_seconds=45)
    if completed.returncode == 0:
        return []
    return [
        {
            "code": f"{stream_type}_decode_failed",
            "time_range": [round(seconds, 3), round(seconds + 0.5, 3)],
            "message": completed.stderr.strip()[-500:] or f"{stream_type} 在该位置无法解码",
        }
    ]


def _sample_moments(duration: float) -> list[float]:
    if duration <= 0:
        return [0.0]
    edge = min(0.25, duration / 4)
    values = [edge, duration / 2, max(edge, duration - edge)]
    return list(dict.fromkeys(round(value, 3) for value in values))


def _frame_rate(value: Any) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else None
    except (TypeError, ValueError):
        return _float_or_none(value)


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rotation(stream: dict[str, Any]) -> int | None:
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            return _int_or_none(side_data["rotation"])
    tags = stream.get("tags") or {}
    return _int_or_none(tags.get("rotate"))
