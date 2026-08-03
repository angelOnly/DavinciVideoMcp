"""执行前的确定性校验，拒绝未经本地化和哈希校验的资源。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_engine.analysis.ffmpeg_runtime import FFmpegRuntimeError, duration_seconds, stream_summary
from davinci_engine.common import sha256_file
from davinci_engine.creative.adapters import DIRECT_MEDIA_MECHANISMS, default_adapter_registry
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
    registry = default_adapter_registry()
    for index, operation in enumerate(plan.creative_operations):
        _validate_creative_operation(plan, operation, index, registry, errors)
    return {
        "valid": not errors,
        "plan_digest": plan.digest,
        "errors": errors,
        "warnings": warnings,
        "expected_duration_seconds": plan.expected_duration_seconds,
    }


def _validate_creative_operation(
    plan: ResolveExecutionPlan,
    operation: object,
    index: int,
    registry: object,
    errors: list[dict[str, str]],
) -> None:
    """创意资源只能以已认证的明确机制进入 Engine 计划。"""

    # 此处使用 duck typing，避免 validator 依赖应用层 Catalog 或 SQLite 实现。
    try:
        path = operation.path()  # type: ignore[union-attr]
    except (AttributeError, ValueError) as exc:
        errors.append({"code": "creative_path_outside_workspace", "message": f"创意操作 {index} 路径无效：{exc}"})
        return
    if not path.exists():
        errors.append({"code": "creative_asset_missing", "message": f"创意操作 {index} 的本地缓存文件不存在。"})
        return
    if sha256_file(path) != operation.content_hash:  # type: ignore[union-attr]
        errors.append({"code": "creative_hash_mismatch", "message": f"创意操作 {index} 的内容哈希不匹配。"})
        return
    try:
        adapter = registry.get(operation.mechanism)  # type: ignore[union-attr]
        preflight = adapter.validate(path, operation.content_hash, {"mechanism": operation.mechanism})  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - 统一返回结构化执行前错误。
        errors.append({"code": "creative_adapter_unavailable", "message": f"创意操作 {index} 没有可用 Adapter：{exc}"})
        return
    if not preflight.ready_for_live_certification:
        errors.append({"code": "creative_adapter_preflight_failed", "message": f"创意操作 {index} 未通过 Adapter 预检：{preflight.reason}"})
        return

    parameters = operation.parameters  # type: ignore[union-attr]
    mechanism = operation.mechanism  # type: ignore[union-attr]
    if mechanism in DIRECT_MEDIA_MECHANISMS:
        _validate_direct_media_operation(index, mechanism, parameters, errors)
        return
    if mechanism == "lut_3d":
        target = _integer(parameters.get("target_clip_index"))
        relative = parameters.get("installed_relative_path")
        if target is None or target < 0 or target >= len(plan.clips):
            errors.append({"code": "lut_target_invalid", "message": f"LUT 操作 {index} 必须指向已有的目标视频片段。"})
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative.replace("\\", "/")).parts
            or not relative.lower().endswith(".cube")
        ):
            errors.append({"code": "lut_deployment_invalid", "message": f"LUT 操作 {index} 缺少受管安装相对路径。"})
        return
    # Font 与 Fusion 的静态/部署 Adapter 已存在，但没有经过对应的标题或效果 Compiler
    # Mapping 合同，不能让通用 parameters 变成隐式自动化执行。
    errors.append(
        {
            "code": "creative_mapping_unavailable",
            "message": f"机制 {mechanism} 尚无认证 Compiler Mapping，不能进入执行计划。",
        }
    )


def _validate_direct_media_operation(
    index: int,
    mechanism: str,
    parameters: object,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(parameters, dict):
        errors.append({"code": "creative_parameters_invalid", "message": f"直接媒体操作 {index} 的参数不是对象。"})
        return
    record_frame = _integer(parameters.get("record_frame"))
    duration = _number(parameters.get("duration_seconds"))
    source_in = _number(parameters.get("source_in_seconds", 0))
    track_key = "audio_track" if mechanism == "audio_asset" else "video_track"
    track = _integer(parameters.get(track_key))
    if record_frame is None or record_frame < 0:
        errors.append({"code": "creative_record_frame_invalid", "message": f"直接媒体操作 {index} 的 record_frame 必须为非负整数。"})
    if duration is None or duration <= 0:
        errors.append({"code": "creative_duration_invalid", "message": f"直接媒体操作 {index} 必须提供正 duration_seconds。"})
    if source_in is None or source_in < 0:
        errors.append({"code": "creative_source_range_invalid", "message": f"直接媒体操作 {index} 的 source_in_seconds 不合法。"})
    if track is None or track < 1:
        errors.append({"code": "creative_track_invalid", "message": f"直接媒体操作 {index} 的 {track_key} 必须为正整数。"})


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
