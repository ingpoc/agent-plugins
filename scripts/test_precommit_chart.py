"""The commit hooks keep the batched-change-list state chart current (BCL-155)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = "plugins/batched-change-list/skills/batched-change-list"


class PrecommitChartTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        shutil.copytree(ROOT / SKILL, self.repo / SKILL, ignore=shutil.ignore_patterns("__pycache__"))
        for path in ("scripts/precommit_chart.py", ".githooks/pre-commit", ".githooks/post-merge"):
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, self.repo / path)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "base")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def chart_current(self, rev="HEAD"):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        archive = subprocess.run(["git", "archive", rev, SKILL], cwd=self.repo, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(scratch)], input=archive, check=True)
        return subprocess.run(["python3", str(scratch / SKILL / "scripts/statechart.py"), "--check"],
                              capture_output=True).returncode == 0

    def edit_reference(self):
        path = self.repo / SKILL / "references/review.md"
        path.write_text(path.read_text() + "\nA new rule.\n")

    def test_commit_that_changes_a_chart_source_carries_the_regenerated_chart(self):
        self.assertTrue(self.chart_current())
        self.edit_reference()
        self.git("add", f"{SKILL}/references/review.md")
        self.git("-c", "core.hooksPath=.githooks", "commit", "-q", "-m", "rule")
        self.assertIn(f"{SKILL}/statechart.html", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertTrue(self.chart_current())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(len(self.git("log", "-1", "--format=%p").split()), 1)

    def test_merge_of_a_branch_with_a_stale_chart_regenerates_it(self):
        self.git("checkout", "-q", "-b", "side")
        self.edit_reference()
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-am", "rule without chart")
        self.assertFalse(self.chart_current())
        self.git("checkout", "-q", "main")
        (self.repo / "notes.md").write_text("x\n")
        self.git("add", "notes.md")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "main moved")
        self.git("-c", "core.hooksPath=.githooks", "merge", "-q", "--no-edit", "side")
        self.assertTrue(self.chart_current())
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Regenerate the batched-change-list state chart after merge\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(len(self.git("log", "-1", "--format=%p").split()), 1)

    def test_commit_outside_the_skill_leaves_the_chart_alone(self):
        (self.repo / "notes.md").write_text("x\n")
        self.git("add", "notes.md")
        self.git("-c", "core.hooksPath=.githooks", "commit", "-q", "-m", "notes")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["notes.md"])


if __name__ == "__main__":
    unittest.main()
