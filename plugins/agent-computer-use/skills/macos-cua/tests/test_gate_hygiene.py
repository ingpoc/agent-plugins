"""Stale-reference lint and gate cleanup evidence survival (offline)."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[3]
CONTRACT = json.loads((ROOT / "hardening-contract.json").read_text())
_spec = importlib.util.spec_from_file_location("gate_cleanup", ROOT / "scripts" / "gate_cleanup.py")
gate_cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_cleanup)
_release_spec = importlib.util.spec_from_file_location(
    "release_agent_computer_use", REPO_ROOT / "scripts" / "release_agent_computer_use.py"
)
release = importlib.util.module_from_spec(_release_spec)
_release_spec.loader.exec_module(release)
_hygiene_spec = importlib.util.spec_from_file_location("machine_hygiene", REPO_ROOT / "scripts" / "hygiene.py")
hygiene = importlib.util.module_from_spec(_hygiene_spec)
_hygiene_spec.loader.exec_module(hygiene)

RETIRED = re.compile(r"\.local/bin/cua-driver")
# Known legacy CUA_DRIVER defaults (driver_status still shells it); remove, never extend.
RETIRED_ALLOW = {"scripts/macos-cua.py", "scripts/workflow.py", "scripts/window_resolve.py"}
GATE_OWNERS = ("scripts/runtime_doctor.py", "scripts/validator_live.py", "scripts/gate_cleanup.py")


def _sources():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT).as_posix()
        if path.is_file() and path.suffix in {".py", ".md", ".sh", ".json"} and not any(
            part in {".build", "__pycache__", ".pytest_cache"} for part in path.parts
        ) and rel != "tests/test_gate_hygiene.py":
            yield rel, path.read_text(errors="ignore")


class StaleReferenceTests(unittest.TestCase):
    def test_no_new_retired_cua_driver_paths(self):
        hits = {rel for rel, text in _sources() if RETIRED.search(text)}
        self.assertEqual(hits - RETIRED_ALLOW, set())

    def test_gate_owners_never_touch_the_retired_driver(self):
        for rel in GATE_OWNERS:
            text = (ROOT / rel).read_text()
            for banned in ("driver_status", "call_driver", "CUA_DRIVER"):
                self.assertNotIn(banned, text, rel)

    def test_contract_commands_point_at_real_files(self):
        for step in [*CONTRACT["steps"], CONTRACT["cleanup"]]:
            for part in step["command"]:
                if part.endswith((".py", ".sh")):
                    self.assertTrue((ROOT / part).is_file(), f"{step.get('name', 'cleanup')}: {part}")

    def test_live_steps_are_not_collected_by_static_discover(self):
        for step in CONTRACT["steps"]:
            for part in step["command"]:
                if part.startswith("tests/test_live_"):
                    self.assertNotIn("TestCase", (ROOT / part).read_text(), part)

    def test_doctor_runs_before_first_live_step(self):
        names = [step["name"] for step in CONTRACT["steps"]]
        self.assertEqual(names[:2], ["static", "doctor"])
        self.assertEqual(CONTRACT["cleanup"]["command"][-1], "scripts/gate_cleanup.py")
        self.assertTrue(CONTRACT["evidence_paths"])


class GateCleanupTests(unittest.TestCase):
    def _contract(self, root, names):
        return {"evidence_paths": [str(Path(root) / n) for n in names]}

    def test_cleanup_keeps_evidence_and_only_runs_read_only_doctor(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "proof.json").write_text("{}")
            calls = []
            def run(*args):
                calls.append(args)
                return {"ok": True, "failed": []}
            report = gate_cleanup.cleanup(self._contract(td, ["proof.json", "absent.png"]), run=run)
        self.assertTrue(report["ok"], report)
        self.assertEqual(calls, [("doctor",)])
        self.assertEqual(len(report["evidence_kept"]), 1)

    def test_cleanup_that_eats_evidence_fails(self):
        with tempfile.TemporaryDirectory() as td:
            proof = Path(td) / "proof.json"
            proof.write_text("{}")
            def run(*args):
                if args == ("doctor",):
                    proof.unlink()
                return {"ok": True}
            report = gate_cleanup.cleanup(self._contract(td, ["proof.json"]), run=run)
        self.assertFalse(report["ok"])
        self.assertEqual(report["evidence_lost"], [str(proof)])

    def test_failed_doctor_is_recorded_not_fatal(self):
        report = gate_cleanup.cleanup({"evidence_paths": []},
                                      run=lambda *a: {"ok": a[0] != "doctor", "failed": ["permissions"]})
        self.assertTrue(report["ok"])
        self.assertFalse(report["doctor"]["ok"])


class ReleaseHygieneTests(unittest.TestCase):
    @staticmethod
    def _git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_linked_worktree_filters_real_scanner_findings_by_owning_clone(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            origin = base / "origin.git"
            seed = base / "seed"
            repos = base / "repos"
            repos.mkdir()
            owner, other = repos / "owner", repos / "other"
            linked_worktree = base / "release-worktree"
            allowed_worktrees = base / "allowed-worktrees"
            allowed_worktrees.mkdir()

            self._git(base, "init", "--bare", "--initial-branch=main", str(origin))
            seed.mkdir()
            self._git(seed, "init", "--initial-branch=main")
            self._git(seed, "config", "user.name", "ACU Hygiene Test")
            self._git(seed, "config", "user.email", "hygiene@example.invalid")
            (seed / "README.md").write_text("base\n")
            self._git(seed, "add", "README.md")
            self._git(seed, "commit", "-m", "base")
            self._git(seed, "remote", "add", "origin", str(origin))
            self._git(seed, "push", "-u", "origin", "main")
            self._git(base, "clone", str(origin), str(owner))
            self._git(base, "clone", str(origin), str(other))

            for repo, branch in ((owner, "owner-local"), (other, "other-local")):
                self._git(repo, "config", "user.name", "ACU Hygiene Test")
                self._git(repo, "config", "user.email", "hygiene@example.invalid")
                self._git(repo, "switch", "-c", branch)
                (repo / f"{branch}.txt").write_text("local commit\n")
                self._git(repo, "add", f"{branch}.txt")
                self._git(repo, "commit", "-m", branch)
            self._git(owner, "switch", "main")
            self._git(owner, "worktree", "add", "-b", "release", str(linked_worktree), "main")
            (other / "dirty.txt").write_text("uncommitted\n")

            manifest = {
                "paths": {
                    "archive": str(base / "archive"),
                    "checkout_root": str(repos),
                    "worktree_root": str(allowed_worktrees),
                },
                "scan": {"roots": [str(repos)], "depth": 3},
            }
            scan = hygiene.Report()
            hygiene.check_duplicate_clones(manifest, scan)
            hygiene.check_worktrees(manifest, scan)
            hygiene.check_local_only_work(manifest, scan)

            local_row = next(row for row in scan.rows if row["check"] == "local-only-work")
            self.assertTrue(any(item.startswith(f"{other}:other-local:") for item in local_row["items"]))
            self.assertTrue(any(item == f"{other}: 1 dirty file(s) remain in place" for item in local_row["items"]))

            findings = release.hygiene_failures(scan.rows, root=linked_worktree)
            local_findings = [item for item in findings if item.startswith("local-only-work:")]
            self.assertEqual(len(local_findings), 1)
            self.assertTrue(local_findings[0].startswith(f"local-only-work: {owner}:owner-local:"))
            self.assertFalse(any(item.startswith(f"local-only-work: {other}:") for item in findings))
            self.assertEqual(sum(item.startswith("duplicate-clones:") for item in findings), 1)
            self.assertIn(f"worktree-placement: {linked_worktree}", findings)


if __name__ == "__main__":
    unittest.main()
