from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from davinci_engine.creative.adapters import CreativeAdapterError, default_adapter_registry


_CUBE = """TITLE \"Unit test\"
LUT_3D_SIZE 2
0 0 0
7.62951e-05 0 0
0 1 0
1 1 0
0 0 1
1 0 1
0 1 1
1 1 1
"""

_SAFE_FUSION_EFFECT = """{
Tools = ordered() {
    EffectRoot = MacroOperator {
        Inputs = ordered() {
            MainInput1 = InstanceInput { },
        },
        Outputs = ordered() {
            MainOutput1 = InstanceOutput { },
        },
    },
}
}"""


class CreativeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = default_adapter_registry()

    def test_lut_preflight_and_managed_deployment_normalize_scientific_notation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "look.cube"
            source.write_text(_CUBE, encoding="utf-8")

            adapter = self.registry.get("lut_3d")
            preflight = adapter.probe(source, {})
            deployment = adapter.install_or_deploy(
                "color.unit-look",
                source,
                {},
                {"allow_global_deploy": True, "lut_root": root / "resolve-luts"},
            )

            self.assertTrue(preflight.ready_for_live_certification)
            self.assertTrue(deployment.requires_resolve_restart)
            self.assertEqual("DavinciMcp\\DavinciMcp_color.unit-look.cube", deployment.installed_relative_path)
            self.assertNotIn("e-", Path(str(deployment.installed_path)).read_text(encoding="utf-8").lower())

    def test_fusion_effect_preflight_accepts_only_single_input_static_macro(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "effect.setting"
            source.write_text(_SAFE_FUSION_EFFECT, encoding="utf-8")

            result = self.registry.preflight("fusion_effect", source, {})

            self.assertTrue(result.ready_for_live_certification)
            self.assertEqual("MainInput1", result.details["input_port"])
            self.assertEqual("MainOutput1", result.details["output_port"])

    def test_fusion_effect_preflight_rejects_script_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "unsafe.setting"
            source.write_text(_SAFE_FUSION_EFFECT + "\nRunScript('not allowed')", encoding="utf-8")

            result = self.registry.preflight("fusion_effect", source, {})

            self.assertFalse(result.ready_for_live_certification)
            self.assertIn("脚本", result.reason or "")

    def test_font_preflight_requires_resolve_font_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "font.ttf"
            source.write_bytes(b"not-a-real-font-but-static-preflight-only")

            result = self.registry.preflight("font_file", source, {})

            self.assertFalse(result.ready_for_live_certification)
            self.assertIn("家族名", result.reason or "")

    def test_unknown_mechanism_is_rejected(self) -> None:
        with self.assertRaisesRegex(CreativeAdapterError, "没有安全 Adapter"):
            self.registry.get("fusion_transition")


if __name__ == "__main__":
    unittest.main()
