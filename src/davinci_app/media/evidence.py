"""媒体证据构建、融合与补充循环；只产出事实证据，不作剪辑决定。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from davinci_app.common import digest_json, run_command, sha256_file, utc_now
from davinci_app.config import AppConfig
from davinci_app.media.contracts import FrameEvidenceAnalyzerPort, MultimodalAnalyzerPort, TranscriberPort
from davinci_app.media.probe import MediaProbeError, extract_audio, ffmpeg_path, probe_media


class EvidenceBuildError(RuntimeError):
    """确定性本地证据无法生成。"""


class EvidenceCompletionError(RuntimeError):
    """完整证据链缺少已实测的外部能力或用户授权。"""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("；".join(reasons))


class EvidenceBuilder:
    """复用已验证工作副本，生成代理、抽帧、镜头与确定性声音证据。"""

    VERSION = "local-evidence-v3"

    def build(self, asset: dict[str, Any], evidence_root: Path, *, proxy_root: Path | None = None) -> dict[str, Any]:
        source = Path(asset["working_path"])
        if not source.exists():
            raise EvidenceBuildError("无法找到已验证的稳定工作副本。")
        evidence_dir = evidence_root / asset["id"]
        manifest_path = evidence_dir / "manifest.json"
        cached = self._read_cache(manifest_path, asset)
        if cached is not None:
            return {"manifest_path": str(manifest_path), "manifest": cached}

        frames_dir = evidence_dir / "frames"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        probe = asset["probe"]
        duration = float(probe.get("duration_seconds") or 0)
        analysis_proxy = None
        analysis_source = source
        if probe.get("video_streams"):
            destination_root = proxy_root or evidence_dir / "proxy"
            analysis_proxy = destination_root / f"{asset['id']}.analysis.mp4"
            self._make_analysis_proxy(source, analysis_proxy)
            analysis_source = analysis_proxy
        frame_records = self._extract_overview_frames(analysis_source, frames_dir, duration) if probe.get("video_streams") else []
        contact_sheet = self._make_contact_sheet(analysis_source, evidence_dir, duration) if probe.get("video_streams") else None
        audio_path = None
        silence: list[dict[str, Any]] = []
        loudness: dict[str, Any] | None = None
        if probe.get("audio_streams"):
            audio_path = evidence_dir / "analysis-audio.wav"
            extract_audio(source, audio_path)
            silence = self._detect_silence(source)
            loudness = self._measure_loudness(source)
        scene_candidates = self._scene_candidates(analysis_source) if probe.get("video_streams") else []
        manifest = {
            "generator": self.VERSION,
            "generated_at": utc_now(),
            "asset_id": asset["id"],
            "source_content_hash": asset["content_hash"],
            "working_content_hash": asset["working_hash"],
            "working_path": str(source),
            "probe": probe,
            "analysis_proxy_path": str(analysis_proxy) if analysis_proxy else None,
            "analysis_proxy_hash": sha256_file(analysis_proxy) if analysis_proxy else None,
            "frames": frame_records,
            "contact_sheet_path": str(contact_sheet) if contact_sheet else None,
            "audio_path": str(audio_path) if audio_path else None,
            "scene_candidates": scene_candidates,
            "silence_candidates": silence,
            "loudness": loudness,
            "analysis_mode": "local_deterministic_only",
            "multimodal": {"mode": "not_requested", "observations": []},
            "transcript": {"mode": "not_requested", "segments": []},
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"manifest_path": str(manifest_path), "manifest": manifest}

    def build_dense_window(
        self,
        manifest: dict[str, Any],
        *,
        start_seconds: float,
        end_seconds: float,
        maximum_duration_seconds: float = 8.0,
    ) -> dict[str, Any]:
        """只为明确缺口生成短窗口，稀疏帧从不充当精确切点事实。"""
        source = Path(str(manifest.get("working_path") or ""))
        if not source.exists():
            raise EvidenceBuildError("无法找到生成密集窗口所需的工作副本。")
        duration = float((manifest.get("probe") or {}).get("duration_seconds") or 0)
        if start_seconds < 0 or end_seconds <= start_seconds or end_seconds > duration + 0.01:
            raise EvidenceBuildError("密集窗口请求超出素材时间范围。")
        expanded_start = max(0.0, start_seconds - 0.5)
        expanded_end = min(duration, end_seconds + 0.5)
        if expanded_end - expanded_start > maximum_duration_seconds:
            raise EvidenceBuildError("单次密集窗口不得超过 8 秒；请请求更小的可判定范围。")
        evidence_dir = Path(str(manifest.get("working_path"))).parent.parent / "evidence" / str(manifest["asset_id"])
        # 若工作副本路径层级被未来存储替换，回退到本 manifest 的既有帧目录即可。
        existing_frames = manifest.get("frames") or []
        if existing_frames and isinstance(existing_frames[0], dict):
            evidence_dir = Path(str(existing_frames[0].get("path"))).parent.parent
        dense_dir = evidence_dir / "dense-windows" / f"{expanded_start:.3f}-{expanded_end:.3f}"
        dense_dir.mkdir(parents=True, exist_ok=True)
        moments = _dense_moments(expanded_start, expanded_end, interval=0.25)
        frames = []
        for index, moment in enumerate(moments):
            path = dense_dir / f"dense-{index + 1:03d}-{moment:.3f}s.jpg"
            if self._extract_frame(source, path, moment):
                frames.append({"path": str(path), "time_seconds": round(moment, 3), "layer": "dense"})
        segment_path = dense_dir / "analysis-window.mp4"
        self._make_video_segment(source, segment_path, expanded_start, expanded_end - expanded_start)
        return {
            "asset_id": manifest["asset_id"],
            "source_range_seconds": [round(expanded_start, 3), round(expanded_end, 3)],
            "frames": frames,
            "video_segment_path": str(segment_path),
            "video_segment_hash": sha256_file(segment_path),
            "reason": "按 SourceUnderstanding 明确请求生成；只用于该时间范围复核。",
        }

    def make_video_segment(self, source: Path, target: Path, start_seconds: float, duration_seconds: float) -> None:
        self._make_video_segment(source, target, start_seconds, duration_seconds)

    def extract_timed_frames(
        self,
        source: Path,
        target_dir: Path,
        *,
        duration_seconds: float,
        maximum_frames: int = 8,
        prefix: str = "review",
    ) -> list[dict[str, Any]]:
        """为抽帧复核均匀取代表画面，不把它们误称为连续视频观察。"""
        if not source.exists():
            raise EvidenceBuildError("无法找到抽帧复核所需的视频文件。")
        if duration_seconds <= 0 or maximum_frames <= 0:
            raise EvidenceBuildError("抽帧复核缺少有效时长或帧数。")
        target_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for index, moment in enumerate(_representative_moments(duration_seconds, maximum_frames)):
            path = target_dir / f"{prefix}-{index + 1:02d}-{moment:.3f}s.jpg"
            if self._extract_frame(source, path, moment):
                records.append({"path": str(path), "time_seconds": moment, "layer": "review"})
        if not records:
            raise EvidenceBuildError("无法从待复核渲染提取代表帧。")
        return records

    def _read_cache(self, manifest_path: Path, asset: dict[str, Any]) -> dict[str, Any] | None:
        if not manifest_path.exists():
            return None
        try:
            cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(cached, dict):
            return None
        if (
            cached.get("generator") != self.VERSION
            or cached.get("source_content_hash") != asset.get("content_hash")
            or cached.get("working_content_hash") != asset.get("working_hash")
        ):
            return None
        paths = [cached.get("analysis_proxy_path"), cached.get("audio_path")]
        for frame in cached.get("frames") or []:
            paths.append(frame.get("path") if isinstance(frame, dict) else frame)
        if all(path is None or Path(str(path)).exists() for path in paths):
            return cached
        return None

    def _make_analysis_proxy(self, source: Path, target: Path) -> None:
        if target.exists() and target.stat().st_size > 0:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.stem}.partial{target.suffix}")
        command = [
            ffmpeg_path(),
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            "scale=960:-2:force_original_aspect_ratio=decrease",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(partial),
        ]
        completed = run_command(command, timeout_seconds=900)
        if completed.returncode != 0 or not partial.exists():
            partial.unlink(missing_ok=True)
            raise EvidenceBuildError(completed.stderr.strip()[-600:] or "无法生成分析代理。")
        partial.replace(target)

    def _extract_overview_frames(self, source: Path, frames_dir: Path, duration: float) -> list[dict[str, Any]]:
        """首版概览最多覆盖 12 个时间点；短素材只取一个稳定代表帧。"""
        moments = _representative_moments(duration, 1 if duration <= 1 else 12)
        records = []
        for index, moment in enumerate(moments):
            path = frames_dir / f"overview-{index + 1:02d}-{moment:.3f}s.jpg"
            if self._extract_frame(source, path, moment):
                records.append({"path": str(path), "time_seconds": round(moment, 3), "layer": "overview"})
        return records

    def _extract_frame(self, source: Path, path: Path, moment: float) -> bool:
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
        return completed.returncode == 0 and path.exists()

    def _make_contact_sheet(self, source: Path, evidence_dir: Path, duration: float) -> Path | None:
        target = evidence_dir / "contact-sheet.jpg"
        interval = max(1.0, duration / 12.0) if duration else 1.0
        command = [
            ffmpeg_path(),
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{interval:.3f},scale=320:-2,tile=4x3",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(target),
        ]
        completed = run_command(command, timeout_seconds=180)
        return target if completed.returncode == 0 and target.exists() else None

    def _make_video_segment(self, source: Path, target: Path, start_seconds: float, duration_seconds: float) -> None:
        if target.exists() and target.stat().st_size > 0:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg_path(),
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source),
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target),
        ]
        completed = run_command(command, timeout_seconds=600)
        if completed.returncode != 0 or not target.exists():
            raise EvidenceBuildError(completed.stderr.strip()[-600:] or "无法生成多模态分析片段。")

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
        return [
            {
                "start_seconds": start,
                "end_seconds": ends[index],
                "method": "ffmpeg_silencedetect",
                "interpretation": "候选；不得自动删除。",
            }
            for index, start in enumerate(starts)
            if index < len(ends)
        ]

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


class MediaEvidenceRuntime:
    """把本地确定性证据、FunASR、Gemini 与 Codex 抽帧观察融合为完整 Evidence Bundle。"""

    VERSION = "complete-evidence-v3"

    def __init__(
        self,
        config: AppConfig,
        transcriber: TranscriberPort,
        multimodal: MultimodalAnalyzerPort,
        frame_analyzer: FrameEvidenceAnalyzerPort,
        *,
        evidence_builder: EvidenceBuilder | None = None,
    ) -> None:
        self.config = config
        self.transcriber = transcriber
        self.multimodal = multimodal
        self.frame_analyzer = frame_analyzer
        self.evidence_builder = evidence_builder or EvidenceBuilder()

    def complete_evidence(
        self,
        run: dict[str, Any],
        deterministic_evidence: list[dict[str, Any]],
        project_paths: dict[str, Path],
    ) -> dict[str, Any]:
        analysis_mode = self._require_prerequisites(run)
        cache_path = project_paths["evidence"] / "complete-evidence.json"
        cached = self._read_complete_cache(cache_path, run, analysis_mode)
        if cached is not None:
            return cached
        assets: list[dict[str, Any]] = []
        for evidence in deterministic_evidence:
            manifest = _manifest_from_evidence(evidence)
            assets.append(self._complete_asset(run, manifest, analysis_mode))
        result = self._fuse(run, assets, analysis_mode)
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["manifest_path"] = str(cache_path)
        return result

    def supplement_evidence(
        self,
        run: dict[str, Any],
        complete_evidence: dict[str, Any],
        project_paths: dict[str, Path],
        gaps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """只执行一次最小密集证据循环；扩大范围由 Skill 明确请求而不是自动猜测。"""
        if not gaps:
            return complete_evidence
        analysis_mode = self._require_prerequisites(run)
        if complete_evidence.get("analysis_mode") != analysis_mode:
            raise EvidenceCompletionError(["当前多模态分析模式已变化，不能把补充证据混入不同能力基线。"])
        assets_by_id = {str(item.get("asset_id")): item for item in complete_evidence.get("assets") or [] if isinstance(item, dict)}
        for gap in gaps:
            if not isinstance(gap, dict):
                raise EvidenceCompletionError(["SourceUnderstanding 的 evidence_gaps 必须是对象数组。"])
            asset_id = str(gap.get("asset_id") or "")
            target = assets_by_id.get(asset_id)
            try:
                start = float(gap["start_seconds"])
                end = float(gap["end_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EvidenceCompletionError(["EvidenceGapRequest 缺少 asset_id、start_seconds 或 end_seconds。"])
            if target is None:
                raise EvidenceCompletionError([f"EvidenceGapRequest 引用了未冻结素材：{asset_id}。"])
            manifest = _manifest_from_path(Path(str(target["deterministic_manifest_path"])))
            dense = self.evidence_builder.build_dense_window(manifest, start_seconds=start, end_seconds=end)
            transcript_context = _transcript_for_range(target.get("transcript") or {}, start, end)
            frame_paths = [Path(item["path"]) for item in dense["frames"]]
            frame_times = [float(item["time_seconds"]) for item in dense["frames"]]
            if analysis_mode == "direct_video_audio":
                multimodal_evidence = self.multimodal.analyze_video_segment(
                    Path(dense["video_segment_path"]),
                    asset_id=asset_id,
                    source_start_seconds=float(dense["source_range_seconds"][0]),
                    source_end_seconds=float(dense["source_range_seconds"][1]),
                    transcript_context=transcript_context,
                )
            else:
                # V1 不把只能看图片的 Gemini 当作视频/声音能力；密集帧直接交给 Codex。
                multimodal_evidence = {
                    "mode": "not_used_codex_frame_transcript_v1",
                    "observations": [],
                    "limitations": ["当前 Gemini 未通过直接音视频实测，本窗口未调用图片降级。"],
                }
            codex = self._analyze_codex_frames(run, frame_paths, asset_id, frame_times, transcript_context)
            target.setdefault("dense_windows", []).append(
                {
                    "request": {"asset_id": asset_id, "start_seconds": start, "end_seconds": end, "reason": gap.get("reason")},
                    "local": dense,
                    "multimodal": multimodal_evidence,
                    "codex_frame_evidence": codex,
                }
            )
        updated = self._fuse(run, list(assets_by_id.values()), analysis_mode)
        cache_path = project_paths["evidence"] / "complete-evidence.json"
        updated["dense_review_round"] = 1
        updated["manifest_path"] = str(cache_path)
        cache_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        return updated

    def review_render(self, render_path: Path, *, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        """优先直接音视频复核；V1 可退回受限的 Codex 抽帧＋转写复核。"""
        if stage not in {"work_preview", "candidate"}:
            raise EvidenceCompletionError(["渲染复核阶段不合法。"])
        try:
            probe = probe_media(render_path)
        except MediaProbeError as exc:
            raise EvidenceCompletionError([f"无法读取待复核渲染：{exc}"]) from exc
        if not probe.has_video or not probe.has_audio or not probe.duration_seconds:
            raise EvidenceCompletionError(["待复核渲染必须包含可读取的视频和音频流。"])
        health = self.multimodal.health_check()
        capabilities = health.get("capabilities") or {}
        if capabilities.get("supports_video_audio") is True and capabilities.get("supports_structured_output") is True:
            return self._review_render_direct(render_path, probe.duration_seconds, stage, context)
        return self._review_render_from_frames(render_path, probe.duration_seconds, stage, context)

    def _review_render_direct(
        self,
        render_path: Path,
        duration_seconds: float,
        stage: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        review_dir = render_path.parent / f"{render_path.stem}.review-evidence"
        review_dir.mkdir(parents=True, exist_ok=True)
        reviews = []
        ready_key = "ready_for_finishing" if stage == "work_preview" else "ready_for_candidate"
        for index, (start, end) in enumerate(_segments(float(duration_seconds), self.config.multimodal_max_segment_seconds)):
            clip = review_dir / f"segment-{index + 1:03d}.mp4"
            self.evidence_builder.make_video_segment(render_path, clip, start, end - start)
            review = _review_video_segment(self.multimodal, clip, stage=stage, start_seconds=start, end_seconds=end, context=context)
            reviews.append(review)
        blocking = [issue for review in reviews for issue in review.get("blocking_issues", [])]
        result = {
            "stage": stage,
            "render_path": str(render_path),
            "render_hash": sha256_file(render_path),
            "segments": reviews,
            "blocking_issues": blocking,
            ready_key: bool(reviews) and not blocking and all(review.get(ready_key) is True for review in reviews),
            "review_basis": "direct_video_audio_v1",
            "limitations": ["复核结论基于分段直接音视频分析，重要边界仍须结合技术 QC 与用户观看。"],
        }
        (review_dir / "review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _review_render_from_frames(
        self,
        render_path: Path,
        duration_seconds: float,
        stage: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """没有直接音视频模型时，仍完成可审计但能力受限的 V1 复核。"""
        review_dir = render_path.parent / f"{render_path.stem}.review-evidence"
        frames = self.evidence_builder.extract_timed_frames(
            render_path,
            review_dir / "frames",
            duration_seconds=float(duration_seconds),
            maximum_frames=12,
        )
        audio_path = review_dir / "render-audio.wav"
        try:
            extract_audio(render_path, audio_path)
            transcript = self.transcriber.transcribe(audio_path, source_content_hash=sha256_file(render_path))
        except (MediaProbeError, OSError, RuntimeError, ValueError) as exc:
            raise EvidenceCompletionError([f"无法生成抽帧＋转写渲染复核证据：{exc}"]) from exc
        project_id = _review_project_id(context)
        review = _review_render_frames(
            self.frame_analyzer,
            [Path(item["path"]) for item in frames],
            stage=stage,
            frame_times=[float(item["time_seconds"]) for item in frames],
            transcript_context=_transcript_for_range(transcript, 0, float(duration_seconds)),
            context=context,
            project_id=project_id,
        )
        ready_key = "ready_for_finishing" if stage == "work_preview" else "ready_for_candidate"
        result = {
            **review,
            "stage": stage,
            "render_path": str(render_path),
            "render_hash": sha256_file(render_path),
            "transcript": transcript,
            "review_basis": "codex_frames_plus_funasr_transcript_v1",
            "limitations": [
                "本轮未使用 Gemini 直接音视频能力；只复核代表抽帧、FunASR 文本和技术 QC。",
                "不能证明未抽到的画面、帧间连续动作、非语言声音或完整声画关系。",
            ],
            ready_key: review.get(ready_key) is True and not bool(review.get("blocking_issues")),
        }
        (review_dir / "review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _require_prerequisites(self, run: dict[str, Any]) -> str:
        reasons = []
        brief = (run.get("input_snapshot") or {}).get("brief") or {}
        funasr = self.transcriber.health_check()
        if not funasr.get("available"):
            reasons.append(f"FunASR 转写不可用：{funasr.get('reason') or '健康检查未通过'}")
        multimodal = self.multimodal.health_check()
        analysis_mode = self._analysis_mode(multimodal)
        if analysis_mode == "direct_video_audio" and brief.get("cloud_analysis_authorized") is not True:
            reasons.append("尚未在项目简报中明确授权上传必要分析代理到已配置的多模态端点。")
        codex = self.frame_analyzer.health_check()
        if not codex.get("available"):
            reasons.append(f"Codex 抽帧补充分析不可用：{codex.get('reason') or '健康检查未通过'}")
        if reasons:
            raise EvidenceCompletionError(reasons)
        return analysis_mode

    def _read_complete_cache(self, path: Path, run: dict[str, Any], analysis_mode: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(cached, dict) or cached.get("generator") != self.VERSION:
            return None
        if cached.get("analysis_mode") != analysis_mode:
            return None
        expected = _input_hashes(run)
        if cached.get("input_hashes") != expected:
            return None
        if cached.get("analyzers") != self._analyzer_identities():
            return None
        cached["manifest_path"] = str(path)
        return cached

    def _complete_asset(self, run: dict[str, Any], manifest: dict[str, Any], analysis_mode: str) -> dict[str, Any]:
        probe = manifest.get("probe") or {}
        asset_id = str(manifest["asset_id"])
        duration = float(probe.get("duration_seconds") or 0)
        transcript: dict[str, Any]
        if manifest.get("audio_path"):
            transcript = self.transcriber.transcribe(
                Path(str(manifest["audio_path"])),
                source_content_hash=str(manifest["source_content_hash"]),
            )
            timed = [segment for segment in transcript.get("segments") or [] if segment.get("start_seconds") is not None]
            if transcript.get("speech_detected") and not timed:
                raise EvidenceCompletionError([f"素材 {asset_id} 的 FunASR 文本没有可回链时间范围。"])
        else:
            transcript = {
                "mode": "no_audio_stream",
                "segments": [],
                "vad_segments": [],
                "speech_detected": False,
                "limitations": ["素材没有音频流，无法产生语音转写。"],
            }
        transcript_context = _transcript_for_range(transcript, 0, duration)
        multimodal_observations = []
        if probe.get("video_streams"):
            if analysis_mode == "direct_video_audio":
                proxy = Path(str(manifest.get("analysis_proxy_path") or ""))
                if not proxy.exists():
                    raise EvidenceCompletionError([f"素材 {asset_id} 缺少分析代理，不能上传原始高码率文件。"])
                segment_dir = proxy.parent / "multimodal-segments"
                for index, (start, end) in enumerate(_segments(duration, self.config.multimodal_max_segment_seconds)):
                    segment_path = segment_dir / f"{asset_id}-{index + 1:03d}.mp4"
                    self.evidence_builder.make_video_segment(proxy, segment_path, start, end - start)
                    multimodal_observations.append(
                        self.multimodal.analyze_video_segment(
                            segment_path,
                            asset_id=asset_id,
                            source_start_seconds=start,
                            source_end_seconds=end,
                            transcript_context=_transcript_for_range(transcript, start, end),
                        )
                    )
        frames = _frame_records(manifest.get("frames") or [])
        codex_frame_evidence = self._analyze_codex_frames(
            run,
            [Path(item["path"]) for item in frames],
            asset_id,
            [float(item["time_seconds"]) for item in frames],
            transcript_context,
        ) if frames else {"analysis_mode": "not_applicable_no_video", "observations": []}
        return {
            "asset_id": asset_id,
            "source_content_hash": manifest["source_content_hash"],
            "working_content_hash": manifest["working_content_hash"],
            "duration_seconds": duration,
            "deterministic_manifest_path": str(manifest.get("_manifest_path") or ""),
            "frames": frames,
            "contact_sheet_path": manifest.get("contact_sheet_path"),
            "transcript": transcript,
            "vad_segments": transcript.get("vad_segments") or [],
            "scene_candidates": manifest.get("scene_candidates") or [],
            "silence_candidates": manifest.get("silence_candidates") or [],
            "loudness": manifest.get("loudness"),
            "multimodal": {
                "mode": analysis_mode if analysis_mode == "direct_video_audio" else "not_used_in_v1",
                "observations": multimodal_observations,
            },
            "codex_frame_evidence": codex_frame_evidence,
            "coverage": {
                "transcript": bool(manifest.get("audio_path")),
                "direct_video_audio": analysis_mode == "direct_video_audio" and bool(multimodal_observations),
                "codex_frame_transcript": analysis_mode == "codex_frame_transcript_mode" and bool(frames),
                "sparse_codex_frames": bool(frames),
            },
            "dense_windows": [],
        }

    def _analyze_codex_frames(
        self,
        run: dict[str, Any],
        frame_paths: list[Path],
        asset_id: str,
        frame_times: list[float],
        transcript_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not frame_paths:
            return {"analysis_mode": "not_applicable_no_video", "observations": []}
        analyzer = self.frame_analyzer
        try:
            return analyzer.analyze_frames(
                frame_paths,
                asset_id=asset_id,
                frame_times=frame_times,
                transcript_context=transcript_context,
                project_id=str(run["project_id"]),
            )
        except TypeError:
            # 保持 Port 合同可用；生产 Codex Adapter 接受 project_id，测试替身可只实现基础签名。
            return analyzer.analyze_frames(
                frame_paths,
                asset_id=asset_id,
                frame_times=frame_times,
                transcript_context=transcript_context,
            )

    def _fuse(self, run: dict[str, Any], assets: list[dict[str, Any]], analysis_mode: str) -> dict[str, Any]:
        conflicts = []
        for asset in assets:
            transcript = asset.get("transcript") or {}
            if transcript.get("speech_detected") and not asset.get("multimodal", {}).get("observations"):
                conflicts.append(
                    {
                        "asset_id": asset.get("asset_id"),
                        "kind": "speech_without_direct_av_observation",
                        "message": "转写存在，但没有可用直接音视频观察；不得据此断言声画关系。",
                    }
                )
        return {
            "generator": self.VERSION,
            "generated_at": utc_now(),
            "project_id": run["project_id"],
            "input_hashes": _input_hashes(run),
            "analysis_mode": analysis_mode,
            "analyzers": self._analyzer_identities(),
            "assets": assets,
            "conflicts": conflicts,
            "limitations": [
                "Gemini 时间范围是片段级观察，不能直接用作帧级切点。"
                if analysis_mode == "direct_video_audio"
                else "当前为 Codex 抽帧＋FunASR 转写模式，不能声称已完成连续音视频理解或帧级切点复核。",
                "Codex 抽帧只覆盖已附加图片；重要动作必须由 SourceUnderstanding 请求密集窗口复核。",
                "FunASR 标点、VAD 与句段是候选边界，不等于最终剪辑边界。",
            ],
        }

    @staticmethod
    def _analysis_mode(health: dict[str, Any]) -> str:
        capabilities = health.get("capabilities") if isinstance(health, dict) else None
        if isinstance(capabilities, dict) and (
            capabilities.get("supports_video_audio") is True
            and capabilities.get("supports_structured_output") is True
        ):
            return "direct_video_audio"
        # V1 以 FunASR、确定性音频证据和 Codex 密集抽帧继续；不调用图片 Gemini 伪装成视频理解。
        return "codex_frame_transcript_mode"

    def _analyzer_identities(self) -> dict[str, Any]:
        return {
            "transcriber": self.transcriber.identity(),
            "multimodal": self.multimodal.identity(),
            "codex_frame": self.frame_analyzer.identity(),
        }


def _manifest_from_evidence(value: dict[str, Any]) -> dict[str, Any]:
    manifest = value.get("manifest")
    if isinstance(manifest, dict):
        result = dict(manifest)
        result["_manifest_path"] = value.get("manifest_path")
        return result
    raw_path = value.get("manifest_path")
    if not isinstance(raw_path, str):
        raise EvidenceCompletionError(["确定性证据缺少 manifest。"])
    return _manifest_from_path(Path(raw_path))


def _manifest_from_path(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceCompletionError([f"无法读取确定性 Evidence Bundle：{path}"]) from exc
    if not isinstance(result, dict):
        raise EvidenceCompletionError(["确定性 Evidence Bundle 不是对象。"])
    result["_manifest_path"] = str(path)
    return result


def _frame_records(raw_frames: list[Any]) -> list[dict[str, Any]]:
    records = []
    for frame in raw_frames:
        if isinstance(frame, str):
            match = re.search(r"-([0-9.]+)s\.jpg$", frame)
            if not match:
                continue
            records.append({"path": frame, "time_seconds": float(match.group(1)), "layer": "overview"})
        elif isinstance(frame, dict) and isinstance(frame.get("path"), str):
            try:
                moment = float(frame.get("time_seconds"))
            except (TypeError, ValueError):
                continue
            records.append({"path": frame["path"], "time_seconds": moment, "layer": frame.get("layer") or "overview"})
    return records


def _segments(duration: float, maximum: float) -> list[tuple[float, float]]:
    if duration <= 0:
        return []
    result = []
    start = 0.0
    while start < duration:
        end = min(duration, start + maximum)
        result.append((round(start, 3), round(end, 3)))
        if end >= duration:
            break
        # 相邻段保持 1 秒重叠；融合层不把两次观察强行合并为单一事实。
        start = max(end - 1.0, start + 0.1)
    return result


def _representative_moments(duration: float, maximum_frames: int) -> list[float]:
    """均匀覆盖开头、中段和结尾；代表帧不是连续动作证据。"""
    if duration <= 0 or maximum_frames <= 0:
        return []
    if maximum_frames == 1:
        return [round(duration / 2.0, 3)]
    margin = min(0.25, duration / 2.0)
    start = margin
    end = max(start, duration - margin)
    return [round(start + (end - start) * index / (maximum_frames - 1), 3) for index in range(maximum_frames)]


def _dense_moments(start: float, end: float, *, interval: float) -> list[float]:
    values = []
    moment = start
    while moment <= end + 0.0001:
        values.append(round(moment, 3))
        moment += interval
    return values


def _transcript_for_range(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    result = []
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment_start = segment.get("start_seconds")
        segment_end = segment.get("end_seconds")
        if segment_start is None or segment_end is None:
            continue
        try:
            if float(segment_end) >= start and float(segment_start) <= end:
                result.append(
                    {
                        "start_seconds": segment_start,
                        "end_seconds": segment_end,
                        "text": segment.get("text"),
                        "speaker": segment.get("speaker"),
                    }
                )
        except (TypeError, ValueError):
            continue
    return result


def _input_hashes(run: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"asset_id": str(asset.get("id")), "content_hash": str(asset.get("content_hash")), "working_hash": str(asset.get("working_hash"))}
        for asset in (run.get("input_snapshot") or {}).get("assets") or []
        if isinstance(asset, dict)
    ]


def _review_project_id(context: dict[str, Any]) -> str:
    run = context.get("run")
    if isinstance(run, dict) and isinstance(run.get("project_id"), str) and run["project_id"]:
        return str(run["project_id"])
    project_id = context.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id
    raise EvidenceCompletionError(["抽帧＋转写渲染复核缺少项目身份，不能创建游离 Codex Thread。"])


def _review_video_segment(
    multimodal: MultimodalAnalyzerPort,
    video_path: Path,
    *,
    stage: str,
    start_seconds: float,
    end_seconds: float,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Adapter 可选提供专用渲染审查；未实现时宁可阻断，不能拿普通观察假装通过。"""
    review = getattr(multimodal, "review_video_segment", None)
    if not callable(review):
        raise EvidenceCompletionError(["当前多模态 Adapter 未实现渲染复核合同。"])
    result = review(
        video_path,
        stage=stage,
        source_start_seconds=start_seconds,
        source_end_seconds=end_seconds,
        context=context,
    )
    if not isinstance(result, dict):
        raise EvidenceCompletionError(["多模态渲染复核没有返回结构化结果。"])
    return result


def _review_render_frames(
    analyzer: FrameEvidenceAnalyzerPort,
    image_paths: list[Path],
    *,
    stage: str,
    frame_times: list[float],
    transcript_context: list[dict[str, Any]],
    context: dict[str, Any],
    project_id: str,
) -> dict[str, Any]:
    """V1 只允许明确实现帧＋转写复核合同的 Codex Adapter 放行。"""
    review = getattr(analyzer, "review_render_frames", None)
    if not callable(review):
        raise EvidenceCompletionError(["当前 Codex 抽帧 Adapter 未实现渲染复核合同。"])
    try:
        result = review(
            image_paths,
            stage=stage,
            frame_times=frame_times,
            transcript_context=transcript_context,
            context=context,
            project_id=project_id,
        )
    except TypeError:
        # 测试替身可省略项目身份；生产 Adapter 必须将 review 绑定真实项目 Thread。
        result = review(
            image_paths,
            stage=stage,
            frame_times=frame_times,
            transcript_context=transcript_context,
            context=context,
        )
    if not isinstance(result, dict):
        raise EvidenceCompletionError(["Codex 抽帧渲染复核没有返回结构化结果。"])
    return result
