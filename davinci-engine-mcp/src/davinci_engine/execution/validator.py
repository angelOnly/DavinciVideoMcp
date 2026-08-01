"""执行前的确定性校验，拒绝未经本地化和哈希校验的资源。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_engine.analysis.ffmpeg_runtime import FFmpegRuntimeError, duration_seconds, stream_summary
from davinci_engine.common import sha256_file
from davinci_engine.execution.plan import ResolveExecutionPlan


def validate(plan: ResolveExecutionPlan) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not plan.project_name.startswith("DavinciMcp_"):
        errors.append({"code": "unsafe_project_name", "message": "工作项目必须以 DavinciMcp_ 开头，避免写入用户项目。"})
    if not plan.timeline_name.startswith("run_"):
        errors.append({"code": "unsafe_timeline_name", "message": "工作时间线必须以 run_ 开头，避免覆盖用户可见基线。"})
    render_file = plan.render_file()
    if render_file.exists():
        errors.append({"code": "render_already_exists", "message": "目标渲染文件已存在，拒绝覆盖。"})
    previous_record_end = -1
    for index, clip in enumerate(plan.clips):
        try:
            local_path = clip.path()
        except ValueError as exc:
            errors.append({"code": "path_outside_workspace", "message": str(exc)})
            continue
        if not local_path.exists():
            errors.append({"code": "asset_missing", "message": f"片段 {index} 的本地缓存文件不存在。"})
            continue
        if sha256_file(local_path) != clip.content_hash:
            errors.append({"code": "asset_hash_mismatch", "message": f"片段 {index} 的内容哈希不匹配。"})
            continue
        try:
            duration = duration_seconds(local_path)
            summary = stream_summary(local_path)
        except FFmpegRuntimeError as exc:
            errors.append({"code": "asset_probe_failed", "message": f"片段 {index} 无法探测：{exc}"})
            continue
        if not summary["has_video"]:
            errors.append({"code": "missing_video", "message": f"片段 {index} 没有视频流。"})
        if clip.include_audio and not summary["has_audio"]:
            warnings.append({"code": "missing_audio", "message": f"片段 {index} 没有音频流，将只放置画面。"})
        if clip.source_out_seconds > duration + 0.05:
            errors.append({"code": "source_range_out_of_bounds", "message": f"片段 {index} 的源出点超过媒体时长。"})
        expected_end = clip.record_frame + round((clip.source_out_seconds - clip.source_in_seconds) * plan.timeline_fps)
        if clip.record_frame < previous_record_end:
            errors.append({"code": "timeline_overlap", "message": f"片段 {index} 与前一片段在目标时间线上重叠。"})
        previous_record_end = max(previous_record_end, expected_end)
    return {
        "valid": not errors,
        "plan_digest": plan.digest,
        "errors": errors,
        "warnings": warnings,
        "expected_duration_seconds": plan.expected_duration_seconds,
    }

