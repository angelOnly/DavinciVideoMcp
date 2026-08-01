"""最小本地 Evidence Bundle：技术、帧、音频与确定性声音证据可回链。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from davinci_app.common import run_command, utc_now
from davinci_app.media.probe import MediaProbeError, extract_audio, ffmpeg_path


class EvidenceBuildError(RuntimeError):
    pass


class EvidenceBuilder:
    VERSION = "local-evidence-v1"

    def build(self, asset: dict[str, Any], evidence_root: Path) -> dict[str, Any]:
        """只复用校验后工作副本，绝不重新接收或替换原始素材。"""
        source = Path(asset["working_path"])
        if not source.exists():
            raise EvidenceBuildError("无法找到已验证的稳定工作副本。")
        evidence_dir = evidence_root / asset["id"]
        frames_dir = evidence_dir / "frames"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        probe = asset["probe"]
        duration = float(probe.get("duration_seconds") or 0)
        frame_paths = self._extract_overview_frames(source, frames_dir, duration) if probe.get("video_streams") else []
        audio_path = None
        silence = []
        loudness: dict[str, Any] | None = None
        if probe.get("audio_streams"):
            audio_path = evidence_dir / "analysis-audio.wav"
            extract_audio(source, audio_path)
            silence = self._detect_silence(source)
            loudness = self._measure_loudness(source)
        scene_candidates = self._scene_candidates(source) if probe.get("video_streams") else []
        manifest = {
            "generator": self.VERSION,
            "generated_at": utc_now(),
            "asset_id": asset["id"],
            "source_content_hash": asset["content_hash"],
            "working_content_hash": asset["working_hash"],
            "source_path": str(source),
            "probe": probe,
            "analysis_mode": "local_deterministic_only",
            "frames": [str(path) for path in frame_paths],
            "audio_path": str(audio_path) if audio_path else None,
            "scene_candidates": scene_candidates,
            "silence_candidates": silence,
            "loudness": loudness,
            "multimodal": {"mode": "not_requested", "observations": []},
            "transcript": {"mode": "not_requested", "segments": []},
        }
        manifest_path = evidence_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"manifest_path": str(manifest_path), "manifest": manifest}

    def _extract_overview_frames(self, source: Path, frames_dir: Path, duration: float) -> list[Path]:
        moments = [0.25] if duration <= 1 else [duration * ratio for ratio in (0.08, 0.33, 0.58, 0.83)]
        output: list[Path] = []
        for index, moment in enumerate(moments):
            path = frames_dir / f"overview-{index + 1:02d}-{moment:.2f}s.jpg"
            command = [
                ffmpeg_path(),
                "-y",
                "-v",
                "error",
                "-ss",
                f"{moment:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(path),
            ]
            completed = run_command(command, timeout_seconds=60)
            if completed.returncode == 0 and path.exists():
                output.append(path)
        return output

    def _scene_candidates(self, source: Path) -> list[dict[str, Any]]:
        command = [
            ffmpeg_path(),
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(source),
            "-vf",
            "select='gt(scene,0.30)',showinfo",
            "-vsync",
            "vfr",
            "-f",
            "null",
            "-",
        ]
        completed = run_command(command, timeout_seconds=240)
        matches = re.findall(r"pts_time:([0-9.]+)", completed.stderr)
        return [
            {"time_seconds": float(value), "method": "ffmpeg_scene_threshold", "threshold": 0.30}
            for value in matches[:80]
        ]

    def _detect_silence(self, source: Path) -> list[dict[str, Any]]:
        command = [
            ffmpeg_path(),
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(source),
            "-af",
            "silencedetect=noise=-35dB:d=0.5",
            "-f",
            "null",
            "-",
        ]
        completed = run_command(command, timeout_seconds=240)
        starts = [float(item) for item in re.findall(r"silence_start: ([0-9.]+)", completed.stderr)]
        ends = [float(item) for item in re.findall(r"silence_end: ([0-9.]+)", completed.stderr)]
        candidates = []
        for index, start in enumerate(starts):
            if index < len(ends):
                candidates.append(
                    {
                        "start_seconds": start,
                        "end_seconds": ends[index],
                        "method": "ffmpeg_silencedetect",
                        "interpretation": "候选；不得自动删除。",
                    }
                )
        return candidates

    def _measure_loudness(self, source: Path) -> dict[str, Any] | None:
        command = [
            ffmpeg_path(),
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(source),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
        completed = run_command(command, timeout_seconds=240)
        match = re.search(r"\{\s*\"input_i\".*?\}", completed.stderr, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return {"method": "ffmpeg_loudnorm", "measurement": payload, "interpretation": "技术测量，不等于混音决定。"}

