"""供 Codex 受控调用使用的窄只读项目证据快照。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from davinci_app.common import ensure_within


class ProjectEvidenceScope:
    """不暴露数据库、完整素材库或原始视频路径的最小证据接口。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def snapshot(self, run: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        assets = []
        for asset in (run.get("input_snapshot") or {}).get("assets") or []:
            if not isinstance(asset, dict):
                continue
            probe = asset.get("probe") if isinstance(asset.get("probe"), dict) else {}
            assets.append(
                {
                    "asset_id": asset.get("id"),
                    "role": asset.get("role"),
                    "original_name": asset.get("original_name"),
                    "duration_seconds": probe.get("duration_seconds"),
                    "has_video": bool(probe.get("video_streams")),
                    "has_audio": bool(probe.get("audio_streams")),
                }
            )
        return {
            "scope": "project_evidence_read_only_v1",
            "project_id": run.get("project_id"),
            "brief": _safe_brief((run.get("input_snapshot") or {}).get("brief")),
            "assets": assets,
            "evidence": _safe_evidence(evidence),
            "limitations": [
                "此快照只含已生成的证据；不得读取未列出的文件或完整素材库。",
                "Codex 未直接观看原始视频或音频；连续声画判断只能引用多模态证据。",
            ],
        }

    def local_images(self, evidence: dict[str, Any], *, maximum: int = 8) -> list[tuple[Path, float]]:
        """只给 Skill 附加少量已记录的抽帧，避免把整个素材库交给模型。"""
        selected: list[tuple[Path, float]] = []
        for asset in evidence.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            for frame in asset.get("frames") or []:
                if not isinstance(frame, dict):
                    continue
                raw_path = frame.get("path")
                if not isinstance(raw_path, str):
                    continue
                try:
                    path = ensure_within(Path(raw_path), self.project_root)
                except ValueError:
                    continue
                if path.exists() and path.is_file():
                    try:
                        moment = float(frame.get("time_seconds"))
                    except (TypeError, ValueError):
                        continue
                    selected.append((path, moment))
                    if len(selected) >= maximum:
                        return selected
        return selected


def _safe_brief(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    # 密钥只存在环境变量；仍只保留剪辑决策有关的原始用户字段。
    return {str(key): item for key, item in value.items() if "key" not in str(key).lower() and "token" not in str(key).lower()}


def _safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    assets = []
    for item in evidence.get("assets") or []:
        if not isinstance(item, dict):
            continue
        assets.append(
            {
                "asset_id": item.get("asset_id"),
                "content_hash": item.get("source_content_hash"),
                "duration_seconds": item.get("duration_seconds"),
                "transcript": item.get("transcript"),
                "vad_segments": item.get("vad_segments"),
                "scene_candidates": item.get("scene_candidates"),
                "silence_candidates": item.get("silence_candidates"),
                "loudness": item.get("loudness"),
                "multimodal": item.get("multimodal"),
                "codex_frame_evidence": item.get("codex_frame_evidence"),
                "coverage": item.get("coverage"),
            }
        )
    return {
        "analysis_mode": evidence.get("analysis_mode"),
        "assets": assets,
        "conflicts": evidence.get("conflicts") or [],
        "limitations": evidence.get("limitations") or [],
    }
