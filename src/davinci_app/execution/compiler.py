"""将明确的 Engine 冒烟预设编译为确定性 ResolveExecutionPlan。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from davinci_app.common import digest_json


class CompilationError(ValueError):
    pass


class EngineSmokeCompiler:
    """仅用于 Engine 冒烟测试的保守编译器，绝不替代专业 EditPlan。"""

    def __init__(self, resolve_workspace_project: str = "DavinciMcp_Workspace") -> None:
        if not resolve_workspace_project.startswith("DavinciMcp_"):
            raise CompilationError("Resolve 工作项目必须以 DavinciMcp_ 开头。")
        self.resolve_workspace_project = resolve_workspace_project

    def compile(self, run: dict[str, Any], project_paths: dict[str, Path]) -> dict[str, Any]:
        if run.get("kind") != "engine_smoke":
            raise CompilationError("EngineSmokeCompiler 只能处理 engine_smoke 运行，不能用于正式剪辑。")
        snapshot = run["input_snapshot"]
        brief = snapshot.get("brief") or {}
        preset = brief.get("testing_preset")
        if preset not in {"fragment_montage", "interview_excerpt"}:
            raise CompilationError("当前最小编译器只支持 fragment_montage 或 interview_excerpt 测试预设。")
        fps = float(brief.get("timeline_fps", 30))
        if fps <= 0:
            raise CompilationError("timeline_fps 必须为正数。")
        width, height = self._resolution(brief.get("orientation", "portrait"))
        clips = []
        cursor = 0
        assets = snapshot.get("assets") or []
        if preset == "fragment_montage":
            maximum = float(brief.get("max_clip_seconds", 8))
            for asset in assets:
                clip = self._montage_clip(asset, cursor, maximum)
                clips.append(clip)
                cursor += math.ceil((clip["source_out_seconds"] - clip["source_in_seconds"]) * fps)
        else:
            asset = assets[0] if assets else None
            if not asset:
                raise CompilationError("采访测试预设需要一个主素材。")
            clip = self._interview_clip(asset, cursor, float(brief.get("max_duration_seconds", 90)))
            clips.append(clip)
        if not clips:
            raise CompilationError("没有可编译的测试片段。")
        plan = {
            "project_id": snapshot["project_id"],
            "run_id": run["id"],
            # 每次运行仍使用独立时间线；共享的受管 Resolve 项目永不指向用户项目。
            "project_name": self.resolve_workspace_project,
            "timeline_name": f"run_{run['id'][:16]}_smoke",
            "timeline_fps": fps,
            "width": width,
            "height": height,
            "clips": clips,
            # 技术预览与正式工作版使用不同文件名，避免被误当成候选基线。
            "render_path": str(project_paths["renders"] / run["id"] / "technical-preview.mp4"),
        }
        plan_path = project_paths["plans"] / f"{run['id']}.resolve-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"plan": plan, "plan_path": str(plan_path), "plan_digest": digest_json(plan)}

    @staticmethod
    def _montage_clip(asset: dict[str, Any], record_frame: int, maximum: float) -> dict[str, Any]:
        probe = asset.get("probe") or {}
        duration = float(probe.get("duration_seconds") or 0)
        if duration < 1:
            raise CompilationError(f"素材时长不足 1 秒：{asset['original_name']}")
        source_in = 0.35 if duration > 3 else 0.0
        source_out = min(duration - 0.15, source_in + maximum)
        if source_out <= source_in:
            source_in, source_out = 0.0, duration
        return EngineSmokeCompiler._placement(asset, source_in, source_out, record_frame)

    @staticmethod
    def _interview_clip(asset: dict[str, Any], record_frame: int, maximum: float) -> dict[str, Any]:
        probe = asset.get("probe") or {}
        duration = float(probe.get("duration_seconds") or 0)
        if duration < 1:
            raise CompilationError("采访素材没有可用时长。")
        # 没有转写与审批简报时，不伪造语义精选；只生成可验证的开场节选。
        source_in = 0.0
        source_out = min(duration, max(1.0, maximum))
        return EngineSmokeCompiler._placement(asset, source_in, source_out, record_frame)

    @staticmethod
    def _placement(asset: dict[str, Any], source_in: float, source_out: float, record_frame: int) -> dict[str, Any]:
        probe = asset.get("probe") or {}
        return {
            "asset_id": asset["id"],
            "content_hash": asset["working_hash"],
            "local_path": asset["working_path"],
            "source_in_seconds": round(source_in, 3),
            "source_out_seconds": round(source_out, 3),
            "record_frame": record_frame,
            "video_track": 1,
            "audio_track": 1,
            "include_audio": bool(probe.get("audio_streams")),
        }

    @staticmethod
    def _resolution(orientation: str) -> tuple[int, int]:
        if orientation == "landscape":
            return 1280, 720
        if orientation == "square":
            return 1080, 1080
        return 1080, 1920
