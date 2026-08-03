from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from davinci_app.editorial.pipeline import ProfessionalPreproduction
from davinci_app.execution.professional_compiler import ProfessionalCompilationError, ProfessionalExecutionCompiler


class ProfessionalExecutionCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = ProfessionalExecutionCompiler(SimpleNamespace(resolve_workspace_project="DavinciMcp_Workspace"))

    def test_compiles_certified_audio_binding_to_exact_engine_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {"plans": root / "plans", "renders": root / "renders"}
            for directory in paths.values():
                directory.mkdir()
            preproduction = ProfessionalPreproduction(
                edit_plan={
                    "capability_requests": [{"capability_id": "audio.cue.one"}],
                    "execution": {
                        "kind": "resolve_source_clips_v1",
                        "timeline_fps": 30,
                        "width": 1920,
                        "height": 1080,
                        "clips": [_clip()],
                        "operations": [
                            {
                                "kind": "place_audio_asset",
                                "capability_id": "audio.cue.one",
                                "record_frame": 45,
                                "duration_seconds": 1.5,
                                "source_in_seconds": 0,
                                "audio_track": 2,
                            }
                        ],
                    },
                },
                edit_plan_digest="plan-digest",
                capability_binding={
                    "edit_plan_digest": "plan-digest",
                    "bindings": [
                        {
                            "capability_id": "audio.cue.one",
                            "mechanism": "audio_asset",
                            "content_hash": "a" * 64,
                            "cache_path": str(root / "cache" / "asset.wav"),
                            "constraints": {},
                        }
                    ],
                },
            )

            result = self.compiler.compile_work_preview(_run(), paths, preproduction)

            operation = result["plan"]["creative_operations"][0]
            self.assertEqual("audio_asset", operation["mechanism"])
            self.assertEqual(2, operation["parameters"]["audio_track"])
            self.assertEqual(45, operation["parameters"]["record_frame"])

    def test_refuses_font_before_text_compiler_mapping_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {"plans": root / "plans", "renders": root / "renders"}
            for directory in paths.values():
                directory.mkdir()
            preproduction = ProfessionalPreproduction(
                edit_plan={
                    "capability_requests": [{"capability_id": "font.zh.one"}],
                    "execution": {
                        "kind": "resolve_source_clips_v1",
                        "timeline_fps": 30,
                        "width": 1920,
                        "height": 1080,
                        "clips": [_clip()],
                        "operations": [{"kind": "apply_text_style", "capability_id": "font.zh.one"}],
                    },
                },
                edit_plan_digest="plan-digest",
                capability_binding={
                    "edit_plan_digest": "plan-digest",
                    "bindings": [
                        {
                            "capability_id": "font.zh.one",
                            "mechanism": "font_file",
                            "content_hash": "b" * 64,
                            "cache_path": str(root / "cache" / "font.ttf"),
                            "constraints": {},
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ProfessionalCompilationError, "没有认证 Compiler Mapping"):
                self.compiler.compile_work_preview(_run(), paths, preproduction)


def _clip() -> dict[str, object]:
    return {
        "asset_id": "source-video",
        "source_in_seconds": 0,
        "source_out_seconds": 3,
        "record_frame": 0,
        "video_track": 1,
        "audio_track": 1,
        "include_audio": True,
    }


def _run() -> dict[str, object]:
    return {
        "id": "run-123",
        "kind": "initial_edit",
        "project_id": "project-123",
        "input_snapshot": {
            "assets": [
                {
                    "id": "source-video",
                    "working_hash": "source-hash",
                    "working_path": "C:/workspace/source.mp4",
                }
            ]
        },
    }


if __name__ == "__main__":
    unittest.main()
