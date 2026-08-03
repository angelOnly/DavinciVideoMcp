"""媒体外部能力的 Port 合同；核心模块只依赖这些结构化结果。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class TranscriberPort(Protocol):
    """本地转写实现必须提供带来源和时间范围的可审计结果。"""

    def health_check(self) -> dict[str, Any]: ...

    def transcribe(self, audio_path: Path, *, source_content_hash: str) -> dict[str, Any]: ...

    def identity(self) -> dict[str, Any]: ...


class MultimodalAnalyzerPort(Protocol):
    """云端多模态实现只输出观察，不输出剪辑决定。"""

    def health_check(self) -> dict[str, Any]: ...

    def analyze_video_segment(
        self,
        video_path: Path,
        *,
        asset_id: str,
        source_start_seconds: float,
        source_end_seconds: float,
        transcript_context: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def analyze_image_evidence(
        self,
        image_paths: list[Path],
        *,
        asset_id: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def review_video_segment(
        self,
        video_path: Path,
        *,
        stage: str,
        source_start_seconds: float,
        source_end_seconds: float,
        context: dict[str, Any],
    ) -> dict[str, Any]: ...

    def identity(self) -> dict[str, Any]: ...


class FrameEvidenceAnalyzerPort(Protocol):
    """Codex 只读取已抽取图片，结果必须声明帧采样限制。"""

    def health_check(self) -> dict[str, Any]: ...

    def analyze_frames(
        self,
        image_paths: list[Path],
        *,
        asset_id: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
        project_id: str | None = None,
    ) -> dict[str, Any]: ...

    def review_render_frames(
        self,
        image_paths: list[Path],
        *,
        stage: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
        context: dict[str, Any],
        project_id: str | None = None,
    ) -> dict[str, Any]: ...

    def identity(self) -> dict[str, Any]: ...
