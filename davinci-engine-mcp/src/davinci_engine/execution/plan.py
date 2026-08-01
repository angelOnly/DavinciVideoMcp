"""ResolveExecutionPlan 的精确代码 Schema。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from davinci_engine.common import ensure_within_workspace, plan_digest


class ExecutionPlanError(ValueError):
    pass


@dataclass(frozen=True)
class ClipPlacement:
    asset_id: str
    content_hash: str
    local_path: str
    source_in_seconds: float
    source_out_seconds: float
    record_frame: int
    video_track: int = 1
    audio_track: int = 1
    include_audio: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClipPlacement":
        required = {
            "asset_id",
            "content_hash",
            "local_path",
            "source_in_seconds",
            "source_out_seconds",
            "record_frame",
        }
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - (required | {"video_track", "audio_track", "include_audio"}))
        if missing or unknown:
            raise ExecutionPlanError(f"片段计划字段不合法；缺少={missing}，未知={unknown}")
        item = cls(
            asset_id=str(value["asset_id"]),
            content_hash=str(value["content_hash"]),
            local_path=str(value["local_path"]),
            source_in_seconds=float(value["source_in_seconds"]),
            source_out_seconds=float(value["source_out_seconds"]),
            record_frame=int(value["record_frame"]),
            video_track=int(value.get("video_track", 1)),
            audio_track=int(value.get("audio_track", 1)),
            include_audio=bool(value.get("include_audio", True)),
        )
        if not item.asset_id or not item.content_hash:
            raise ExecutionPlanError("片段必须具有 asset_id 与内容哈希。")
        if item.source_in_seconds < 0 or item.source_out_seconds <= item.source_in_seconds:
            raise ExecutionPlanError("片段源范围必须是非负且出点大于入点。")
        if item.record_frame < 0 or item.video_track < 1 or item.audio_track < 1:
            raise ExecutionPlanError("目标帧和轨道索引必须为合法正值。")
        return item

    def path(self) -> Path:
        return ensure_within_workspace(Path(self.local_path))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolveExecutionPlan:
    project_id: str
    run_id: str
    project_name: str
    timeline_name: str
    timeline_fps: float
    width: int
    height: int
    clips: tuple[ClipPlacement, ...]
    render_path: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResolveExecutionPlan":
        required = {
            "project_id",
            "run_id",
            "project_name",
            "timeline_name",
            "timeline_fps",
            "width",
            "height",
            "clips",
            "render_path",
        }
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        if missing or unknown:
            raise ExecutionPlanError(f"执行计划字段不合法；缺少={missing}，未知={unknown}")
        raw_clips = value["clips"]
        if not isinstance(raw_clips, list) or not raw_clips:
            raise ExecutionPlanError("执行计划至少需要一个片段。")
        plan = cls(
            project_id=str(value["project_id"]),
            run_id=str(value["run_id"]),
            project_name=str(value["project_name"]),
            timeline_name=str(value["timeline_name"]),
            timeline_fps=float(value["timeline_fps"]),
            width=int(value["width"]),
            height=int(value["height"]),
            clips=tuple(ClipPlacement.from_dict(item) for item in raw_clips),
            render_path=str(value["render_path"]),
        )
        if not all((plan.project_id, plan.run_id, plan.project_name, plan.timeline_name)):
            raise ExecutionPlanError("项目、运行和时间线名称不能为空。")
        if plan.timeline_fps <= 0 or plan.width <= 0 or plan.height <= 0:
            raise ExecutionPlanError("时间线帧率与画幅必须为正数。")
        plan.render_file()
        return plan

    def render_file(self) -> Path:
        output = ensure_within_workspace(Path(self.render_path))
        if output.suffix.lower() != ".mp4":
            raise ExecutionPlanError("首期只允许输出 MP4 渲染文件。")
        return output

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "project_name": self.project_name,
            "timeline_name": self.timeline_name,
            "timeline_fps": self.timeline_fps,
            "width": self.width,
            "height": self.height,
            "clips": [clip.to_dict() for clip in self.clips],
            "render_path": self.render_path,
        }

    @property
    def digest(self) -> str:
        return plan_digest(self.to_dict())

    @property
    def expected_duration_seconds(self) -> float:
        return max(
            (clip.record_frame / self.timeline_fps) + (clip.source_out_seconds - clip.source_in_seconds)
            for clip in self.clips
        )

