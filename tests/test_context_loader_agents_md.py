"""
AGENTS.md / CLAUDE.md discovery in context_loader (codex-parity item 3).

Kim-native names (KIM.md, KIM.local.md, .kim/*) keep precedence; the
ecosystem-standard names are discovered after them, AGENTS.md before
CLAUDE.md.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.context_loader import (
    _INSTRUCTION_FILES,
    discover_instruction_files,
)


class DiscoveryListTests(unittest.TestCase):
    def test_priority_order_in_discovery_list(self):
        names = [str(n) for n in _INSTRUCTION_FILES]
        self.assertLess(names.index("KIM.md"), names.index("AGENTS.md"))
        self.assertLess(names.index("KIM.local.md"), names.index("AGENTS.md"))
        self.assertLess(names.index("AGENTS.md"), names.index("CLAUDE.md"))


class DiscoveryBehaviorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_agents_md_picked_up_when_no_kim_md(self):
        (self.root / "AGENTS.md").write_text("agents instructions", encoding="utf-8")
        files = discover_instruction_files(self.root)
        paths = [f["path"] for f in files]
        self.assertTrue(any(p.endswith("AGENTS.md") for p in paths), paths)

    def test_claude_md_picked_up_when_nothing_else(self):
        (self.root / "CLAUDE.md").write_text("claude instructions", encoding="utf-8")
        files = discover_instruction_files(self.root)
        paths = [f["path"] for f in files]
        self.assertTrue(any(p.endswith("CLAUDE.md") for p in paths), paths)

    def test_kim_md_precedes_agents_md_same_directory(self):
        (self.root / "KIM.md").write_text("kim instructions", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("agents instructions", encoding="utf-8")
        files = discover_instruction_files(self.root)
        names = [Path(f["path"]).name for f in files
                 if Path(f["path"]).parent == self.root]
        self.assertIn("KIM.md", names)
        self.assertIn("AGENTS.md", names)
        self.assertLess(names.index("KIM.md"), names.index("AGENTS.md"))

    def test_agents_md_precedes_claude_md_same_directory(self):
        (self.root / "AGENTS.md").write_text("agents instructions", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("claude instructions", encoding="utf-8")
        files = discover_instruction_files(self.root)
        names = [Path(f["path"]).name for f in files
                 if Path(f["path"]).parent == self.root]
        self.assertLess(names.index("AGENTS.md"), names.index("CLAUDE.md"))

    def test_identical_content_deduped(self):
        (self.root / "KIM.md").write_text("same content", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("same content", encoding="utf-8")
        files = discover_instruction_files(self.root)
        names = [Path(f["path"]).name for f in files
                 if Path(f["path"]).parent == self.root]
        # Dedup by content hash: KIM.md wins, AGENTS.md dropped.
        self.assertEqual(names, ["KIM.md"])


if __name__ == "__main__":
    unittest.main()
