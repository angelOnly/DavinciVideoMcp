"""服务端权威素材校验与稳定工作副本策略。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from davinci_app.common import is_windows_placeholder, sha256_file
from davinci_app.media.probe import MediaProbe, MediaProbeError, decode_check, make_working_copy, probe_media


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    content_hash: str | None
    probe: MediaProbe | None
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    working_copy_created: bool


class UploadValidator:
    """只验证本地 staging 文件；业务状态由 ProjectService 维护。"""

    def validate(
        self,
        path: Path,
        *,
        role: str,
        working_copy_path: Path,
        full_scan: bool = False,
    ) -> ValidationResult:
        warnings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not path.exists() or not path.is_file():
            return self._failure("file_missing", "上传文件不存在或不可访问。")
        if path.stat().st_size == 0:
            return self._failure("empty_file", "文件为空，请重新上传或替换素材。")
        if is_windows_placeholder(path):
            return self._failure(
                "cloud_placeholder",
                "文件仍是云盘占位文件，请先在本机完整下载后再提交。",
            )

        try:
            before_hash = sha256_file(path)
            probe = probe_media(path)
        except (OSError, MediaProbeError) as exc:
            return self._failure("probe_failed", f"无法读取素材：{exc}")

        errors.extend(self._required_stream_errors(role, probe))
        if probe.duration_seconds is not None and probe.duration_seconds <= 0:
            errors.append({"code": "zero_duration", "message": "媒体时长为零，不能用于剪辑。"})
        if not probe.has_video and not probe.has_audio:
            errors.append({"code": "no_media_stream", "message": "文件没有可用的视频、音频或图片媒体流。"})
        if errors:
            return ValidationResult(False, before_hash, probe, warnings, errors, False)

        try:
            errors.extend(decode_check(path, probe, full_scan=full_scan))
        except (OSError, MediaProbeError) as exc:
            errors.append({"code": "decode_check_failed", "message": f"解码验证失败：{exc}"})
        if errors:
            return ValidationResult(False, before_hash, probe, warnings, errors, False)

        if sha256_file(path) != before_hash:
            return self._failure(
                "source_changed",
                "校验期间源文件发生变化，请重新上传后再试。",
                probe=probe,
            )

        working_copy_created = False
        if self._requires_working_copy(probe):
            try:
                make_working_copy(path, working_copy_path, probe)
                working_probe = probe_media(working_copy_path)
                working_errors = decode_check(working_copy_path, working_probe, full_scan=False)
                if working_errors:
                    return ValidationResult(False, before_hash, probe, warnings, working_errors, False)
                working_copy_created = True
                warnings.append(
                    {
                        "code": "working_copy_created",
                        "message": "已为可变帧率或兼容性问题生成稳定工作副本；原文件保持不变。",
                    }
                )
            except (OSError, MediaProbeError) as exc:
                return self._failure(
                    "working_copy_failed",
                    f"素材需要稳定工作副本，但生成失败：{exc}",
                    probe=probe,
                )
        elif probe.has_video and (probe.width or 0) < 720:
            warnings.append(
                {
                    "code": "low_resolution",
                    "message": "视频分辨率较低，放大到目标画幅时可能影响清晰度。",
                }
            )
        if role in {"main", "interview", "primary"} and not probe.has_audio:
            warnings.append(
                {
                    "code": "missing_audio",
                    "message": "主视频没有音轨；如需保留原声，请替换为含音频的文件。",
                }
            )
        return ValidationResult(True, before_hash, probe, warnings, [], working_copy_created)

    @staticmethod
    def _required_stream_errors(role: str, probe: MediaProbe) -> list[dict[str, Any]]:
        if role in {"main", "interview", "primary", "b_roll", "reference"} and not probe.has_video:
            return [{"code": "video_required", "message": "该素材角色需要可解码视频流。"}]
        if role in {"music", "sound_effect", "voiceover"} and not probe.has_audio:
            return [{"code": "audio_required", "message": "该素材角色需要可解码音频流。"}]
        return []

    @staticmethod
    def _requires_working_copy(probe: MediaProbe) -> bool:
        # HEVC 与 Constrained Baseline H.264 在 Resolve 工作站间兼容性较差；先在上传
        # 阶段固化为 H.264/AAC CFR 工作副本，保留原始文件不变。实际导入失败仍须由
        # Engine 写后读回判定，不能仅根据编码名称臆测成功或失败。
        codec = (probe.video_codec or "").lower()
        profile = (probe.video_profile or "").lower()
        supported_codecs = {"h264", "prores", "dnxhd", "dnxhr"}
        return bool(
            probe.has_video
            and (
                probe.is_variable_frame_rate
                or codec not in supported_codecs
                or codec == "hevc"
                or (codec == "h264" and "baseline" in profile)
            )
        )

    @staticmethod
    def _failure(code: str, message: str, probe: MediaProbe | None = None) -> ValidationResult:
        return ValidationResult(
            valid=False,
            content_hash=None,
            probe=probe,
            warnings=[],
            errors=[{"code": code, "message": message}],
            working_copy_created=False,
        )
