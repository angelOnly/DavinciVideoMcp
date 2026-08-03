"""把已批准的专业 EditPlan 严格映射为当前 Engine 已支持的执行计划。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from davinci_app.common import digest_json
from davinci_app.config import AppConfig
from davinci_app.editorial.pipeline import ProfessionalPreproduction


class ProfessionalCompilationError(ValueError):
    """专业计划存在当前 Engine 尚未认证的执行需求。"""


class ProfessionalExecutionCompiler:
    """只消费真实 EditPlan，绝不根据素材顺序或固定秒数自行选片。"""

    _CLIP_FIELDS = {
        "asset_id",
        "source_in_seconds",
        "source_out_seconds",
        "record_frame",
        "video_track",
        "audio_track",
        "include_audio",
    }

    def __init__(self, config: AppConfig) -> None:
        self.resolve_workspace_project = config.resolve_workspace_project

    def compile_work_preview(
        self,
        run: dict[str, Any],
        project_paths: dict[str, Path],
        preproduction: ProfessionalPreproduction,
    ) -> dict[str, Any]:
        return self._compile(run, project_paths, preproduction, stage="work-preview")

    def compile_candidate(
        self,
        run: dict[str, Any],
        project_paths: dict[str, Path],
        preproduction: ProfessionalPreproduction,
        finishing: dict[str, Any],
    ) -> dict[str, Any]:
        """收尾只能带来已认证 Mapping 支持的明确执行变更。"""
        if finishing.get("edit_plan_digest") != preproduction.edit_plan_digest:
            raise ProfessionalCompilationError("收尾方案与当前 EditPlan 不属于同一基线。")
        changes = finishing.get("execution_changes")
        if not isinstance(changes, list):
            raise ProfessionalCompilationError("收尾方案必须明确 execution_changes；空数组表示无需可执行变更。")
        if changes:
            raise ProfessionalCompilationError(
                "当前 Engine 尚无这些收尾调整的认证 Compiler Mapping，已拒绝把它们静默忽略。"
            )
        return self._compile(run, project_paths, preproduction, stage="candidate")

    def _compile(
        self,
        run: dict[str, Any],
        project_paths: dict[str, Path],
        preproduction: ProfessionalPreproduction,
        *,
        stage: str,
    ) -> dict[str, Any]:
        if run.get("kind") != "initial_edit":
            raise ProfessionalCompilationError("专业执行编译器只能处理 initial_edit 运行。")
        binding = preproduction.capability_binding
        if binding.get("edit_plan_digest") != preproduction.edit_plan_digest:
            raise ProfessionalCompilationError("CapabilityBinding 与 EditPlan 摘要不一致。")
        bindings = binding.get("bindings")
        if not isinstance(bindings, list):
            raise ProfessionalCompilationError("CapabilityBinding 缺少受管绑定列表。")
        binding_by_id = {
            str(item.get("capability_id")): item
            for item in bindings
            if isinstance(item, dict) and isinstance(item.get("capability_id"), str)
        }
        if len(binding_by_id) != len(bindings):
            raise ProfessionalCompilationError("CapabilityBinding 包含无效或重复的能力 ID。")

        execution = preproduction.edit_plan.get("execution")
        if not isinstance(execution, dict) or execution.get("kind") != "resolve_source_clips_v1":
            raise ProfessionalCompilationError(
                "EditPlan 必须提供由专业流程明确批准的 resolve_source_clips_v1 执行映射。"
            )
        operations = execution.get("operations")
        clips = self._compile_clips(execution.get("clips"), run)
        creative_operations = self._compile_creative_operations(operations, binding_by_id, clips)
        fps = _positive_number(execution.get("timeline_fps"), "execution.timeline_fps")
        width = _positive_integer(execution.get("width"), "execution.width")
        height = _positive_integer(execution.get("height"), "execution.height")
        render_path = project_paths["renders"] / run["id"] / f"{stage}.mp4"
        render_path.parent.mkdir(parents=True, exist_ok=True)
        plan = {
            "project_id": run["project_id"],
            "run_id": run["id"],
            "project_name": self.resolve_workspace_project,
            "timeline_name": f"run_{run['id'][:16]}_{stage.replace('-', '_')}",
            "timeline_fps": fps,
            "width": width,
            "height": height,
            "clips": clips,
            "creative_operations": creative_operations,
            "render_path": str(render_path),
        }
        plan_path = project_paths["plans"] / f"{run['id']}.{stage}.resolve-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "plan": plan,
            "plan_path": str(plan_path),
            "plan_digest": digest_json(plan),
            "stage": stage,
        }

    def _compile_clips(self, raw_clips: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw_clips, list) or not raw_clips:
            raise ProfessionalCompilationError("专业 EditPlan 至少需要一个明确选定的执行片段。")
        assets = {
            str(asset["id"]): asset
            for asset in (run.get("input_snapshot") or {}).get("assets") or []
            if isinstance(asset, dict) and asset.get("id")
        }
        compiled: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_clips):
            if not isinstance(raw, dict):
                raise ProfessionalCompilationError(f"执行片段 {index} 必须是对象。")
            missing = sorted(self._CLIP_FIELDS - set(raw))
            unknown = sorted(set(raw) - self._CLIP_FIELDS)
            if missing or unknown:
                raise ProfessionalCompilationError(f"执行片段 {index} 字段不合法；缺少={missing}，未知={unknown}。")
            asset = assets.get(str(raw["asset_id"]))
            if asset is None:
                raise ProfessionalCompilationError(f"执行片段 {index} 引用了未冻结的素材。")
            source_in = _non_negative_number(raw["source_in_seconds"], f"执行片段 {index} 的 source_in_seconds")
            source_out = _positive_number(raw["source_out_seconds"], f"执行片段 {index} 的 source_out_seconds")
            if source_out <= source_in:
                raise ProfessionalCompilationError(f"执行片段 {index} 的源出点必须大于入点。")
            compiled.append(
                {
                    "asset_id": asset["id"],
                    # 路径与哈希只来自冻结输入，Skill 不能注入任意本地文件。
                    "content_hash": asset["working_hash"],
                    "local_path": asset["working_path"],
                    "source_in_seconds": source_in,
                    "source_out_seconds": source_out,
                    "record_frame": _non_negative_integer(raw["record_frame"], f"执行片段 {index} 的 record_frame"),
                    "video_track": _positive_integer(raw["video_track"], f"执行片段 {index} 的 video_track"),
                    "audio_track": _positive_integer(raw["audio_track"], f"执行片段 {index} 的 audio_track"),
                    "include_audio": _bool(raw["include_audio"], f"执行片段 {index} 的 include_audio"),
                }
            )
        return compiled

    def _compile_creative_operations(
        self,
        raw_operations: Any,
        binding_by_id: dict[str, dict[str, Any]],
        clips: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """只把有明确认证 Mapping 的少数机制编译成 Engine 操作。"""

        if not isinstance(raw_operations, list):
            raise ProfessionalCompilationError("EditPlan 的 execution.operations 必须是数组。")
        compiled: list[dict[str, Any]] = []
        used_capabilities: set[str] = set()
        for index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict):
                raise ProfessionalCompilationError(f"创意操作 {index} 必须是对象。")
            # 严格 JSON Schema 为不同操作共用一个闭合对象，非本机制字段以 null 返回；
            # 编译前先去除这些 null，再按实际机制执行精确字段检查。
            raw = {key: value for key, value in raw.items() if value is not None}
            kind = raw.get("kind")
            capability_id = raw.get("capability_id")
            if not isinstance(kind, str) or not isinstance(capability_id, str):
                raise ProfessionalCompilationError(f"创意操作 {index} 必须包含 kind 和 capability_id。")
            binding = binding_by_id.get(capability_id)
            if binding is None:
                raise ProfessionalCompilationError(f"创意操作 {index} 引用了未绑定或未认证的能力。")
            if capability_id in used_capabilities:
                raise ProfessionalCompilationError(f"同一能力 {capability_id} 不能在一个最小执行计划中重复应用。")
            mechanism = str(binding.get("mechanism") or "")
            base = {
                "capability_id": capability_id,
                "mechanism": mechanism,
                "content_hash": str(binding.get("content_hash") or ""),
                "local_path": str(binding.get("cache_path") or ""),
            }
            if not base["content_hash"] or not base["local_path"]:
                raise ProfessionalCompilationError(f"能力 {capability_id} 缺少已本地化的内容身份。")
            if kind == "place_audio_asset":
                self._require_exact_fields(
                    raw,
                    {"kind", "capability_id", "record_frame", "duration_seconds", "source_in_seconds", "audio_track"},
                    index,
                )
                if mechanism != "audio_asset":
                    raise ProfessionalCompilationError(f"能力 {capability_id} 不是 audio_asset，不能作为音频放置。")
                parameters = {
                    "record_frame": _non_negative_integer(raw["record_frame"], f"创意操作 {index} 的 record_frame"),
                    "duration_seconds": _positive_number(raw["duration_seconds"], f"创意操作 {index} 的 duration_seconds"),
                    "source_in_seconds": _non_negative_number(raw["source_in_seconds"], f"创意操作 {index} 的 source_in_seconds"),
                    "audio_track": _positive_integer(raw["audio_track"], f"创意操作 {index} 的 audio_track"),
                }
            elif kind == "place_visual_asset":
                self._require_exact_fields(
                    raw,
                    {"kind", "capability_id", "record_frame", "duration_seconds", "source_in_seconds", "video_track"},
                    index,
                )
                if mechanism not in {"image_asset", "video_asset", "video_overlay"}:
                    raise ProfessionalCompilationError(f"能力 {capability_id} 不是可放置的认证视觉媒体。")
                parameters = {
                    "record_frame": _non_negative_integer(raw["record_frame"], f"创意操作 {index} 的 record_frame"),
                    "duration_seconds": _positive_number(raw["duration_seconds"], f"创意操作 {index} 的 duration_seconds"),
                    "source_in_seconds": _non_negative_number(raw["source_in_seconds"], f"创意操作 {index} 的 source_in_seconds"),
                    "video_track": _positive_integer(raw["video_track"], f"创意操作 {index} 的 video_track"),
                }
            elif kind == "apply_lut_3d":
                self._require_exact_fields(raw, {"kind", "capability_id", "target_clip_index"}, index)
                if mechanism != "lut_3d":
                    raise ProfessionalCompilationError(f"能力 {capability_id} 不是 lut_3d，不能作为调色操作。")
                target = _non_negative_integer(raw["target_clip_index"], f"创意操作 {index} 的 target_clip_index")
                if target >= len(clips):
                    raise ProfessionalCompilationError(f"创意操作 {index} 指向不存在的目标片段。")
                constraints = binding.get("constraints")
                if not isinstance(constraints, dict):
                    raise ProfessionalCompilationError(f"能力 {capability_id} 的认证约束损坏。")
                deployment = constraints.get("deployment")
                relative = (
                    deployment.get("installed_relative_path")
                    if isinstance(deployment, dict)
                    else constraints.get("installed_relative_path")
                )
                if not isinstance(relative, str) or not relative:
                    raise ProfessionalCompilationError(
                        f"能力 {capability_id} 没有经认证的 Resolve LUT 安装相对路径。"
                    )
                parameters = {"target_clip_index": target, "installed_relative_path": relative}
            else:
                raise ProfessionalCompilationError(
                    f"创意操作 {index} 的 kind={kind!r} 没有认证 Compiler Mapping，不能静默跳过。"
                )
            compiled.append({**base, "parameters": parameters})
            used_capabilities.add(capability_id)
        unused = sorted(set(binding_by_id) - used_capabilities)
        if unused:
            raise ProfessionalCompilationError(
                f"已绑定能力没有精确执行操作：{', '.join(unused)}；拒绝把它们静默省略。"
            )
        return compiled

    @staticmethod
    def _require_exact_fields(raw: dict[str, Any], expected: set[str], index: int) -> None:
        missing = sorted(expected - set(raw))
        unknown = sorted(set(raw) - expected)
        if missing or unknown:
            raise ProfessionalCompilationError(f"创意操作 {index} 字段不合法；缺少={missing}，未知={unknown}。")


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProfessionalCompilationError(f"{label} 必须是正数。") from exc
    if number <= 0:
        raise ProfessionalCompilationError(f"{label} 必须是正数。")
    return number


def _non_negative_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProfessionalCompilationError(f"{label} 必须是非负数。") from exc
    if number < 0:
        raise ProfessionalCompilationError(f"{label} 必须是非负数。")
    return number


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ProfessionalCompilationError(f"{label} 必须是正整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfessionalCompilationError(f"{label} 必须是正整数。") from exc
    if number <= 0 or number != value:
        raise ProfessionalCompilationError(f"{label} 必须是正整数。")
    return number


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ProfessionalCompilationError(f"{label} 必须是非负整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfessionalCompilationError(f"{label} 必须是非负整数。") from exc
    if number < 0 or number != value:
        raise ProfessionalCompilationError(f"{label} 必须是非负整数。")
    return number


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProfessionalCompilationError(f"{label} 必须是布尔值。")
    return value
