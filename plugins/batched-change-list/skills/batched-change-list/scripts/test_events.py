import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events


class EventTests(unittest.TestCase):
    def test_job_event_stamps_finding_ids_from_its_plan_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracker = root / "121.json"
            tracker.write_text(json.dumps({"tracking": {"plan": {"groups": [
                {"id": "R2-DO4", "finding_ids": ["DO-4", "DO-12"]}]}}}))
            events.job_event(tracker, "job-1", "start", state_dir=root / "state", group_id="R2-DO4")
            self.assertEqual(events.read(tracker, root / "state")[0]["finding_ids"], ["DO-4", "DO-12"])

    def test_stamped_lifecycle_metadata_is_persisted_without_worktree_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = {"group_id": "lane-7", "round": 3, "model": "gpt-6-luna"}
            events.job_event("repo", "job-1", "start", state_dir=tmp, **metadata)
            events.job_event("repo", "job-1", "finish", state_dir=tmp, **metadata,
                             tokens_source="stamp", tokens={"total": 12})
            raw = [json.loads(line) for line in events.events_path("repo", tmp).read_text().splitlines()]
            self.assertEqual([row["event"] for row in raw], ["start", "finish"])
            for row in raw:
                self.assertEqual({key: row[key] for key in metadata}, metadata)
            rows = events.read("repo", tmp)
            self.assertEqual(len(rows), 1)
            self.assertEqual({key: rows[0][key] for key in metadata}, metadata)
            self.assertEqual(rows[0]["tokens"], {"total": 12})
            self.assertEqual(rows[0]["tokens_source"], "stamp")

    def test_missing_event_metadata_remains_a_documented_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            events.job_event("repo", "job-1", "start", state_dir=tmp, agent="Codex")
            row = events.read("repo", tmp)[0]
            self.assertNotIn("group_id", row)
            self.assertNotIn("round", row)
            self.assertNotIn("model", row)
            self.assertNotIn("model_version", row)

    def test_lifecycle_folds_by_stable_job_id_and_keeps_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            events.job_event("repo", "job-1", "start", state_dir=tmp, agent="Codex")
            events.job_event("repo", "job-1", "finish", state_dir=tmp, model="gpt-test",
                             tokens_source="stamp", tokens={"total": 12})
            events.job_event("repo", "job-2", "incomplete", state_dir=tmp, agent="Grok Bot", tokens={"total": 99})
            rows = events.read("repo", tmp)
            self.assertEqual({row["job_id"] for row in rows}, {"job-1", "job-2"})
            self.assertEqual(next(row for row in rows if row["job_id"] == "job-1")["event"], "finish")
            self.assertEqual(next(row for row in rows if row["job_id"] == "job-2")["event"], "incomplete")
            self.assertEqual(next(row for row in rows if row["job_id"] == "job-1")["model"], "gpt-test")

    def test_trail_event_maps_steps_and_keeps_group_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracker = root / "list.json"
            tracker.write_text(json.dumps({"tracking": {"plan": {"groups": [
                {"id": "A", "finding_ids": ["ISSUE-01"]}]}}}))
            events.trail_event(tracker, "failed_first", group_id="A", finding_ids=["ISSUE-01", "ISSUE-01"],
                               result="test_guard fails: expected refusal", job_id="job-1", commit="abc1234",
                               state_dir=root / "state")
            events.trail_event(tracker, "root_cause", group_id="A", finding_ids=["ISSUE-01"],
                               result="manage_findings.py:_lane_groups", job_id="job-1", state_dir=root / "state")
            rows = [json.loads(line) for line in events.events_path(tracker, root / "state").read_text().splitlines()]
            self.assertEqual([row["event"] for row in rows], ["issue_failed_first", "root_cause_identified"])
            self.assertEqual([row["step"] for row in rows], ["failed_first", "root_cause"])
            self.assertEqual(rows[0]["group_id"], "A")
            self.assertEqual(rows[0]["finding_ids"], ["ISSUE-01"])
            self.assertEqual(rows[0]["commit"], "abc1234")
            with self.assertRaisesRegex(ValueError, "unsupported trail step"):
                events.trail_event(tracker, "guessed", group_id="A", finding_ids=[], result="x",
                                   state_dir=root / "state")

    def test_failed_start_stays_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            events.job_event("repo", "job", "failed_start", state_dir=tmp, agent="Codex")
            self.assertEqual(events.read("repo", tmp)[0]["event"], "failed_start")

    def test_events_are_scoped_to_resolved_list_identity_for_jobs_and_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "list.json"
            same = root / "other-worktree" / "list.json"
            second = root / "one" / "another-list.json"
            for path in (first, same, second):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            alias = root / "alias.json"
            alias.symlink_to(first)
            events.job_event(first, "j1", "start", state_dir=root / "state")
            events.stage_event(alias, "review", "start", state_dir=root / "state")
            events.job_event(first, "j1", "finish", state_dir=root / "state")
            events.job_event(second, "j2", "start", state_dir=root / "state")
            events.job_event(same, "j3", "start", state_dir=root / "state")
            self.assertEqual(events.events_path(first, root / "state"), events.events_path(alias, root / "state"))
            self.assertNotEqual(events.events_path(first, root / "state"), events.events_path(second, root / "state"))
            self.assertEqual([row["event"] for row in events.read(first, root / "state")], ["stage_start", "finish"])
            self.assertEqual([row["job_id"] for row in events.read(same, root / "state")], ["j3"])


if __name__ == "__main__":
    unittest.main()
