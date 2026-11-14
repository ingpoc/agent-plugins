"""Stale-reference lint and gate cleanup evidence survival (offline)."""
import importlib.util
import json
from pathlib import Path
import re
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
    def test_other_clone_local_only_commits_and_dirty_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            release_clone = (Path(td) / "release" / "agent-plugins").resolve()
            other_clone = (Path(td) / "other" / "agent-plugins").resolve()
            report = [{
                "check": "local-only-work",
                "status": "FAIL",
                "items": [
                    f"{release_clone}:release-branch: 1 local-only commit(s), head abc",
                    f"{release_clone}: 1 dirty file(s) remain in place",
                    f"{other_clone}:other-branch: 2 local-only commit(s), head def",
                    f"{other_clone}: 1 dirty file(s) remain in place",
                ],
            }, {
                "check": "worktree-placement",
                "status": "FAIL",
                "items": [str(other_clone), str(release_clone)],
            }, {
                "check": "duplicate-clones",
                "status": "FAIL",
                "items": [
                    f"https://example.invalid/repo -> {other_clone}",
                    f"https://example.invalid/repo -> {release_clone}, {other_clone}",
                ],
            }]
            findings = release.hygiene_failures(report, root=release_clone)
            self.assertEqual(findings[:3], [
                f"local-only-work: {release_clone}:release-branch: 1 local-only commit(s), head abc",
                f"local-only-work: {release_clone}: 1 dirty file(s) remain in place",
                f"worktree-placement: {release_clone}",
            ])
            duplicates = [finding for finding in findings if finding.startswith("duplicate-clones:")]
            self.assertEqual(len(duplicates), 1)
            self.assertIn(str(release_clone), duplicates[0])


if __name__ == "__main__":
    unittest.main()
