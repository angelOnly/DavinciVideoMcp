from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from davinci_app.execution.compiler import EngineSmokeCompiler


class EngineSmokeCompilerTests(unittest.TestCase):
    def test_fragment_montage_preserves_source_order_without_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name: root / name for name in ("plans", "renders")}
            for path in paths.values():
                path.mkdir()
            assets = [
                _asset("a", 10.0, True),
                _asset("b", 4.0, False),
            ]
            run = {
                "id": "run-123",
                "kind": "engine_smoke",
                "input_snapshot": {
                    "project_id": "project-123",
                    "brief": {"testing_preset": "fragment_montage", "max_clip_seconds": 6},
                    "assets": assets,
                },
            }
            result = EngineSmokeCompiler().compile(run, paths)
            clips = result["plan"]["clips"]
            self.assertEqual(["a", "b"], [clip["asset_id"] for clip in clips])
            self.assertEqual(0, clips[0]["record_frame"])
            self.assertGreaterEqual(clips[1]["record_frame"], 1)
            self.assertTrue(Path(result["plan_path"]).exists())

    def test_interview_is_explicitly_limited_to_requested_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name: root / name for name in ("plans", "renders")}
            for path in paths.values():
                path.mkdir()
            run = {
                "id": "run-123",
                "kind": "engine_smoke",
                "input_snapshot": {
                    "project_id": "project-123",
                    "brief": {"testing_preset": "interview_excerpt", "max_duration_seconds": 90},
                    "assets": [_asset("interview", 600.0, True)],
                },
            }
            result = EngineSmokeCompiler().compile(run, paths)
            clip = result["plan"]["clips"][0]
            self.assertEqual(90, clip["source_out_seconds"])

    def test_rejects_formal_run_even_when_it_contains_testing_preset(self) -> None:
        run = {
            "id": "run-123",
            "kind": "initial_edit",
            "input_snapshot": {
                "project_id": "project-123",
                "brief": {"testing_preset": "fragment_montage"},
                "assets": [_asset("a", 10.0, True)],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name: root / name for name in ("plans", "renders")}
            for path in paths.values():
                path.mkdir()
            with self.assertRaisesRegex(ValueError, "只能处理 engine_smoke"):
                EngineSmokeCompiler().compile(run, paths)


def _asset(identifier: str, duration: float, has_audio: bool) -> dict[str, object]:
    return {
        "id": identifier,
        "original_name": f"{identifier}.mp4",
        "content_hash": "source-hash",
        "working_hash": "working-hash",
        "working_path": f"C:/workspace/{identifier}.mp4",
        "probe": {"duration_seconds": duration, "audio_streams": 1 if has_audio else 0},
    }
