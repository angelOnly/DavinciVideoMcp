from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from davinci_app.adapters.codex_app_server import CodexAppServerSkillRuntime, skill_output_schema
from davinci_app.config import AppConfig


class _ProjectThreads:
    def __init__(self) -> None:
        self.thread_id: str | None = None

    def get_codex_thread_id(self, _: str) -> str | None:
        return self.thread_id

    def record_codex_thread_id(self, _: str, thread_id: str) -> str:
        self.thread_id = thread_id
        return thread_id


class _Session:
    def __init__(self, _: AppConfig) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.turns = 0

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            self.turns += 1
            return {"turn": {"id": f"turn-{self.turns}"}}
        raise AssertionError(f"意外请求：{method}")

    def wait_for_turn(self, _: str) -> tuple[dict[str, Any], list[str]]:
        return ({"status": "completed"}, ['{"semantic_units": [], "relationships": [], "evidence_gaps": [], "unknowns": []}'])


class CodexSkillRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        workspace = root / "workspace"
        self.config = AppConfig(
            repository_root=root,
            workspace_root=workspace,
            data_root=workspace / "data",
            projects_root=workspace / "projects",
            creative_cache_root=workspace / "creative-cache",
            product_database=workspace / "data" / "product.db",
            creative_catalog_database=workspace / "data" / "creative.db",
            expected_conda_environment="test",
            expected_python_version=(3, 10, 20),
            multimodal_model="test",
            multimodal_base_url=None,
            funasr_manifest=root / "models" / "manifest.yaml",
            creative_raw_root=root / "creative-raw",
            creative_certified_root=root / "creative-certified",
            resolve_workspace_project="test",
        )
        self.config.ensure_directories()
        skill_dir = root / ".agents" / "skills" / "video-source-understanding"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# test", encoding="utf-8")
        self.sessions: list[_Session] = []

        def factory(config: AppConfig) -> _Session:
            session = _Session(config)
            self.sessions.append(session)
            return session

        self.runtime = CodexAppServerSkillRuntime(self.config, _ProjectThreads(), session_factory=factory)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_skill_turn_has_explicit_skill_schema_and_read_only_sandbox(self) -> None:
        result = self.runtime.invoke(
            "video-source-understanding",
            mode=None,
            payload={
                "run": {"project_id": "project", "input_snapshot": {"brief": {}}},
                "evidence": {"assets": [], "analysis_mode": "direct_video_audio_plus_sparse_codex_frames"},
            },
        )

        self.assertEqual([], result["semantic_units"])
        request = next(params for method, params in self.sessions[0].requests if method == "turn/start")
        self.assertEqual(":read-only", request["permissions"])
        self.assertNotIn("sandboxPolicy", request)
        self.assertIn("outputSchema", request)
        self.assertTrue(any(item.get("type") == "skill" for item in request["input"]))
        self.assertFalse(any("Resolve" in str(item) for item in request["input"] if item.get("type") == "skill"))

    def test_all_professional_output_schemas_are_closed_for_strict_structured_output(self) -> None:
        variants = [
            ("video-source-understanding", None),
            ("video-edit-director", "direction"),
            ("video-edit-director", "finalize"),
            ("video-sound-rhythm-designer", None),
            ("video-visual-designer", None),
            ("video-typography-designer", None),
            ("video-finishing-designer", None),
        ]
        for skill_name, mode in variants:
            with self.subTest(skill=skill_name, mode=mode):
                self._assert_closed_schema(skill_output_schema(skill_name, mode))

    def _assert_closed_schema(self, schema: dict[str, Any]) -> None:
        kind = schema.get("type")
        if kind == "object":
            self.assertIs(False, schema.get("additionalProperties"))
            self.assertEqual(set(schema.get("properties", {})), set(schema.get("required", [])))
            for child in schema.get("properties", {}).values():
                if isinstance(child, dict):
                    self._assert_closed_schema(child)
        if kind == "array":
            items = schema.get("items")
            if isinstance(items, dict):
                self._assert_closed_schema(items)
