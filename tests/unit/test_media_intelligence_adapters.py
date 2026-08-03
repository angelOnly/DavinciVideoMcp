from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from davinci_app.adapters.funasr_transcriber import FunASRTranscriberAdapter
from davinci_app.adapters.openai_multimodal import (
    MultimodalAdapterError,
    OpenAICompatibleMultimodalAdapter,
    _parse_completion,
)
from davinci_app.config import AppConfig
from davinci_app.media.evidence import EvidenceBuilder, EvidenceCompletionError, MediaEvidenceRuntime


def _config(root: Path, **overrides: Any) -> AppConfig:
    workspace = root / "workspace"
    values: dict[str, Any] = {
        "repository_root": root,
        "workspace_root": workspace,
        "data_root": workspace / "data",
        "projects_root": workspace / "projects",
        "creative_cache_root": workspace / "creative-cache",
        "product_database": workspace / "data" / "product.db",
        "creative_catalog_database": workspace / "data" / "creative.db",
        "expected_conda_environment": "test",
        "expected_python_version": (3, 10, 20),
        "multimodal_model": "gemini-3-flash",
        "multimodal_base_url": "https://example.invalid/v1",
        "funasr_manifest": root / "models" / "manifest.yaml",
        "creative_raw_root": root / "creative-raw",
        "creative_certified_root": root / "creative-certified",
        "resolve_workspace_project": "DavinciMcp_Test",
        "multimodal_api_key": "test-secret",
    }
    values.update(overrides)
    config = AppConfig(**values)
    config.ensure_directories()
    return config


class _FakeAutoModel:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def generate(self, **_: Any) -> list[dict[str, Any]]:
        if "vad" in str(self.kwargs["model"]):
            return [{"value": [[0, 600], [800, 1500]]}]
        return [
            {
                "text": "你好，世界。",
                "sentence_info": [
                    {"text": "你好，", "timestamp": [0, 600]},
                    {"text": "世界。", "timestamp": [800, 1500]},
                ],
            }
        ]


class _HealthyPort:
    def __init__(self, name: str) -> None:
        self.name = name

    def health_check(self) -> dict[str, Any]:
        return {
            "available": True,
            "capabilities": {"supports_video_audio": True, "supports_structured_output": True},
        }

    def identity(self) -> dict[str, str]:
        return {"name": self.name}


class _ImageFallbackPort(_HealthyPort):
    """只通过图片、文本和结构化输出的端点替身。"""

    def health_check(self) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "未通过带声音 MP4 探测。",
            "capabilities": {
                "supports_text": True,
                "supports_image": True,
                "supports_audio": False,
                "supports_video": False,
                "supports_video_audio": False,
                "supports_structured_output": True,
            },
        }


