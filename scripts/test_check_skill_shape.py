#!/usr/bin/env python3
"""Strict skill-shape failures must fail the repository validation flow."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402


class SkillShapeCheckTests(unittest.TestCase):
    def run_validate_only(self, home: Path, ci: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        plugin = check.ROOT / "plugins" / "agent-computer-use"
        with (
            patch.object(Path, "home", return_value=home),
            patch.dict(os.environ, {"CI": ci}),
            patch.object(check, "selected_plugins", return_value=([], "test")),
            patch.object(check, "plugin_dirs", return_value=[plugin]),
            patch.object(check, "validate_plugin", return_value=[]),
            patch.object(check, "check_catalogs", return_value=[]),
            patch.object(check.release_agent_computer_use, "policy"),
            patch.object(check, "check_host_dest", return_value=[]),
            patch.object(check, "check_triage_renders", return_value=[]),
            patch.object(check.check_inventory, "load_allowlist"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = check.main(["--validate-only"])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_checker_subprocess_failure_fails_validate_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            checker = home / ".agents/skills/skill-refine/scripts/skill_shape.py"
            checker.parent.mkdir(parents=True)
            checker.write_text("import sys\nprint('simulated strict shape failure', file=sys.stderr)\nsys.exit(1)\n")
            result, _, stderr = self.run_validate_only(home, "")
        self.assertEqual(result, 1)
        self.assertIn("simulated strict shape failure", stderr)

    def test_missing_checker_fails_validate_only_outside_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, stderr = self.run_validate_only(Path(directory), "")
        self.assertEqual(result, 1)
        self.assertIn("strict skill_shape checker missing", stderr)

    def test_ci_reports_skip_when_checker_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, stderr = self.run_validate_only(Path(directory), "true")
        self.assertEqual(result, 0)
        self.assertEqual(
            stderr.strip(),
            "strict skill-shape skipped in CI: skill-refine checker is not installed",
        )


if __name__ == "__main__":
    unittest.main()
