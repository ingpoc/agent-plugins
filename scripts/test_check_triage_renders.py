#!/usr/bin/env python3
"""Filename and PR URL must agree before a triage page is checked."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_pairs_include_older_matching_trackers_for_duplicate_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            triage = Path(tmp)
            older = _write(triage, "pr-113-findings.json", 113)
            current = _write(triage, "pr-112-findings.json", 112)
            pairs = check.triage_render_pairs(triage)
            self.assertEqual(pairs, [
                (current, triage / "pr-112.html"),
                (older, triage / "pr-113.html"),
            ])

    def _checker_fixture(self, triage: Path) -> Path:
        renderer = triage / "render.py"
        renderer.write_text("import pathlib, sys\npathlib.Path(sys.argv[2]).write_bytes(b'fresh')\n")
        return renderer

    def test_current_pr_is_checked_even_if_higher_numbered_tracker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage = root / "docs" / "triage"
            triage.mkdir(parents=True)
            _write(triage, "pr-112-findings.json", 112)
            _write(triage, "pr-113-findings.json", 113)
            (triage / "pr-112.html").write_text("stale")
            (triage / "pr-113.html").write_bytes(b"fresh")
            renderer = self._checker_fixture(root)
            with patch.object(check, "ROOT", root), patch.object(check, "TRIAGE_RENDERER", renderer), \
                    patch.object(check, "current_triage_pr", return_value=112):
                errors = check.check_triage_renders()
            self.assertTrue(any("pr-112.html: differs" in error for error in errors), errors)

    def test_duplicate_trackers_claiming_current_page_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage = root / "docs" / "triage"
            triage.mkdir(parents=True)
            _write(triage, "pr-112-findings.json", 112)
            _write(triage, "legacy-findings.json", 112)
            (triage / "pr-112.html").write_bytes(b"fresh")
            renderer = self._checker_fixture(root)
            with patch.object(check, "ROOT", root), patch.object(check, "TRIAGE_RENDERER", renderer), \
                    patch.object(check, "current_triage_pr", return_value=112):
                errors = check.check_triage_renders()
            self.assertTrue(any("claimed by both" in error and "pr-112.html" in error for error in errors), errors)

    def test_staged_tracker_requires_its_report_in_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage = root / "docs" / "triage"
            triage.mkdir(parents=True)
            tracker = _write(triage, "pr-112-findings.json", 112)
            report = triage / "pr-112.html"
            report.write_bytes(b"fresh")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", str(tracker.relative_to(root))], cwd=root, check=True)
            subprocess.run([
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "tracker",
            ], cwd=root, check=True)
            renderer = self._checker_fixture(root)
            with patch.object(check, "ROOT", root), patch.object(check, "TRIAGE_RENDERER", renderer), \
                    patch.object(check, "current_triage_pr", return_value=112):
                errors = check.check_triage_renders()
            self.assertTrue(any("pr-112.html" in error and "missing from the index" in error
                                for error in errors), errors)

    def test_indexed_tracker_removed_from_working_tree_still_requires_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage = root / "docs" / "triage"
            triage.mkdir(parents=True)
            tracker = _write(triage, "pr-112-findings.json", 112)
            (triage / "pr-112.html").write_bytes(b"fresh")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", str(tracker.relative_to(root))], cwd=root, check=True)
            tracker.unlink()
            renderer = self._checker_fixture(root)
            with patch.object(check, "ROOT", root), patch.object(check, "TRIAGE_RENDERER", renderer), \
                    patch.object(check, "current_triage_pr", return_value=112):
                errors = check.check_triage_renders()
            self.assertTrue(any("pr-112.html" in error and "missing from the index" in error
                                for error in errors), errors)

    def test_index_query_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage = root / "docs" / "triage"
            triage.mkdir(parents=True)
            _write(triage, "pr-112-findings.json", 112)
            renderer = self._checker_fixture(root)
            with patch.object(check, "ROOT", root), patch.object(check, "TRIAGE_RENDERER", renderer), \
                    patch.object(check, "current_triage_pr", return_value=112), \
                    patch.object(check.subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "git")):
                errors = check.check_triage_renders()
            self.assertTrue(any("could not read Git index" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