class _NoDirectAudioVideoPort(_ImageFallbackPort):
    """用于断言降级链路绝不会把 MP4 交给未通过探测的端点。"""

    def __init__(self) -> None:
        super().__init__("multimodal")
        self.video_calls = 0

    def analyze_video_segment(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.video_calls += 1
        raise AssertionError("未通过直接音视频探测的端点不应收到视频片段。")


class _FrameEvidenceAnalyzer(_HealthyPort):
    def __init__(self) -> None:
        super().__init__("codex")
        self.calls = 0

    def analyze_frames(self, image_paths: list[Path], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if not image_paths or kwargs.get("project_id") != "project":
            raise AssertionError("Codex 抽帧分析没有收到受管项目图片证据。")
        return {
            "analysis_mode": "sparse_frame_only",
            "observations": [
                {
                    "frame_time_seconds": kwargs["frame_times"][0],
                    "visual_observation": "测试画面",
                    "uncertainty": "抽帧样本",
                    "needs_dense_review": False,
                }
            ],
        }


class _ReviewTranscriber(_HealthyPort):
    def transcribe(self, _: Path, *, source_content_hash: str) -> dict[str, Any]:
        return {
            "source_content_hash": source_content_hash,
            "speech_detected": True,
            "segments": [{"start_seconds": 0.0, "end_seconds": 2.0, "text": "测试对白", "speaker": None}],
        }


class _FrameRenderReviewer(_HealthyPort):
    def __init__(self) -> None:
        super().__init__("codex")
        self.called = False

    def review_render_frames(self, image_paths: list[Path], **kwargs: Any) -> dict[str, Any]:
        self.called = True
        if not image_paths or kwargs.get("project_id") != "project":
            raise AssertionError("抽帧渲染复核没有收到受管项目证据。")
        return {
            "observations": [{"frame_time_seconds": 1.0, "observation": "画面正常", "uncertainty": "代表帧"}],
            "blocking_issues": [],
            "ready_for_finishing": True,
            "ready_for_candidate": True,
        }


class _ReviewEvidenceBuilder:
    def __init__(self) -> None:
        self.maximum_frames: int | None = None

    def extract_timed_frames(
        self,
        _: Path,
        target_dir: Path,
        *,
        duration_seconds: float,
        maximum_frames: int,
        prefix: str = "review",
    ) -> list[dict[str, Any]]:
        self.maximum_frames = maximum_frames
        target_dir.mkdir(parents=True, exist_ok=True)
        frame = target_dir / f"{prefix}.jpg"
        frame.write_bytes(b"frame")
        return [{"path": str(frame), "time_seconds": min(1.0, duration_seconds), "layer": "review"}]


class MediaAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_funasr_uses_only_manifest_local_models_and_normalises_vad_timestamps(self) -> None:
        models = self.root / "models"
        for relative in ("iic/asr", "iic/vad", "iic/punc"):
            directory = models / relative
            directory.mkdir(parents=True)
            (directory / "config.yaml").write_text("name: local", encoding="utf-8")
            (directory / "model.pt").write_bytes(b"local-weight")
        (models / "manifest.yaml").write_text(
            """funasr:
  root: ./models
  asr_model: iic/asr
  vad_model: iic/vad
  punc_model: iic/punc
  speaker_model: null
  auto_download: false
""",
            encoding="utf-8",
        )
        audio = self.root / "sample.wav"
        audio.write_bytes(b"audio")
        adapter = FunASRTranscriberAdapter(_config(self.root), auto_model_factory=_FakeAutoModel)

        result = adapter.transcribe(audio, source_content_hash="asset-hash")

        self.assertEqual("你好，", result["segments"][0]["text"])
        self.assertEqual(0.0, result["segments"][0]["start_seconds"])
        self.assertEqual(1.5, result["segments"][1]["end_seconds"])
        self.assertEqual(2, len(result["vad_segments"]))
        self.assertFalse(result["model"]["models"]["asr"]["path"].startswith("http"))

    def test_multimodal_health_uses_actual_modality_requests_and_refuses_video_without_audio_confirmation(self) -> None:
        calls: list[dict[str, Any]] = []

        def transport(_: str, body: dict[str, Any], __: int, headers: dict[str, str]) -> dict[str, Any]:
            calls.append(body)
            self.assertEqual("Bearer test-secret", headers["Authorization"])
            content = body["messages"][1]["content"]
            kinds = {item["type"] for item in content if item["type"] != "text"}
            if "video_url" in kinds:
                # 反代若只确认 video 而不确认 audio，必须保持 false。
                value = {"modalities_observed": ["video"], "summary": "仅视频"}
            elif "input_audio" in kinds:
                value = {"modalities_observed": ["audio"], "summary": "音频"}
            elif "image_url" in kinds:
                value = {"modalities_observed": ["image"], "summary": "图片"}
            else:
                value = {"modalities_observed": ["text"], "summary": "文本"}
            return {"choices": [{"message": {"content": json.dumps(value)}}]}

        adapter = OpenAICompatibleMultimodalAdapter(_config(self.root), transport=transport)
        health = adapter.health_check()

        self.assertTrue(health["capabilities"]["supports_text"])
        self.assertTrue(health["capabilities"]["supports_image"])
        self.assertTrue(health["capabilities"]["supports_audio"])
        self.assertTrue(health["capabilities"]["supports_video"])
        self.assertFalse(health["capabilities"]["supports_video_audio"])
        self.assertTrue(health["capabilities"]["supports_structured_output"])
        self.assertFalse(health["available"])
        self.assertGreaterEqual(len(calls), 4)

    def test_multimodal_parser_accepts_one_fenced_json_object_but_not_extra_explanation(self) -> None:
        parsed = _parse_completion(
            {"choices": [{"message": {"content": "```json\n{\"modalities_observed\":[\"text\"],\"summary\":\"ok\"}\n```"}}]}
        )
        self.assertEqual(["text"], parsed["modalities_observed"])
        with self.assertRaisesRegex(MultimodalAdapterError, "JSON"):
            _parse_completion(
                {"choices": [{"message": {"content": "说明：{\"modalities_observed\":[\"text\"]}"}}]}
            )

    def test_image_evidence_preserves_source_timestamps_instead_of_treating_them_as_clip_offsets(self) -> None:
        frame = self.root / "frame.png"
        frame.write_bytes(b"not-decoded-by-fake-transport")
        second_frame = self.root / "frame-2.png"
        second_frame.write_bytes(b"not-decoded-by-fake-transport")

        def transport(_: str, __: dict[str, Any], ___: int, ____: dict[str, str]) -> dict[str, Any]:
            value = {
                "observations": [
                    {
                        "start_seconds": 12.0,
                        "end_seconds": 18.0,
                        "visual_observation": "静态测试画面",
                        "audio_observation": "未知",
                        "audio_visual_relation": "无法从图片判断",
                        "uncertainty": "稀疏抽帧",
                        "needs_dense_review": True,
                    }
                ],
                "overall_uncertainty": "仅图片",
            }
            return {"choices": [{"message": {"content": json.dumps(value)}}]}

        adapter = OpenAICompatibleMultimodalAdapter(_config(self.root), transport=transport)
        adapter._health_cache = {"capabilities": {"supports_image": True}}

        result = adapter.analyze_image_evidence(
            [frame, second_frame],
            asset_id="asset",
            frame_times=[12.0, 18.0],
            transcript_context=[],
        )

        self.assertEqual(12.0, result["observations"][0]["start_seconds"])
        self.assertEqual(18.0, result["observations"][0]["end_seconds"])

    def test_v1_uses_codex_frame_transcript_mode_when_direct_video_audio_is_unavailable(self) -> None:
        runtime = MediaEvidenceRuntime(
            _config(self.root),
            _HealthyPort("funasr"),  # type: ignore[arg-type]
            _ImageFallbackPort("multimodal"),  # type: ignore[arg-type]
            _HealthyPort("codex"),  # type: ignore[arg-type]
        )
        paths = {"evidence": self.root / "evidence"}
        paths["evidence"].mkdir()
        run = {
            "project_id": "project",
            "input_snapshot": {"brief": {}, "assets": []},
        }

        result = runtime.complete_evidence(run, [], paths)

        self.assertEqual("codex_frame_transcript_mode", result["analysis_mode"])

    def test_overview_frames_cover_twelve_time_samples_for_long_video(self) -> None:
        builder = EvidenceBuilder()
        with patch.object(builder, "_extract_frame", return_value=True):
            frames = builder._extract_overview_frames(self.root / "source.mp4", self.root / "frames", 60.0)

        self.assertEqual(12, len(frames))
        self.assertLess(frames[0]["time_seconds"], frames[-1]["time_seconds"])

    def test_v1_continues_with_funasr_and_codex_frames_when_direct_audio_video_is_unavailable(self) -> None:
        multimodal = _NoDirectAudioVideoPort()
        frames = []
        for index, moment in enumerate((0.5, 2.5, 4.5), start=1):
            frame = self.root / f"frame-{index}.jpg"
            frame.write_bytes(b"frame")
            frames.append({"path": str(frame), "time_seconds": moment, "layer": "overview"})
        audio = self.root / "audio.wav"
        audio.write_bytes(b"audio")
        runtime = MediaEvidenceRuntime(
            _config(self.root),
            _ReviewTranscriber("funasr"),  # type: ignore[arg-type]
            multimodal,  # type: ignore[arg-type]
            _FrameEvidenceAnalyzer(),  # type: ignore[arg-type]
        )
        paths = {"evidence": self.root / "evidence"}
        paths["evidence"].mkdir()
        run = {
            "project_id": "project",
            "input_snapshot": {
                "brief": {},
                "assets": [{"id": "asset", "content_hash": "source", "working_hash": "working"}],
            },
        }
        manifest = {
            "asset_id": "asset",
            "source_content_hash": "source",
            "working_content_hash": "working",
            "probe": {"duration_seconds": 5.0, "video_streams": 1, "audio_streams": 1},
            "audio_path": str(audio),
            "frames": frames,
            "scene_candidates": [],
            "silence_candidates": [],
            "loudness": None,
        }

        result = runtime.complete_evidence(run, [{"manifest": manifest, "manifest_path": str(self.root / "manifest.json")}], paths)

        self.assertEqual("codex_frame_transcript_mode", result["analysis_mode"])
        self.assertEqual(0, multimodal.video_calls)
        asset = result["assets"][0]
        self.assertTrue(asset["coverage"]["codex_frame_transcript"])
        self.assertEqual("not_used_in_v1", asset["multimodal"]["mode"])

    def test_v1_can_review_render_with_codex_frames_and_funasr_without_direct_video_audio(self) -> None:
        reviewer = _FrameRenderReviewer()
        evidence_builder = _ReviewEvidenceBuilder()
        runtime = MediaEvidenceRuntime(
            _config(self.root),
            _ReviewTranscriber("funasr"),  # type: ignore[arg-type]
            _ImageFallbackPort("multimodal"),  # type: ignore[arg-type]
            reviewer,  # type: ignore[arg-type]
            evidence_builder=evidence_builder,  # type: ignore[arg-type]
        )
        render = self.root / "render.mp4"
        render.write_bytes(b"render")

        with patch(
            "davinci_app.media.evidence.probe_media",
            return_value=SimpleNamespace(has_video=True, has_audio=True, duration_seconds=4.0),
        ), patch(
            "davinci_app.media.evidence.extract_audio",
            side_effect=lambda _source, target: target.write_bytes(b"audio"),
        ):
            result = runtime.review_render(render, stage="candidate", context={"project_id": "project"})

        self.assertTrue(reviewer.called)
        self.assertEqual(12, evidence_builder.maximum_frames)
        self.assertEqual("codex_frames_plus_funasr_transcript_v1", result["review_basis"])
        self.assertTrue(result["ready_for_candidate"])

    def test_local_dotenv_supplies_multimodal_settings_without_overriding_process_secrets(self) -> None:
        (self.root / ".env").write_text(
            "MULTIMODAL_BASE_URL=https://local.example/v1\nMULTIMODAL_API_KEY=local-secret\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            config = AppConfig.from_environment(self.root)
            self.assertEqual("https://local.example/v1", config.multimodal_base_url)
            self.assertEqual("local-secret", config.multimodal_api_key)
        with patch.dict(os.environ, {"MULTIMODAL_API_KEY": "process-secret"}, clear=True):
            config = AppConfig.from_environment(self.root)
            self.assertEqual("process-secret", config.multimodal_api_key)

    def test_full_evidence_refuses_cloud_upload_without_explicit_project_authorization(self) -> None:
        runtime = MediaEvidenceRuntime(
            _config(self.root),
            _HealthyPort("funasr"),  # type: ignore[arg-type]
            _HealthyPort("multimodal"),  # type: ignore[arg-type]
            _HealthyPort("codex"),  # type: ignore[arg-type]
        )
        run = {"project_id": "project", "input_snapshot": {"brief": {}, "assets": []}}
        paths = {"evidence": self.root / "evidence"}
        paths["evidence"].mkdir()

        with self.assertRaisesRegex(EvidenceCompletionError, "明确授权"):
            runtime.complete_evidence(run, [], paths)
