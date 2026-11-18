import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import check_gate_count
import manage_findings
import render
import workflow_definition

SCRIPT = Path(__file__).with_name("manage_findings.py")
EXAMPLE = Path(__file__).parents[1] / "examples/findings.json"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.data = self.root / "findings.json"
        self.page = self.root / "findings.html"
        self.template = json.loads(EXAMPLE.read_text(encoding="utf-8"))["findings"][0]

    def finding(self, identifier, status="untested", *, introspect=False, round_number=1):
        item = copy.deepcopy(self.template)
        item.update(id=identifier, title=f"Finding {identifier}", status=status,
                    observed=f"Observed {identifier}", verification="Recorded verification.",
                    verification_refs=["receipt"])
        if introspect:
            item["title"] = f"Introspect 2026-11-18: {identifier}"
            item["details"] = {"fixed_in": "Introspect"}
            item["cycle"], item["round"] = 1, round_number
            item["steps"] = [f"Stage: Verify {round_number}"]
            item["none"] = item["steps"].copy()
            item["sections"] = {name: {"none": f"Reviewed {name}."}
                                 for name in ("delete", "simplify", "optimize", "automate")}
        else:
            item["details"] = {}
        return item

    def gate_findings(self):
        count = max(workflow_definition.DEFAULT_ROLLOVER_THRESHOLD,
                    workflow_definition.MERGE_PREVIOUS_PR_THRESHOLD)
        return [self.finding(f"FIX-{i:02d}", "verified_fixed") for i in range(count)]

    def large_tracker(self):
        closed = [self.finding(f"OLD-{i:02d}", "verified_fixed") for i in range(1, 68)]
        introspects = [self.finding(f"WF-{i:02d}", "verified_fixed", introspect=True, round_number=i)
                       for i in range(6, 23)]
        blockers = [self.finding(f"HW-{i:02d}", "blocked") for i in range(1, 5)]
        current = [self.finding(f"OPEN-{i:02d}") for i in range(1, 12)]
        task = "WF-22"
        tracking = {
            "cycle": 1,
            "round": 22,
            "workflow": {"stage": "verify", "updated_at": "2026-11-18T00:00:00Z"},
            "stages_reached": ["install"],
            "pull_request": {"label": "PR #115", "url": "https://example.test/repo/pull/115"},
            "rollover": {"excluded_ids": [item["id"] for item in closed + introspects]},
            "introspect": {"task": task, "result": task, "at": "2026-11-18T00:00:00Z",
                           "steps": ["Stage: Verify 22"], "none": ["Stage: Verify 22"],
                           "sections": introspects[-1]["sections"], "cycle": 1, "round": 22},
        }
        document = {"title": "Migration fixture", "findings": closed + introspects + blockers + current,
                    "tracking": tracking}
        self.assertEqual(len(document["findings"]), 99)
        return document

    def write_tracker(self, document, path=None):
        path = path or self.data
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def batch(self, operations, *, data=None, render=True, force=False):
        operations_file = self.root / "operations.json"
        operations_file.write_text(json.dumps(operations), encoding="utf-8")
        command = [sys.executable, str(SCRIPT), "batch", str(data or self.data), str(operations_file)]
        if render:
            command.extend(["--render", str(self.page), "--base", str(self.root)])
        if force:
            command.append("--force")
        return subprocess.run(command, text=True, capture_output=True,
                              env={**os.environ, "BCL_STATE_DIR": str(self.state)})

    def register_live(self, document, identifier="115"):
        live = self.state / "example--repo" / f"{identifier}.json"
        self.write_tracker(document, live)
        record = {"live": str(live), "home": str(self.root),
                  "snapshot": f"docs/triage/pr-{identifier}-findings.json",
                  "html": f"docs/triage/pr-{identifier}.html",
                  "repo": "example/repo", "id": identifier}
        key = f"example/repo/{identifier}"
        registry = self.state / "registry.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({key: record}), encoding="utf-8")
        return live

    def test_archive_keeps_gate_count(self):
        document = self.large_tracker()
        self.write_tracker(document)
        previous = {"id": "OLD-ARCHIVE", "title": "Earlier archive entry",
                    "archived_from_pr": 114, "archived_at": "2026-11-17T00:00:00Z"}
        retry = {**document["findings"][0], "archived_from_pr": 115,
                 "archived_at": "2026-11-17T00:00:00Z"}
        (self.root / "archive.json").write_text(json.dumps([previous, retry]), encoding="utf-8")
        before = check_gate_count.gate_count(document)
        result = self.batch([{"action": "archive"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual(check_gate_count.gate_count(saved), before)
        self.assertEqual(len(saved["findings"]), 15)
        archive = json.loads((self.root / "archive.json").read_text())
        self.assertEqual(len(archive), 68)
        self.assertEqual(archive[0], previous)
        self.assertEqual(archive[1], retry)
        self.assertEqual(sum(row["id"] == "OLD-01" for row in archive), 1)
        self.assertEqual(saved["tracking"]["archive"]["count"], 68)
        self.assertEqual(len(saved["tracking"]["introspect_history"]), 17)

    def test_archive_moves_wf_introspect_records(self):
        wf = self.finding("WF-01", "verified_fixed", introspect=True)
        real_wf = self.finding("WF-02", "verified_fixed")
        document = {"title": "Archive", "findings": [wf, real_wf, self.finding("OPEN-01")],
                    "tracking": {"round": 1, "cycle": 1, "rollover": {"excluded_ids": ["WF-01"]},
                                 "pull_request": {"label": "PR #115", "url": "https://example.test/pull/115"},
                                 "introspect": {"task": "WF-01", "result": "WF-01", "at": "2026-11-18T00:00:00Z",
                                                "steps": wf["steps"], "none": wf["none"],
                                                "sections": wf["sections"], "round": 1, "cycle": 1}}}
        self.write_tracker(document)
        before = check_gate_count.gate_count(document)
        self.assertEqual(before[0], 1)
        result = self.batch([{"action": "archive"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual(check_gate_count.gate_count(saved), before)
        self.assertEqual([item["id"] for item in saved["findings"]], ["WF-02", "OPEN-01"])
        self.assertEqual(saved["tracking"]["introspect_history"][0]["source_task"], "WF-01")
        self.assertFalse((self.root / "archive.json").exists())

    def test_archive_matches_0_1_38_gate_for_mixed_wf_findings(self):
        introspects = [self.finding(f"WF-{number:02}", "verified_fixed", introspect=True)
                       for number in (1, 2)]
        real = self.finding("WF-03", "verified_fixed")
        document = {"title": "Mixed WF gate", "findings": introspects + [real, self.finding("OPEN")],
                    "tracking": {"rollover": {"excluded_ids": ["WF-01", "WF-02"]}}}
        self.write_tracker(document)
        # The 0.1.38 gate counts only WF-03: one fixed, threshold 15, open work remains.
        expected = (1, 15, False)
        self.assertEqual(check_gate_count.gate_count(document), expected)
        result = self.batch([{"action": "archive"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual(check_gate_count.gate_count(saved), expected)
        self.assertEqual([item["id"] for item in saved["findings"]], ["WF-03", "OPEN"])

    def test_legacy_introspect_preserves_provenance_and_unattributed_never_covers(self):
        unknown = self.finding("WF-01", "verified_fixed", introspect=True)
        unknown.pop("cycle"); unknown.pop("round")
        unknown["fixed_at"] = "2025-02-01T03:04:05Z"
        partial = self.finding("WF-02", "verified_fixed", introspect=True)
        partial.pop("cycle"); partial.pop("round")
        partial["details"].update(cycle=2, time="2025-02-02T04:05:06Z")
        known = self.finding("WF-03", "verified_fixed", introspect=True, round_number=3)
        unknown_time = self.finding("WF-04", "verified_fixed", introspect=True)
        unknown_time.pop("cycle"); unknown_time.pop("round")
        unknown_time["title"] = "Legacy Introspect"
        document = {"title": "Legacy history", "findings": [unknown, partial, known, unknown_time],
                    "tracking": {"cycle": 2, "round": 7, "stages_reached": ["install"],
                                 "rollover": {"excluded_ids": ["WF-01", "WF-02", "WF-03", "WF-04"]}}}
        self.write_tracker(document)
        before = check_gate_count.gate_count(document)
        result = self.batch([{"action": "archive"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        records = {record["source_task"]: record for record in saved["tracking"]["introspect_history"]}
        self.assertEqual((records["WF-01"]["cycle"], records["WF-01"]["round"],
                          records["WF-01"]["at"], records["WF-01"]["fixed_at"], records["WF-01"]["attribution"]),
                         (None, None, unknown["fixed_at"], unknown["fixed_at"], "unattributed"))
        self.assertEqual((records["WF-02"]["cycle"], records["WF-02"]["round"],
                          records["WF-02"]["at"], records["WF-02"]["time"]),
                         (2, None, partial["details"]["time"], partial["details"]["time"]))
        self.assertEqual((records["WF-03"]["cycle"], records["WF-03"]["round"]), (1, 3))
        self.assertIsNone(records["WF-04"]["at"])
        self.assertIsNone(render.introspect_record_for_round(saved["tracking"]))
        self.assertEqual(check_gate_count.gate_count(saved), before)
        records["WF-01"].update(cycle=2, round=7)
        self.assertIsNone(render.introspect_record_for_round(saved["tracking"]))

    def test_conflicting_legacy_provenance_is_rejected_without_mutation(self):
        for field in ("cycle", "fixed_at"):
            with self.subTest(field=field):
                item = self.finding("WF-01", "verified_fixed", introspect=True)
                if field == "cycle":
                    item["details"][field] = 2
                else:
                    item[field], item["details"][field] = "2025-01-01", "2025-01-02"
                document = {"title": "Conflicting history", "findings": [item],
                            "tracking": {"rollover": {"excluded_ids": ["WF-01"]}}}
                self.write_tracker(document)
                original = self.data.read_bytes()
                result = self.batch([{"action": "archive"}])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"conflicting {field}", result.stderr)
                self.assertEqual(self.data.read_bytes(), original)
                self.assertFalse((self.root / "archive.json").exists())

    def test_identical_unattributed_records_keep_both_source_ids(self):
        first = self.finding("WF-01", "verified_fixed", introspect=True)
        first.pop("cycle"); first.pop("round")
        second = copy.deepcopy(first)
        second["id"] = "WF-02"
        document = {"title": "Duplicate content", "findings": [first, second],
                    "tracking": {"rollover": {"excluded_ids": ["WF-01", "WF-02"]}}}
        self.write_tracker(document)
        result = self.batch([{"action": "archive"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        history = json.loads(self.data.read_text())["tracking"]["introspect_history"]
        self.assertEqual([record["source_task"] for record in history], ["WF-01", "WF-02"])

    def test_rollover_archives_verified_history(self):
        document = self.large_tracker()
        live = self.register_live(document)
        before = live.read_bytes()
        operations = [{"action": "rollover", "source_pr": 115, "worktree": "next-wt",
                       "pull_request": {"label": "PR #116", "url": "https://example.test/repo/pull/116"}}]
        result = self.batch(operations, data=live, render=False, force=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(live.read_bytes(), before)
        new_live = live.parent / "116.json"
        saved = json.loads(new_live.read_text())
        archive = json.loads((live.parent / "archive.json").read_text())
        self.assertEqual(len(archive), 67)
        self.assertEqual(len(saved["findings"]), 15)
        self.assertEqual(len(saved["tracking"]["introspect_history"]), 17)
        self.assertEqual(saved["tracking"]["archive"]["count"], 67)
        self.assertEqual(saved["tracking"]["rollover"]["excluded_ids"], [])

    def test_rejected_rollover_never_changes_archive(self):
        for rejection in ("gate", "target"):
            with self.subTest(rejection=rejection):
                old = self.finding("OLD", "verified_fixed")
                findings = ([old, self.finding("OPEN")] if rejection == "gate"
                            else [old, *self.gate_findings()])
                document = {"title": "Rollover", "findings": findings,
                            "tracking": {"pull_request": {"label": "PR #115", "url": "https://example.test/pull/115"},
                                         "rollover": {"excluded_ids": ["OLD"]}}}
                live = self.register_live(document)
                original = live.read_bytes()
                archive_path = live.parent / "archive.json"
                archive_path.write_text('[{"id":"PREVIOUS"}]\n')
                archive_original = archive_path.read_bytes()
                modified = archive_path.stat().st_mtime_ns
                target = live.parent / "116.json"
                if rejection == "target":
                    target.write_text("existing target")
                result = self.batch([{"action": "rollover", "source_pr": 115, "worktree": "next-wt",
                                      "pull_request": {"label": "PR #116", "url": "https://example.test/pull/116"}}],
                                    data=live, render=False, force=rejection == "target")
                self.assertNotEqual(result.returncode, 0)
                expected = "rollover gate not met" if rejection == "gate" else "new live list already exists"
                self.assertIn(expected, result.stderr)
                self.assertEqual(live.read_bytes(), original)
                self.assertEqual(archive_path.read_bytes(), archive_original)
                self.assertEqual(archive_path.stat().st_mtime_ns, modified)

    def test_rollover_restores_archive_if_registration_fails(self):
        for existed in (False, True):
            with self.subTest(existed=existed):
                old = self.finding("OLD", "verified_fixed")
                document = {"title": "Rollover", "findings": [old, *self.gate_findings()],
                            "tracking": {"pull_request": {"label": "PR #115", "url": "https://example.test/pull/115"},
                                         "rollover": {"excluded_ids": ["OLD"]}}}
                live = self.register_live(document)
                original = live.read_bytes()
                archive_path = live.parent / "archive.json"
                if existed:
                    archive_path.write_text('[{"id":"PREVIOUS"}]\n')
                    archive_original = archive_path.read_bytes()
                else:
                    archive_path.unlink(missing_ok=True)
                operation = {"action": "rollover", "source_pr": 115, "worktree": "next-wt",
                             "pull_request": {"label": "PR #116", "url": "https://example.test/pull/116"}}
                registry_original = (self.state / "registry.json").read_bytes()
                with mock.patch.object(manage_findings.live_state, "STATE", self.state), \
                     mock.patch.object(manage_findings.live_state, "save_registry", side_effect=OSError("registration failed")):
                    with self.assertRaisesRegex(OSError, "registration failed"):
                        manage_findings.rollover_live(live, operation, force=True)
                self.assertEqual(live.read_bytes(), original)
                self.assertFalse((live.parent / "116.json").exists())
                self.assertEqual((self.state / "registry.json").read_bytes(), registry_original)
                self.assertEqual(archive_path.exists(), existed)
                if existed:
                    self.assertEqual(archive_path.read_bytes(), archive_original)

    def test_sibling_trackers_serialize_archive_and_park_writes(self):
        worker = '''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import manage_findings
marker, release, sidecar, data, operations, page, base = sys.argv[2:9]
read = manage_findings.read_sibling_json
def hold(path):
    rows = read(path)
    if path.name == sidecar:
        Path(marker).touch()
        deadline = time.monotonic() + 15
        while not Path(release).exists():
            if time.monotonic() > deadline: raise RuntimeError("release timed out")
            time.sleep(0.01)
    return rows
manage_findings.read_sibling_json = hold
sys.argv = ["manage_findings.py", "batch", data, operations, "--render", page, "--base", base]
sys.exit(manage_findings.main())
'''
        for action in ("archive", "park"):
            with self.subTest(action=action):
                repo = self.root / action
                repo.mkdir()
                marker, release = repo / "reading", repo / "release"
                for index in (1, 2):
                    identifier = f"ITEM-{index}"
                    status = "verified_fixed" if action == "archive" else "blocked"
                    document = {"title": "Concurrent", "findings": [self.finding(identifier, status)],
                                "tracking": {"rollover": {"excluded_ids": [identifier] if action == "archive" else []}}}
                    self.write_tracker(document, repo / f"{index}.json")
                    operation = {"action": action}
                    if action == "park":
                        operation.update(ids=[identifier], needs="hardware")
                    (repo / f"ops-{index}.json").write_text(json.dumps([operation]))
                env = {**os.environ, "BCL_STATE_DIR": str(self.state)}
                first = subprocess.Popen([sys.executable, "-c", worker, str(SCRIPT.parent), str(marker), str(release),
                                          f"{action if action == 'archive' else 'parked'}.json", str(repo / "1.json"),
                                          str(repo / "ops-1.json"), str(repo / "1.html"), str(repo)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                second = None
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and first.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), "first writer did not reach the sidecar")
                    second = subprocess.Popen([sys.executable, str(SCRIPT), "batch", str(repo / "2.json"),
                                               str(repo / "ops-2.json"), "--render", str(repo / "2.html"),
                                               "--base", str(repo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              text=True, env=env)
                    try:
                        second.wait(timeout=0.75)
                        finished_early = True
                    except subprocess.TimeoutExpired:
                        finished_early = False
                finally:
                    release.touch()
                    out1, err1 = first.communicate(timeout=20)
                    if second is not None:
                        out2, err2 = second.communicate(timeout=20)
                self.assertFalse(finished_early, "sibling writer passed the shared lock")
                self.assertEqual(first.returncode, 0, out1 + err1)
                self.assertEqual(second.returncode, 0, out2 + err2)
                sidecar = repo / ("archive.json" if action == "archive" else "parked.json")
                self.assertEqual({row["id"] for row in json.loads(sidecar.read_text())}, {"ITEM-1", "ITEM-2"})

    def test_park_moves_blocked_to_parked_file(self):
        document = {"title": "Park", "findings": [self.finding("MIC", "blocked")],
                    "tracking": {"pull_request": {"label": "PR #115", "url": "https://example.test/pull/115"},
                                 "rollover": {"excluded_ids": []}}}
        self.write_tracker(document)
        result = self.batch([{"action": "park", "ids": ["MIC"], "needs": "real microphone"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        parked = json.loads((self.root / "parked.json").read_text())
        self.assertEqual(saved["findings"], [])
        self.assertEqual((parked[0]["id"], parked[0]["status"], parked[0]["needs"],
                          parked[0]["parked_from_pr"]), ("MIC", "blocked", "real microphone", 115))
        self.assertTrue(parked[0]["parked_at"])
        self.assertIn("1 parked findings in", self.page.read_text())
        self.assertIn("needs: real microphone", self.page.read_text())

    def test_park_rejects_non_blocked_findings(self):
        document = {"title": "Park", "findings": [self.finding("OPEN-01")],
                    "tracking": {"rollover": {"excluded_ids": []}}}
        self.write_tracker(document)
        before = self.data.read_bytes()
        result = self.batch([{"action": "park", "ids": ["OPEN-01"], "needs": "device"}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only blocked findings", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        self.assertFalse((self.root / "parked.json").exists())

    def test_park_retry_keeps_existing_sidecar_entry(self):
        item = self.finding("MIC", "blocked")
        document = {"title": "Retry park", "findings": [item],
                    "tracking": {"rollover": {"excluded_ids": []}}}
        self.write_tracker(document)
        existing = {**item, "needs": "real microphone", "parked_at": "earlier",
                    "parked_from_pr": 114}
        (self.root / "parked.json").write_text(json.dumps([existing]), encoding="utf-8")
        result = self.batch([{"action": "park", "ids": ["MIC"], "needs": "real microphone"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "parked.json").read_text()), [existing])
        self.assertEqual(json.loads(self.data.read_text())["findings"], [])

    def test_unpark_restores_blocked(self):
        document = {"title": "Unpark", "findings": [self.finding("MIC", "blocked")],
                    "tracking": {"rollover": {"excluded_ids": []}}}
        self.write_tracker(document)
        parked = self.batch([{"action": "park", "ids": ["MIC"], "needs": "real microphone"}])
        self.assertEqual(parked.returncode, 0, parked.stderr)
        restored = self.batch([{"action": "unpark", "ids": ["MIC"]}])
        self.assertEqual(restored.returncode, 0, restored.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual([(item["id"], item["status"]) for item in saved["findings"]], [("MIC", "blocked")])
        self.assertEqual(json.loads((self.root / "parked.json").read_text()), [])
        self.assertNotIn("parked", saved["tracking"])

    def test_unpark_retry_cleans_duplicate_live_and_parked_copy(self):
        item = self.finding("MIC", "blocked")
        document = {"title": "Retry unpark", "findings": [item],
                    "tracking": {"rollover": {"excluded_ids": []}}}
        self.write_tracker(document)
        parked = {**item, "needs": "real microphone", "parked_at": "earlier",
                  "parked_from_pr": 114}
        (self.root / "parked.json").write_text(json.dumps([parked]), encoding="utf-8")
        result = self.batch([{"action": "unpark", "ids": ["MIC"]}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row["id"] for row in json.loads(self.data.read_text())["findings"]], ["MIC"])
        self.assertEqual(json.loads((self.root / "parked.json").read_text()), [])

    def test_parked_not_counted_open(self):
        document = {"title": "Gate", "findings": [self.finding("MIC", "blocked")],
                    "tracking": {"rollover": {"excluded_ids": []}}}
        self.write_tracker(document)
        before = check_gate_count.gate_count(document)
        self.assertFalse(before[2])
        result = self.batch([{"action": "park", "ids": ["MIC"], "needs": "real microphone"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        after = check_gate_count.gate_count(json.loads(self.data.read_text()))
        self.assertEqual(after, (0, 15, True))

    def test_rollover_keeps_parked_out_of_new_list(self):
        document = {"title": "Park before rollover", "findings": [self.finding("MIC", "blocked"), *self.gate_findings()],
                    "tracking": {"pull_request": {"label": "PR #115", "url": "https://example.test/repo/pull/115"},
                                 "rollover": {"excluded_ids": []}}}
        live = self.register_live(document)
        result = self.batch([{"action": "park", "ids": ["MIC"], "needs": "real microphone"}], data=live)
        self.assertEqual(result.returncode, 0, result.stderr)
        rollover = [{"action": "rollover", "source_pr": 115, "worktree": "next-wt",
                     "pull_request": {"label": "PR #116", "url": "https://example.test/repo/pull/116"}}]
        result = self.batch(rollover, data=live, render=False, force=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads((live.parent / "116.json").read_text())
        parked = json.loads((live.parent / "parked.json").read_text())
        self.assertEqual(saved["findings"], [])
        self.assertEqual([item["id"] for item in parked], ["MIC"])
        self.assertEqual(saved["tracking"]["parked"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
