from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from davinci_app.common import sha256_file, utc_now
from davinci_app.config import AppConfig
from davinci_app.media.validation import UploadValidator
from davinci_app.persistence import ProductDatabase
from davinci_app.project.service import ProjectService, ProjectStateError


class ProjectArtifactBoundaryTests(unittest.TestCase):
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
        self._sequence = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_mp4_file_without_professional_proof_cannot_be_published(self) -> None:
        project, run = self._run("initial_edit")
        candidate = self._record_file_artifact(run, "candidate_render", plan_digest="plan", finishing_digest="finishing")

        with self.assertRaisesRegex(ProjectStateError, "候选发布被门禁阻止"):
            self.projects.publish_video_version(run["id"], candidate["id"])

        self.assertEqual([], self.projects.list_video_versions(project["id"]))

    def test_technical_preview_can_never_be_published(self) -> None:
        project, run = self._run("engine_smoke")
        preview = self._record_file_artifact(run, "technical_preview", plan_digest=None, finishing_digest=None)

        with self.assertRaisesRegex(ProjectStateError, "绝不能发布"):
            self.projects.publish_video_version(run["id"], preview["id"])

        self.assertEqual([], self.projects.list_video_versions(project["id"]))

    def test_complete_proof_publishes_and_mutated_render_does_not(self) -> None:
        project, run = self._run("initial_edit")
        work = self._record_file_artifact(run, "work_preview", plan_digest="plan", finishing_digest=None)
        candidate = self._record_file_artifact(run, "candidate_render", plan_digest="plan", finishing_digest="finishing")
        self._record_complete_professional_proof(run["id"], work["output_hash"], candidate["output_hash"])

        version = self.projects.publish_video_version(run["id"], candidate["id"])
        self.assertEqual("candidate", version["state"])
        self.assertEqual(1, version["version_number"])

        project2, run2 = self._run("initial_edit")
        work2 = self._record_file_artifact(run2, "work_preview", plan_digest="plan", finishing_digest=None)
        candidate2 = self._record_file_artifact(run2, "candidate_render", plan_digest="plan", finishing_digest="finishing")
        self._record_complete_professional_proof(run2["id"], work2["output_hash"], candidate2["output_hash"])
        Path(candidate2["output_path"]).write_bytes(b"changed-after-verification")

        with self.assertRaisesRegex(ProjectStateError, "验证后发生变化"):
            self.projects.publish_video_version(run2["id"], candidate2["id"])
        self.assertEqual([], self.projects.list_video_versions(project2["id"]))

        project3, run3 = self._run("initial_edit")
        work3 = self._record_file_artifact(run3, "work_preview", plan_digest="plan", finishing_digest=None)
        candidate3 = self._record_file_artifact(run3, "candidate_render", plan_digest="plan", finishing_digest="finishing")
        self._record_complete_professional_proof(run3["id"], work3["output_hash"], candidate3["output_hash"])
        Path(work3["output_path"]).write_bytes(b"work-preview-changed-after-review")

        with self.assertRaisesRegex(ProjectStateError, "工作版在复核后发生变化"):
            self.projects.publish_video_version(run3["id"], candidate3["id"])
        self.assertEqual([], self.projects.list_video_versions(project3["id"]))

    def test_legacy_testing_candidate_is_migrated_to_internal_preview(self) -> None:
        now = utc_now()
        output = self.config.workspace_root / "legacy.mp4"
        output.write_bytes(b"legacy")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO projects(id, title, brief_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("legacy-project", "旧测试", json.dumps({"testing_preset": "fragment_montage"}), "ready_for_review", now, now),
            )
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-run",
                    "legacy-project",
                    json.dumps({"brief": {"testing_preset": "fragment_montage"}}),
                    "succeeded",
                    "initial_edit",
                    now,
                    now,
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO video_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("legacy-version", "legacy-project", "legacy-run", 1, "candidate", str(output), sha256_file(output), "legacy-plan", now),
            )

        self.database.initialize()

        with self.database.connection() as connection:
            version = connection.execute("SELECT state, version_number FROM video_versions WHERE id = 'legacy-version'").fetchone()
            preview = connection.execute(
                "SELECT artifact_type, state FROM video_artifacts WHERE run_id = 'legacy-run'"
            ).fetchone()
            project = connection.execute("SELECT status FROM projects WHERE id = 'legacy-project'").fetchone()
        self.assertEqual("technical_preview_migrated", version["state"])
        self.assertEqual(-1, version["version_number"])
        self.assertEqual(("technical_preview", "verified"), (preview["artifact_type"], preview["state"]))
        self.assertEqual("technical_preview_available", project["status"])

    def _run(self, kind: str) -> tuple[dict[str, object], dict[str, object]]:
        self._sequence += 1
        suffix = f"{kind}-{self._sequence}"
        brief = {"testing_preset": "fragment_montage"} if kind == "engine_smoke" else {}
        project = self.projects.create_project(f"项目-{suffix}", brief)
        paths = self.projects.project_paths(project["id"])
        source = paths["source"] / "asset.mp4"
        working = paths["working"] / "asset.working.mp4"
        source.write_bytes(b"source-video")
        working.write_bytes(b"working-video")
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
                    f"asset-{suffix}",
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
        run = self.projects.freeze_run(project["id"], kind=kind)
        return project, run

    def _record_file_artifact(
        self, run: dict[str, object], artifact_type: str, *, plan_digest: str | None, finishing_digest: str | None
    ) -> dict[str, object]:
        paths = self.projects.project_paths(str(run["project_id"]))
        output = paths["renders"] / str(run["id"]) / f"{artifact_type}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"{artifact_type}-{run['id']}".encode("utf-8"))
        return self.projects.record_video_artifact(
            str(run["id"]),
            artifact_type,
            output,
            plan_digest=plan_digest,
            finishing_digest=finishing_digest,
            verification={"valid": True},
        )

    def _record_complete_professional_proof(self, run_id: str, work_hash: str, candidate_hash: str) -> None:
        artifacts = {
            "evidence_bundle": {},
            "source_understanding": {},
            "editorial_direction": {},
            "sound_advice": {},
            "visual_advice": {},
            "typography_advice": {},
            "edit_plan": {"digest": "plan"},
            "capability_binding": {"edit_plan_digest": "plan", "bindings": []},
            "work_preview_review": {"work_preview_hash": work_hash, "ready_for_finishing": True},
            "finishing_adjustment": {
                "digest": "finishing",
                "edit_plan_digest": "plan",
                "work_preview_hash": work_hash,
                "ready_for_candidate": True,
            },
            "candidate_validation": {
                "candidate_render_hash": candidate_hash,
                "edit_plan_digest": "plan",
                "finishing_digest": "finishing",
                "valid": True,
            },
        }
        for artifact_type, payload in artifacts.items():
            self.projects.record_run_artifact(run_id, artifact_type, payload)
