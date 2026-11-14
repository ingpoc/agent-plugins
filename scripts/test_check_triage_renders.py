#!/usr/bin/env python3
"""Filename and PR URL must agree before a triage page is checked."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check


def _write(root: Path, name: str, pr: int) -> Path:
    path = root / name
    path.write_text(json.dumps({
        "tracking": {"pull_request": {"url": f"https://cursor.com/codebase/example/pull/{pr}"}}
    }))
    return path


class TriageRenderPairsTests(unittest.TestCase):
    def test_skips_findings_file_whose_name_and_pr_url_disagree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            triage = Path(tmp)
            leftover = _write(triage, "pr-87-findings.json", 110)
            current = _write(triage, "pr-111-findings.json", 111)
            (triage / "pr-110.html").write_text("stale-110")
            (triage / "pr-111.html").write_text("page-111")
            pairs = check.triage_render_pairs(triage)
            self.assertEqual(pairs, [(current, triage / "pr-111.html")])
            self.assertTrue(leftover.is_file())

    def test_pairs_when_filename_and_url_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            triage = Path(tmp)
            data = _write(triage, "pr-110-findings.json", 110)
            pairs = check.triage_render_pairs(triage)
            self.assertEqual(pairs, [(data, triage / "pr-110.html")])


if __name__ == "__main__":
    unittest.main()
