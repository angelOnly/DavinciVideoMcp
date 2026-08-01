from __future__ import annotations

import re
import unittest
from pathlib import Path


class DependencyBoundaryTests(unittest.TestCase):
    def test_core_modules_do_not_import_external_runtime_sdks(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src" / "davinci_app"
        forbidden_imports = ("DaVinciResolveScript", "FastAPI", "funasr", "openai", "mcp")
        for directory in (root / "project", root / "media", root / "execution"):
            for source in directory.rglob("*.py"):
                content = source.read_text(encoding="utf-8")
                for token in forbidden_imports:
                    pattern = rf"^\s*(?:from|import)\s+{re.escape(token)}(?:\.|\s|$)"
                    self.assertIsNone(re.search(pattern, content, flags=re.MULTILINE), f"{source} 不能直接依赖 {token}")
