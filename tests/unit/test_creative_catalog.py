from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from davinci_app.creative.catalog import CapabilityBindingUnavailable, CreativeCatalog


class CreativeCatalogTests(unittest.TestCase):
    def test_uncertified_capability_cannot_enter_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = CreativeCatalog(root / "catalog.db", root / "certified", root / "cache")
            catalog.initialize()

            self.assertEqual(0, catalog.certified_count())
            with self.assertRaisesRegex(CapabilityBindingUnavailable, "未处于 certified"):
                catalog.bind(
                    {"capability_requests": [{"capability_id": "uncertified-font", "parameters": {}}]},
                    edit_plan_digest="plan",
                )

    def test_certified_capability_is_localized_by_hash_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            certified_root = root / "certified"
            source = certified_root / "audio" / "cue.wav"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"verified-audio")
            content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            catalog = CreativeCatalog(root / "catalog.db", certified_root, root / "cache")
            catalog.register(
                capability_id="audio.cue.one",
                category="audio",
                mechanism="audio_asset",
                display_name="提示音",
                source_path=source,
                content_hash=content_hash,
                state="certified",
                description="短提示音",
                constraints={"supports_language": "none"},
                certification={
                    "steps": {
                        "discover": True,
                        "deploy": True,
                        "execute": True,
                        "readback": True,
                        "render": True,
                    }
                },
            )

            binding = catalog.bind(
                {"capability_requests": [{"capability_id": "audio.cue.one", "purpose": "强调"}]},
                edit_plan_digest="plan-digest",
            )

            item = binding["bindings"][0]
            self.assertEqual("audio_asset", item["mechanism"])
            self.assertEqual(content_hash, item["content_hash"])
            self.assertTrue(Path(item["cache_path"]).is_file())
            self.assertEqual("content_addressed_local_cache_v1", item["localization"]["strategy"])
