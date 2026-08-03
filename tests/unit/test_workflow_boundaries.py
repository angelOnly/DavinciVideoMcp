from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from davinci_app.common import sha256_file, utc_now
from davinci_app.config import AppConfig
from davinci_app.media.validation import UploadValidator
from davinci_app.persistence import ProductDatabase
from davinci_app.project.service import ProjectService
from davinci_app.workflow.queue import TaskQueue
from davinci_app.workflow.worker import WorkflowWorker


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> "_FakeEngine":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def call(self, name: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append(name)
        if name == "engine_status":
            return {
                "state": "succeeded",
                "ffmpeg": {"available": True},
                "resolve": {"connected": True, "ready_for_execution": True},
            }
        if name == "validate_execution_plan":
            return {"state": "succeeded", "validation": {"valid": True, "expected_duration_seconds": 1}}
        if name == "preview_execution_plan":
            return {"state": "succeeded", "plan_digest": "permit"}
        if name == "execute_execution_plan":
            return {"state": "succeeded"}
        if name == "render_version":
            output = Path(arguments["plan"]["render_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fake-render")
            return {"state": "succeeded", "output_path": str(output)}
        if name == "verify_render":
            return {"state": "succeeded", "verification": {"valid": True}}
        raise AssertionError(f"意外的 Engine 调用：{name}")


class _StubEvidenceBuilder:
    def build(self, asset: dict[str, Any], evidence_root: Path, *, proxy_root: Path | None = None) -> dict[str, Any]:
        return {"manifest_path": str(evidence_root / f"{asset['id']}.json"), "manifest": {"asset_id": asset["id"]}}


class WorkflowBoundaryTests(unittest.TestCase):
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
            resolve_workspace_project="DavinciMcp_Test",
        )
        self.config.ensure_directories()
        self.database = ProductDatabase(self.config.product_database)
        self.database.initialize()
        self.projects = ProjectService(self.config, self.database, UploadValidator())
        self.queue = TaskQueue(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_engine_smoke_creates_only_technical_preview(self) -> None:
        project, run = self._run("engine_smoke")
        engine = _FakeEngine()
        worker = WorkflowWorker(self.projects, self.queue, engine_client_factory=lambda: engine)

        self.assertTrue(worker.run_once())

        self.assertEqual("succeeded", self.projects.get_run(run["id"])["status"])
        self.assertEqual("technical_preview_available", self.projects.get_project(project["id"])["status"])
        self.assertEqual([], self.projects.list_video_versions(project["id"]))
        artifacts = self.projects.list_video_artifacts(project["id"], run_id=run["id"])
        self.assertEqual(["technical_preview"], [artifact["artifact_type"] for artifact in artifacts])
        self.assertNotIn("build_candidate", engine.calls)

    def test_missing_professional_prerequisites_waits_before_engine(self) -> None:
        project, run = self._run("initial_edit")
        engine = _FakeEngine()
        worker = WorkflowWorker(
            self.projects,
            self.queue,
            evidence_builder=_StubEvidenceBuilder(),
            engine_client_factory=lambda: engine,
        )

        self.assertTrue(worker.run_once())

        current_run = self.projects.get_run(run["id"])
        self.assertEqual("waiting_user", current_run["status"])
        self.assertEqual("professional_prerequisites_missing", current_run["failure"]["code"])
        self.assertEqual("waiting_user", self.projects.get_project(project["id"])["status"])
        self.assertEqual([], engine.calls)
        self.assertEqual([], self.projects.list_video_artifacts(project["id"], run_id=run["id"]))
        with self.database.connection() as connection:
            task = connection.execute("SELECT status FROM tasks WHERE run_id = ?", (run["id"],)).fetchone()
        self.assertEqual("waiting_user", task["status"])

    def _run(self, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        brief = {"testing_preset": "fragment_montage", "timeline_fps": 30} if kind == "engine_smoke" else {}
        project = self.projects.create_project(kind, brief)
        paths = self.projects.project_paths(project["id"])
        source = paths["source"] / "asset.mp4"
        working = paths["working"] / "asset.working.mp4"
        source.write_bytes(b"source")
        working.write_bytes(b"working")
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO assets(
                    id, project_id, original_name, role, state, source_path, working_path,
                    content_hash, working_hash, probe_json, warnings_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, '[]', ?, ?)
                """,
                (
                    f"asset-{kind}",
                    project["id"],
                    "asset.mp4",
                    "primary",
                    str(source),
                    str(working),
                    sha256_file(source),
                    sha256_file(working),
                    json.dumps({"duration_seconds": 10, "audio_streams": 1, "video_streams": 1}),
                    now,
                    now,
                ),
            )
        return project, self.projects.freeze_run(project["id"], kind=kind)
