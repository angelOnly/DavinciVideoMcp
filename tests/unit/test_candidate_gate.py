from __future__ import annotations

import copy
import unittest

from davinci_app.project.candidate_gate import (
    REQUIRED_CANDIDATE_STAGES,
    CandidatePublishGateError,
    verify_candidate_publishable,
)


class CandidatePublishGateTests(unittest.TestCase):
    def test_each_required_professional_artifact_is_a_hard_gate(self) -> None:
        stages, videos = _complete_proof()
        for missing in REQUIRED_CANDIDATE_STAGES:
            with self.subTest(missing=missing):
                incomplete = [item for item in stages if item["artifact_type"] != missing]
                with self.assertRaisesRegex(CandidatePublishGateError, missing):
                    verify_candidate_publishable(incomplete, videos, "candidate")

    def test_technical_preview_can_never_substitute_for_candidate_render(self) -> None:
        stages, videos = _complete_proof()
        videos[1] = {
            "id": "candidate",
            "artifact_type": "technical_preview",
            "state": "verified",
            "output_hash": "candidate-hash",
            "plan_digest": "plan-hash",
            "finishing_digest": "finishing-hash",
        }
        with self.assertRaisesRegex(CandidatePublishGateError, "candidate_render"):
            verify_candidate_publishable(stages, videos, "candidate")

    def test_baseline_mismatch_rejects_an_existing_mp4_artifact(self) -> None:
        stages, videos = _complete_proof()
        mismatched = copy.deepcopy(stages)
        next(item for item in mismatched if item["artifact_type"] == "candidate_validation")["payload"][
            "candidate_render_hash"
        ] = "different-file"
        with self.assertRaisesRegex(CandidatePublishGateError, "候选验证没有绑定"):
            verify_candidate_publishable(mismatched, videos, "candidate")


def _complete_proof() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    payloads: dict[str, dict[str, object]] = {name: {} for name in REQUIRED_CANDIDATE_STAGES}
    payloads["edit_plan"] = {"digest": "plan-hash"}
    payloads["capability_binding"] = {"edit_plan_digest": "plan-hash", "bindings": []}
    payloads["work_preview_review"] = {"work_preview_hash": "work-hash", "ready_for_finishing": True}
    payloads["finishing_adjustment"] = {
        "digest": "finishing-hash",
        "edit_plan_digest": "plan-hash",
        "work_preview_hash": "work-hash",
        "ready_for_candidate": True,
    }
    payloads["candidate_validation"] = {
        "candidate_render_hash": "candidate-hash",
        "edit_plan_digest": "plan-hash",
        "finishing_digest": "finishing-hash",
        "valid": True,
    }
    stages = [
        {"artifact_type": name, "state": "succeeded", "payload": payload}
        for name, payload in payloads.items()
    ]
    videos = [
        {
            "id": "work",
            "artifact_type": "work_preview",
            "state": "verified",
            "output_hash": "work-hash",
            "plan_digest": "plan-hash",
            "finishing_digest": None,
        },
        {
            "id": "candidate",
            "artifact_type": "candidate_render",
            "state": "verified",
            "output_hash": "candidate-hash",
            "plan_digest": "plan-hash",
            "finishing_digest": "finishing-hash",
        },
    ]
    return stages, videos
