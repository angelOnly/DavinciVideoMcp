from __future__ import annotations

import unittest

from davinci_app.editorial.pipeline import ProfessionalPipelineBlocked, _require_valid_source_understanding


class SourceUnderstandingValidationTests(unittest.TestCase):
    def test_accepts_reachable_semantic_unit_and_declared_unknown(self) -> None:
        source = {
            "semantic_units": [
                {
                    "asset_id": "asset-1",
                    "start_seconds": 1.0,
                    "end_seconds": 4.0,
                    "evidence_references": ["funasr:asset-1@1.0-4.0"],
                }
            ],
            "relationships": [],
            "evidence_gaps": [],
            "unknowns": [
                {
                    "asset_id": None,
                    "start_seconds": None,
                    "end_seconds": None,
                    "question": "缺少用户目标",
                    "reason": "素材事实不足以判断选片。",
                }
            ],
        }

        _require_valid_source_understanding(source, _evidence())

    def test_rejects_unreachable_range_or_missing_evidence_reference(self) -> None:
        source = {
            "semantic_units": [
                {
                    "asset_id": "asset-1",
                    "start_seconds": 1.0,
                    "end_seconds": 12.0,
                    "evidence_references": [],
                }
            ],
            "relationships": [],
            "evidence_gaps": [],
            "unknowns": [],
        }

        with self.assertRaises(ProfessionalPipelineBlocked) as captured:
            _require_valid_source_understanding(source, _evidence())

        self.assertEqual("source_understanding", captured.exception.stage)
        self.assertIn("语义单元 0", str(captured.exception))


def _evidence() -> dict[str, object]:
    return {"assets": [{"asset_id": "asset-1", "duration_seconds": 10.0}]}
