import json
import copy
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
import check_introspect
import manage_findings
import workflow_definition
import check_install_sessions

try:
    import fcntl
except ImportError:
    fcntl = None

SCRIPT = Path(__file__).with_name("manage_findings.py")
EXAMPLE = Path(__file__).parents[1] / "examples/findings.json"
RENDERER = Path(__file__).with_name("render.py")


class ManageFindingsTests(unittest.TestCase):
    def evidence_record(self, **overrides):
        return {'checked':'Checked 7 scenarios', 'expected':'Version 0.1.42 works', 'observed':'All 7 passed',
                'verdict':'Passed all cases', 'check_id':'check-1', 'build':'0.1.42', 'session':'run 7',
                'timestamp':'2026-11-16T10:00:00Z', 'reference':'receipt', **overrides}

    def test_archive_history_keeps_ambiguous_rows_and_check_id_lookup(self):
        tracking = {
            "verification_history": [
                {"id": "run-old", "cycle": 1, "checks": {"ISSUE-01": {"check_id": "check-old"}}},
                {"id": "run-legacy", "checks": {"ISSUE-01": {"check_id": "check-legacy"}}},
                {"id": "run-current", "cycle": 2, "checks": {"ISSUE-01": {"check_id": "check-current"}}},
            ],
            "introspect_history": [{"cycle": 1, "round": 1, "result": "old"}, {"result": "legacy"}],
        }
        moved = manage_findings.archive_cycle_history(tracking, 1)
        archive = self.root / "archive.json"
        archive.write_text(json.dumps(moved))
        tracking["archive"] = {"path": str(archive)}
        self.assertEqual([row["id"] for row in tracking["verification_history"]], ["run-legacy", "run-current"])
        self.assertEqual([row.get("cycle") for row in tracking["introspect_history"]], [None])
        self.assertEqual(manage_findings.verification_check({"tracking": tracking}, "ISSUE-01", "check-old")["check_id"], "check-old")

    def test_fixed_inline_introspect_requires_a_named_proof(self):
        steps = ["performed work"]
        sections = {name: {"none": "No changes."} for name in check_introspect.SECTION_ORDER}
        sections["delete"] = {"items": [{"item": steps[0], "evidence": "Diff saved.", "outcome": "fixed inline"}]}
        with self.assertRaisesRegex(ValueError, "named command or test"):
            check_introspect.validate_introspect(sections, steps, [], {})
        sections["delete"]["items"][0]["proof"] = "python3 -m unittest test_fix.py"
        self.assertEqual(check_introspect.validate_introspect(sections, steps, [], {})["steps"], steps)

    def test_archive_history_batch_preserves_legacy_and_resolves_archived_check(self):
        document = copy.deepcopy(self.document)
        document["tracking"] = {}
        old = {"id": "old-run", "cycle": 1, "checks": {"ISSUE-01": {"state": "fixed", "check_id": "old-check"}}}
        legacy = {"id": "legacy-run", "checks": {"ISSUE-01": {"state": "fixed", "check_id": "legacy-check"}}}
        current = {"id": "new-run", "cycle": 2, "checks": {"ISSUE-01": {"state": "fixed", "check_id": "new-check"}}}
        document["tracking"].update(cycle=3, verification_history=[old, legacy, current],
                                     introspect_history=[{"cycle": 1, "round": 1, "result": "old"}, {"result": "legacy"}])
        self.data.write_text(json.dumps(document))
        operations = self.root / "archive-history.json"
        operations.write_text(json.dumps([{"action": "archive-history", "through_cycle": 1}]))
        result = self.cli("batch", self.data, operations, "--render", self.root / "page.html")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual([row["id"] for row in saved["tracking"]["verification_history"]], ["legacy-run", "new-run"])
        self.assertEqual(len(saved["tracking"]["introspect_history"]), 1)
        self.assertEqual(manage_findings.verification_check(saved, "ISSUE-01", "old-check")["check_id"], "old-check")
        rows = json.loads((self.root / "archive.json").read_text())
        self.assertEqual({row["record_type"] for row in rows}, {"verification_history", "introspect_history"})

    def test_cleanup_outcome_is_separate_from_verification_verdict(self):
        document = copy.deepcopy(self.document)
        document["tracking"] = {}
        check = {"state": "inconclusive", "check_id": "c1", "evidence": "could not observe",
                 "finished_at": "2026-11-21T00:00:00Z"}
        document["tracking"]["verification_run"] = {"id": "run-1", "status": "active", "build": "1.0",
            "revision": "abc", "installed_at": "2026-11-21T00:00:00Z", "install_evidence": "receipt",
            "checks": {"ISSUE-01": check}}
        self.data.write_text(json.dumps(document))
        operations = self.root / "cleanup.json"
        operations.write_text(json.dumps([{"action": "record-cleanup", "run_id": "run-1", "outcome": "failed",
                                           "reason": "temporary files remain"}]))
        result = self.cli("batch", self.data, operations, "--render", self.root / "page.html")
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(self.data.read_text())["tracking"]["verification_run"]
        self.assertEqual(run["checks"]["ISSUE-01"], check)
        self.assertEqual(run["cleanup"]["outcome"], "failed")

    def test_record_evidence_rejects_snake_case(self):
        with self.assertRaisesRegex(ValueError, 'reference or details'):
            manage_findings.validate_record(self.evidence_record(observed='test_foo_bar passed'))

    def test_record_evidence_rejects_dotted_code_name(self):
        with self.assertRaisesRegex(ValueError, 'reference or details'):
            manage_findings.validate_record(self.evidence_record(checked='module.function worked'))

    def test_record_evidence_rejects_paths(self):
        with self.assertRaisesRegex(ValueError, 'reference or details'):
            manage_findings.validate_record(self.evidence_record(expected='See ~/reports/result'))

    def test_record_evidence_rejects_filenames(self):
        with self.assertRaisesRegex(ValueError, 'reference or details'):
            manage_findings.validate_record(self.evidence_record(observed='See verify.log'))

    def test_record_evidence_rejects_hashes(self):
        with self.assertRaisesRegex(ValueError, 'reference or details'):
            manage_findings.validate_record(self.evidence_record(verdict='Passed at abcdef1'))

    def test_record_evidence_rejects_technical_labels(self):
        for value in ('Named tests: seven passed', 'Log: run output'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reference or details'):
                manage_findings.validate_record(self.evidence_record(observed=value))

    def test_record_evidence_accepts_counts_versions_and_details(self):
        record = self.evidence_record(details='test_foo_bar in verify.log')
        manage_findings.validate_record(record)
        self.assertEqual(record['details'], 'test_foo_bar in verify.log')

    def test_add_rejects_flags_in_finding_text(self):
        for field in ('title', 'summary', 'impact', 'done_when'):
            with self.subTest(field=field):
                entry = {'id':'ISSUE-99','title':'Plain title','summary':'Plain summary',
                         'impact':'Plain impact','done_when':'Plain result','source':'User reported',
                         'status':'untested','observed':'User saw a problem','recommendation':'Review behavior'}
                entry[field] = '--phase review'
                path = self.root / f'add-{field}.json'; path.write_text(json.dumps(entry))
                result = self.cli('add', self.data, path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('move them to reference or details', result.stderr)

    def test_update_rejects_field_names_in_finding_text(self):
        for field in ('title', 'summary', 'impact', 'done_when'):
            with self.subTest(field=field):
                path = self.root / f'update-{field}.json'; path.write_text(json.dumps({field:'codex.review_status'}))
                result = self.cli('update', self.data, 'ISSUE-01', path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('move them to reference or details', result.stderr)

    def test_finish_check_rejects_technical_parsed_fields(self):
        with self.assertRaisesRegex(ValueError, 'reference or details'):
            manage_findings.validate_verification_reason(
                'Checked: test_foo_bar; Expected: fixed; Observed: passed')

    def test_finish_check_keeps_optional_details_separate(self):
        operations = self.root / 'finish.json'
        page = self.root / 'finish.html'
        def batch(*actions):
            operations.write_text(json.dumps(actions))
            result = self.cli('batch', self.data, operations, '--render', page)
            self.assertEqual(result.returncode, 0, result.stderr)
        batch({'action':'start-verification','run':{'id':'run1','build':'1.0.0','revision':'abc',
              'installed_at':'2026-11-16T10:00:00Z','install_evidence':'receipt'}})
        batch({'action':'start-check','run_id':'run1','id':'ISSUE-01','observation':'Visible',
               'observed_at':'2026-11-16T10:01:00Z'})
        operations.write_text(json.dumps([{'action':'finish-check','run_id':'run1','id':'ISSUE-01','outcome':'inconclusive',
             'evidence':'Checked: visible behavior; Expected: succeeds; Observed: command failed',
             'details':'test_foo_bar in verify.log'}]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        check = json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(check['details'], 'test_foo_bar in verify.log')

    def test_legacy_evidence_moves_identifiers_to_details(self):
        row = manage_findings.RENDERER.version_evidence({'id':'ISSUE-01','verification_evidence':[
            {'checked':'test_review_job passed','expected':'fixed','observed':'See verify.log',
             'verdict':'Passed at abcdef1','build':'0.1.42','timestamp':'2026-11-16T10:00:00Z'}]}, [])[0]
        self.assertTrue(all(row[field] == 'Recorded in technical terms; see Details'
                             for field in ('checked','observed','verdict')))
        self.assertIn('test_review_job', row['details'])
        self.assertIn('verify.log', row['details'])
        self.assertNotIn('test_review_job', json.dumps({k: row[k] for k in ('checked','expected','observed','verdict')}))

    def test_current_round_finding_assignments_are_unique(self):
        ops, page = self.root / 'overlap.json', self.root / 'overlap.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        first = {'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}
        second = {'id':'B','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}
        before = self.data.read_bytes()
        result = apply({'action':'set-plan','plan':{'round':1,'groups':[first, second]}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('multiple current-round groups', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        second.update(status='deferred', phase='deferred', parked_to=2)
        self.assertEqual(apply({'action':'set-plan','plan':{'round':1,'groups':[first, second]}}).returncode, 0)
        self.assertEqual(apply({'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Starting'}).returncode, 0)
        before = self.data.read_bytes()
        result = apply({'action':'update-plan-group','id':'B','patch':{'status':'pending'}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('multiple current-round groups', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        historical = json.loads(self.data.read_text())
        historical['tracking']['plan']['groups'][1].update(status='pending', phase='fix')
        self.data.write_text(json.dumps(historical))
        before = self.data.read_bytes()
        result = apply({'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Starting'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('multiple current-round groups', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_dependencies_block_early_work_and_cycles_but_allow_ready_work(self):
        ops, page = self.root / 'dependencies.json', self.root / 'page.html'
        def apply(*operations):
            ops.write_text(json.dumps(operations))
            return self.cli('batch', self.data, ops, '--render', page)
        groups = [
            {'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'running'},
            {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'},
        ]
        before = self.data.read_bytes()
        result = apply({'action':'set-plan','plan':{'round':1,'groups':groups}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must wait', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        groups[1]['status'] = 'pending'
        self.assertEqual(apply({'action':'set-plan','plan':{'round':1,'groups':groups}}).returncode, 0)
        before = self.data.read_bytes()
        self.assertNotEqual(apply({'action':'update-plan-group','id':'B','patch':{'status':'running'}}).returncode, 0)
        self.assertNotEqual(apply({'action':'set-activity','stage':'plan_fix','ids':['ISSUE-02'],'now':'Starting B'}).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        forged = apply({'action':'update-plan-group','id':'A','patch':{'status':'verified'}})
        self.assertNotEqual(forged.returncode, 0)
        self.assertIn('requires verified findings', forged.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        proof = {'status':'verified_fixed','verification':'Checked on installed build','verification_refs':['receipt']}
        self.assertEqual(apply({'action':'update','id':'ISSUE-01','patch':proof}).returncode, 0)
        self.assertEqual(apply({'action':'update-plan-group','id':'A','patch':{'status':'verified'}}).returncode, 0)
        self.assertEqual(apply({'action':'update-plan-group','id':'B','patch':{'status':'running'}}).returncode, 0)
        cyclic = [{'id':'A','finding_ids':['ISSUE-01'],'depends_on':['B'],'status':'pending'},
                  {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'pending'}]
        result = apply({'action':'set-plan','plan':{'round':1,'groups':cyclic}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('dependency cycle', result.stderr)

    def test_activity_rejects_unplanned_or_parked_findings(self):
        ops, page = self.root / 'eligible.json', self.root / 'eligible.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':[],'status':'deferred','phase':'deferred','parked_to':2}]}
        entry = {'id':'ISSUE-03','title':'Unplanned issue','source':'Agent reported','status':'untested',
                 'observed':'An issue exists.','recommendation':'Investigate.'}
        self.assertEqual(apply({'action':'add','entry':entry}, {'action':'set-plan','plan':plan}).returncode, 0)
        before = self.data.read_bytes()
        for identifier in ('ISSUE-02', 'ISSUE-03'):
            result = apply({'action':'set-activity','stage':'plan_fix','ids':[identifier],'now':'Starting'})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('eligible current-round plan group', result.stderr)
            self.assertEqual(self.data.read_bytes(), before)

    def test_lane_state_update_refreshes_workflow_clock(self):
        ops, page = self.root / 'clock.json', self.root / 'clock.html'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}
        ops.write_text(json.dumps([{'action':'set-plan','plan':plan}]))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', page).returncode, 0)
        document = json.loads(self.data.read_text())
        document['tracking']['workflow']['updated_at'] = '2020-01-01T00:00:00Z'
        self.data.write_text(json.dumps(document))
        ops.write_text(json.dumps([{'action':'update-plan-group','id':'A','patch':{'status':'running'}}]))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', page).returncode, 0)
        workflow = json.loads(self.data.read_text())['tracking']['workflow']
        self.assertGreater(workflow['updated_at'], '2020-01-01T00:00:00Z')
        self.assertIn('Lane A updated', workflow['now'])
        document = json.loads(self.data.read_text())
        document['tracking']['workflow']['updated_at'] = '2020-01-01T00:00:00Z'
        self.data.write_text(json.dumps(document))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', page).returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())['tracking']['workflow']['updated_at'], '2020-01-01T00:00:00Z')

    def test_plan_cannot_claim_verified_prerequisites_or_dependents(self):
        ops, page = self.root / 'reopen.json', self.root / 'reopen.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        proof = {'status':'verified_fixed','verification':'Checked on installed build','verification_refs':['receipt']}
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'verified'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'},
                {'id':'C','finding_ids':['ISSUE-03'],'depends_on':['A'],'status':'verified'}]}
        before = self.data.read_bytes()
        result = apply({'action':'update','id':'ISSUE-01','patch':proof}, {'action':'set-plan','plan':plan})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot claim completed job state', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_set_plan_cannot_claim_a_verified_dependency(self):
        ops, page = self.root / 'recheck-plan.json', self.root / 'recheck-plan.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        proof = {'status':'verified_fixed','verification':'Checked on installed build','verification_refs':['receipt']}
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'verified'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'}]}
        before = self.data.read_bytes()
        result = apply({'action':'update','id':'ISSUE-01','patch':proof}, {'action':'set-plan','plan':plan})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot claim completed job state', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_standalone_update_invalidates_running_dependent(self):
        document = json.loads(self.data.read_text())
        document['findings'][0].update(status='verified_fixed', verification='Checked', verification_refs=['receipt'])
        document['tracking'] = {'plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'verified'},
                                  {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'}]}}
        self.data.write_text(json.dumps(document))
        patch = self.root / 'standalone-patch.json'
        patch.write_text(json.dumps({'status':'failing'}))
        result = self.cli('update', self.data, 'ISSUE-01', patch)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.data.read_text())
        self.assertEqual(document['findings'][0]['status'], 'failing')
        self.assertEqual(document['tracking']['plan']['groups'][1]['status'], 'blocked')

    def test_standalone_delete_invalidates_running_dependent(self):
        document = json.loads(self.data.read_text())
        document['findings'][0].update(status='verified_fixed', verification='Checked', verification_refs=['receipt'])
        document['tracking'] = {'plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'verified'},
                                  {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'}]}}
        self.data.write_text(json.dumps(document))
        result = self.cli('delete', self.data, 'ISSUE-01')
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.data.read_text())
        self.assertEqual([item['id'] for item in document['findings']], ['ISSUE-02'])
        self.assertEqual(document['tracking']['plan']['groups'][1]['status'], 'blocked')

    def test_batch_update_expected_status_rejects_mismatch_without_writing(self):
        ops, page = self.root / 'expected.json', self.root / 'expected.html'
        before = self.data.read_bytes()
        ops.write_text(json.dumps([{'action':'update','id':'ISSUE-01','expected_status':'in_progress',
                                   'patch':{'status':'failing'}}]))
        result = self.cli('batch', self.data, ops, '--render', page)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('status changed; refusing update', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_plan_precondition_checks_original_plan_across_batch(self):
        ops, page = self.root / 'fingerprint.json', self.root / 'fingerprint.html'
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':[],'status':'pending'}]}
        ops.write_text(json.dumps([{'action':'set-plan','plan':plan}]))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', page).returncode, 0)
        current = json.loads(self.data.read_text())['tracking']['plan']
        digest = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        ops.write_text(json.dumps([{'action':'update-plan-group','id':group,'expected_plan_sha256':digest,'patch':{'status':'running'}} for group in ('A','B')]))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', page).returncode, 0)
        before = self.data.read_bytes()
        result = self.cli('batch', self.data, ops, '--render', page)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('plan changed; refusing group update', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        ops.write_text(json.dumps([{'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],
                                    'now':'Starting','expected_plan_sha256':digest}]))
        result = self.cli('batch', self.data, ops, '--render', page)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('plan changed; refusing activity update', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_group_precondition_allows_other_lane_updates_but_not_selected_lane_change(self):
        ops, page = self.root / 'group-precondition.json', self.root / 'group-precondition.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':[],'status':'pending'}]}
        self.assertEqual(apply({'action':'set-plan','plan':plan}).returncode, 0)
        saved = json.loads(self.data.read_text())['tracking']['plan']
        precondition = {'expected_group':saved['groups'][0], 'expected_cycle':saved['cycle'], 'expected_round':saved['round']}
        self.assertEqual(apply({'action':'update-plan-group','id':'B','patch':{'status':'running'}}).returncode, 0)
        self.assertEqual(apply({'action':'update-plan-group','id':'A','patch':{'status':'running'},**precondition}).returncode, 0)
        before = self.data.read_bytes()
        result = apply({'action':'update-plan-group','id':'A','patch':{'status':'review'},**precondition})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('plan group changed', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    @unittest.skipIf(fcntl is None, 'requires Unix flock')
    def test_competing_writers_serialize_without_blocking_readers(self):
        lock_dir = Path('/tmp') / f'bcl-tracker-locks-{os.getuid()}'
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock_name = hashlib.sha256(str(self.data.resolve()).encode()).hexdigest() + '.lock'
        original = json.loads(self.data.read_text())['findings'][0]['status']
        processes = []
        with (lock_dir / lock_name).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for index, status in enumerate(('failing', 'in_progress')):
                ops = self.root / f'compete-{index}.json'
                ops.write_text(json.dumps([{'action':'update','id':'ISSUE-01','expected_status':original,
                                            'patch':{'status':status}}]))
                processes.append(subprocess.Popen([sys.executable, str(SCRIPT), 'batch', str(self.data), str(ops),
                                                   '--render', str(self.root / f'compete-{index}.html')],
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            read = self.cli('list', self.data)
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertTrue(all(process.poll() is None for process in processes))
            fcntl.flock(lock, fcntl.LOCK_UN)
        outputs = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 2], outputs)
        self.assertIn('status changed; refusing update', ''.join(stderr for _, stderr in outputs))

    def test_verification_rejects_unknown_build_or_revision_without_writing(self):
        ops = self.root / 'run.json'
        run = {'id':'run-1','build':'ACU 1.2.3','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        before = self.data.read_bytes()
        for field in ('build', 'revision'):
            candidate = {**run, field:'unknown'}
            ops.write_text(json.dumps([{'action':'start-verification','run':candidate}]))
            result = self.cli('batch', self.data, ops, '--render', self.root / 'page.html')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('known build and revision', result.stderr)
            self.assertEqual(self.data.read_bytes(), before)

    def test_plan_validation_replacement_and_group_patch(self):
        ops, page = self.root / 'plan.json', self.root / 'plan.html'
        def batch(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        invalid = [
            {'round':1,'groups':[{'id':'A','finding_ids':['MISSING'],'depends_on':[],'status':'pending'}]},
            {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':['B'],'status':'pending'}]},
            {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'unknown'}]},
        ]
        for plan in invalid:
            before = self.data.read_bytes()
            result = batch({'action':'set-plan','plan':plan})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.data.read_bytes(), before)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','label':'Settings behavior','verify_summary':'Check the selected panel'}]}
        self.assertEqual(batch({'action':'set-plan','plan':plan}).returncode, 0)
        planned = json.loads(self.data.read_text())['tracking']
        self.assertEqual(planned['workflow']['stage'], 'plan_fix')
        self.assertTrue(planned['workflow']['updated_at'].endswith('Z'))
        self.assertIn('Plan recorded', planned['workflow']['now'])
        self.assertIn('plan_fix', planned['stages_reached'])
        self.assertEqual(batch({'action':'set-plan','plan':plan}, {'action':'update-plan-group','id':'A','patch':{'status':'done','label':'Updated label','verify_summary':'Runtime check passed'}}).returncode, 0)
        group = json.loads(self.data.read_text())['tracking']['plan']['groups'][0]
        self.assertEqual((group['status'], group['label'], group['verify_summary']), ('done','Updated label','Runtime check passed'))

    def test_plan_check_refuses_open_lanes_that_change_the_same_files(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import manage_findings
        lane = lambda gid, files, status="planned": {"id": gid, "finding_ids": [gid + "-1"], "status": status, "files": files}
        with self.assertRaisesRegex(ValueError, "lanes A and B both change scripts/render.py"):
            manage_findings.validate_plan_groups({"round": 1, "groups": [lane("A", ["scripts/render.py"]), lane("B", ["scripts/render.py", "x.md"])]})
        with self.assertRaisesRegex(ValueError, "lanes A and C both change"):
            manage_findings.validate_plan_groups({"round": 1, "groups": [lane("A", ["scripts/*.py"]), lane("C", ["scripts/bcl_tool.py"])]})
        manage_findings.validate_plan_groups({"round": 1, "groups": [lane("A", ["scripts/render.py"]), lane("B", ["bin/bcl"])]})
        manage_findings.validate_plan_groups({"round": 1, "groups": [lane("A", ["bin/bcl"]), lane("B", ["bin/bcl"], "deferred")]})
        manage_findings.validate_plan_groups({"round": 1, "groups": [lane("A", ["bin/bcl"]), {"id": "N", "finding_ids": ["N-1"], "status": "planned"}]})

    def test_plan_check_caps_group_size_and_allows_max_effort(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import manage_findings
        manage_findings.validate_plan_groups({
            "round": 2,
            "groups": [{"id": "A", "finding_ids": ["I1", "I2", "I3", "I4"], "status": "pending",
                        "codex": {"effort": "medium"}}],
        })
        with self.assertRaises(ValueError) as err:
            manage_findings.validate_plan_groups({
                "round": 2,
                "groups": [{"id": "BIG", "finding_ids": ["A", "B", "C", "D", "E"], "status": "pending"}],
            })
        self.assertIn("cap a parallel fix group", str(err.exception))
        manage_findings.validate_plan_groups({
            "round": 2,
            "groups": [{"id": "HARD", "finding_ids": ["A", "B", "C", "D"], "status": "pending",
                        "codex": {"model": "gpt-6-luna", "effort": "max"}}],
        })
        with self.assertRaisesRegex(ValueError, "cap a parallel fix group"):
            manage_findings.validate_plan_groups({
                "round": 2,
                "groups": [{"id": "TOO_BIG", "finding_ids": ["A", "B", "C", "D", "E"],
                            "status": "pending", "codex": {"effort": "max"}}],
            })
        manage_findings.validate_plan_groups({
            "round": 2,
            "groups": [{"id": "OLD", "finding_ids": ["A", "B", "C", "D", "E"], "status": "deferred",
                        "phase": "fix", "codex": {"effort": "max"}}],
        })
        manage_findings.validate_plan_groups({
            "round": 2,
            "groups": [{"id": "PARKED", "finding_ids": ["A", "B", "C", "D", "E"], "status": "parked",
                        "phase": "fix", "codex": {"effort": "max"}}],
        })
        for phase in ("done", "fix"):
            with self.subTest(status="running", phase=phase):
                manage_findings.validate_plan_groups({
                    "round": 2,
                    "groups": [{"id": "ACTIVE", "finding_ids": ["A"], "status": "running",
                                "phase": phase, "codex": {"effort": "max"}}],
                })
        with self.assertRaisesRegex(ValueError, "cap a parallel fix group"):
            manage_findings.validate_plan_groups({
                "round": 2,
                "groups": [{"id": "ACTIVE", "finding_ids": ["A", "B", "C", "D", "E"],
                            "status": "running", "phase": "done", "codex": {"effort": "medium"}}],
            })
        manage_findings.validate_plan_groups({
            "round": 2,
            "groups": [{"id": "PHASE", "finding_ids": ["A", "B", "C", "D", "E"],
                        "phase": "deferred", "codex": {"effort": "max"}}],
        })

    def test_set_plan_accepts_max_effort_and_rejects_oversized_groups(self):
        hard_group = {"id": "HARD", "finding_ids": ["ISSUE-01"],
                      "depends_on": [], "status": "pending",
                      "codex": {"model": "gpt-6-luna", "effort": "max"}}
        result = self._replay_batch(json.loads(self.data.read_bytes()), {
            "action": "set-plan", "plan": {"round": 1, "groups": [hard_group]},
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        for group, message in (
            ({"id": "BIG", "finding_ids": ["ISSUE-01", "ISSUE-02", "ISSUE-03", "ISSUE-04", "ISSUE-05"],
              "depends_on": [], "status": "pending", "codex": {"effort": "medium"}}, "split it"),
        ):
            with self.subTest(group=group):
                self.data.write_text(json.dumps(json.loads(self.data.read_bytes())), encoding="utf-8")
                before = self.data.read_bytes()
                result = self._replay_batch(json.loads(before), {
                    "action": "set-plan", "plan": {"round": 1, "groups": [group]},
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.data.read_bytes(), before)

    def test_set_plan_uses_status_before_phase_and_preserves_tracker_on_refusal(self):
        for group, accepted in (
            ({"id": "ACTIVE", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "running",
              "phase": "done", "codex": {"effort": "max"}}, True),
            ({"id": "DEFERRED", "finding_ids": ["ISSUE-01"],
              "depends_on": [], "status": "deferred", "phase": "fix", "codex": {"effort": "max"}}, True),
        ):
            with self.subTest(group=group):
                before = self.data.read_bytes()
                result = self._replay_batch(json.loads(before), {
                    "action": "set-plan", "plan": {"round": 1, "groups": [group]},
                })
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("cap a parallel fix group", result.stderr)
                    self.assertEqual(self.data.read_bytes(), before)

    def test_blocked_outcome_requires_external_blocker(self):
        ops, page = self.root / 'ops.json', self.root / 'page.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        run = {'id':'install-1','build':'ACU','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(apply({'action':'start-verification','run':run}).returncode, 0)
        self.assertEqual(apply({'action':'start-check','id':'ISSUE-01','run_id':'install-1',
                                'observation':'current visible state','observed_at':'2026-11-06T00:01:00Z'}).returncode, 0)
        finish = {'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'blocked',
                  'evidence':'Checked: current behavior; Expected: available; Observed: waiting for vendor'}
        rejected = apply(finish)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('named external blocker field', rejected.stderr)
        accepted = apply({**finish, 'blocker':'Vendor service access'})
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        check = json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(check['blocker'], 'Vendor service access')

    def test_recheck_preserves_previous_result_and_other_checks(self):
        ops, page = self.root / 'ops.json', self.root / 'page.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        run = {'id':'install-1','build':'ACU','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(apply({'action':'start-verification','run':run}).returncode, 0)
        start = {'action':'start-check','id':'ISSUE-01','run_id':'install-1','observation':'initial visible state','observed_at':'2026-11-06T00:01:00Z'}
        self.assertEqual(apply(start).returncode, 0)
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'inconclusive','evidence':'Checked: the issue behavior; Expected: reproduce consistently; Observed: first result'}).returncode, 0)
        before = json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-02']
        self.assertNotEqual(apply(start).returncode, 0)
        self.assertEqual(apply({**start, 'recheck':True, 'observation':'new visible state','observed_at':'2026-11-06T00:02:00Z'}).returncode, 0)
        current = json.loads(self.data.read_text())['tracking']['verification_run']['checks']
        self.assertEqual(current['ISSUE-01']['state'], 'checking')
        self.assertEqual(current['ISSUE-01']['previous_checks'][0]['evidence'], 'Checked: the issue behavior; Expected: reproduce consistently; Observed: first result')
        self.assertEqual(current['ISSUE-02'], before)
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'not_fixed','evidence':'Checked: the issue behavior; Expected: fixed; Observed: second result'}).returncode, 0)
        current = json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(current['evidence'], 'Checked: the issue behavior; Expected: fixed; Observed: second result')
        self.assertEqual(current['previous_checks'][0]['evidence'], 'Checked: the issue behavior; Expected: reproduce consistently; Observed: first result')
        start2 = {'action':'start-check','id':'ISSUE-02','run_id':'install-1','observation':'second issue visible state','observed_at':'2026-11-06T00:03:00Z'}
        self.assertEqual(apply(start2).returncode, 0)
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-02','run_id':'install-1','outcome':'inconclusive','evidence':'Checked: the issue behavior; Expected: confirmed; Observed: other'}).returncode, 0)
        self.assertEqual(apply({'action':'complete-verification','run_id':'install-1'}).returncode, 0)
        self.assertEqual(apply({**start, 'recheck':True, 'observation':'third visible state','observed_at':'2026-11-06T00:04:00Z'}).returncode, 0)
        run = json.loads(self.data.read_text())['tracking']['verification_run']
        self.assertEqual(run['status'], 'active')
        self.assertEqual(run['checks']['ISSUE-02']['evidence'], 'Checked: the issue behavior; Expected: confirmed; Observed: other')

    def test_each_phase_updates_the_timeline_and_can_be_cleared(self):
        ops, page = self.root / 'phase.json', self.root / 'report.html'
        for stage in ('collect', 'present', 'plan_fix', 'None'):
            pre = []
            if stage == 'plan_fix':
                pre.extend([{'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}},
                            {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Starting the checked stage'}])
            ops.write_text(json.dumps(pre + [{'action':'set-stage','stage':None if stage == 'None' else stage}]))
            args = ['batch', self.data, ops, '--render', page]
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(self.data.read_text())
            expected = None if stage == 'None' else stage
            self.assertEqual(doc['tracking']['workflow']['stage'], expected)
            html = page.read_text()
            current_steps = 1 if stage in ('collect', 'present') else 0
            with self.subTest(stage=stage):
                self.assertEqual(html.count('aria-current="step"'), current_steps)
            self.assertNotIn('class="workflow-now"', html)

    def test_workflow_stage_guard_and_clear(self):
        ops, page = self.root / 'stage.json', self.root / 'stage.html'
        def run(*operations):
            ops.write_text(json.dumps(list(operations)))
            return self.cli('batch', self.data, ops, '--render', page)
        before = self.data.read_bytes()
        result = run({'action':'set-stage','stage':'plan_fix'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('in_progress', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        result = run({'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Starting a fix cycle'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires a plan', result.stderr)
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer'}}]}}
        result = run(plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Starting a fix cycle'})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data['findings'][0]['status'], 'in_progress')
        self.assertEqual(data['tracking']['workflow']['stage'], 'plan_fix')
        html = page.read_text()
        self.assertIn('class="fresh">Updated', html)
        self.assertNotIn('class="workflow-now"', html)
        self.assertIn('Plan &amp; fix', html)
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer'}}]}}
        self.assertEqual(run(plan).returncode, 0)
        self.assertEqual(run({'action':'set-activity','stage':'review','ids':['ISSUE-01'],'now':'Review'}).returncode, 0)
        install = run({'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'})
        self.assertNotEqual(install.returncode, 0)
        self.assertIn("install requires a clear bcl release-check", install.stderr)
        before = self.data.read_bytes()
        self.assertNotEqual(run({'action':'set-stage','stage':'made-up'}).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        self.assertEqual(run({'action':'set-stage','stage':None}).returncode, 0)
        self.assertNotIn('aria-current="step"', page.read_text())

    def test_fix_stages_reject_empty_activity_or_unstarted_tracker(self):
        for stage in ('plan_fix', 'review', 'install', 'verify'):
            for operation in (
                {'action':'set-stage','stage':stage},
                {'action':'set-activity','stage':stage,'ids':[],'now':'Starting work'},
            ):
                before = self.data.read_bytes()
                operations = self.root / 'guard.json'
                operations.write_text(json.dumps([operation]))
                result = self.cli('batch', self.data, operations, '--render', self.root / 'guard.html')
                self.assertNotEqual(result.returncode, 0, (stage, operation))
                self.assertTrue('in_progress' in result.stderr or 'recorded plan' in result.stderr or 'requires install' in result.stderr or 'requires a plan' in result.stderr or 'requires review' in result.stderr or 'requires plan_fix' in result.stderr, result.stderr)
                self.assertEqual(self.data.read_bytes(), before)
        operations = self.root / 'guard.json'
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}}
        operations.write_text(json.dumps([plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Fix'}]))
        result = self.cli('batch', self.data, operations, '--render', self.root / 'guard.html')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())['findings'][0]['status'], 'in_progress')
        before = self.data.read_bytes()
        operations.write_text(json.dumps([{'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'}]))
        result = self.cli('batch', self.data, operations, '--render', self.root / 'guard.html')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('install requires review to be reached first', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        operations.write_text(json.dumps([{'action':'set-activity','stage':'install','ids':[],'now':'No selected finding'}]))
        operations.write_text(json.dumps([{'action':'set-activity','stage':'review','ids':['ISSUE-01'],'now':'Review'}, {'action':'set-activity','stage':'install','ids':[],'now':'No selected finding'}]))
        empty = self.cli('batch', self.data, operations, '--render', self.root / 'empty.html')
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn('nonempty ID list', empty.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_finding_write_preserves_last_workflow_activity(self):
        document = json.loads(self.data.read_text())
        document['tracking'] = {'workflow': {'stage':'collect','updated_at':'2020-01-01T00:00:00Z','now':'Last activity'}}
        self.data.write_text(json.dumps(document))
        entry = self.root / 'entry.json'
        entry.write_text(json.dumps({'id':'ISSUE-03','title':'A saved finding','source':'User reported',
            'status':'untested','observed':'The issue is visible.','recommendation':'Review the issue.'}))
        result = self.cli('add', self.data, entry)
        self.assertEqual(result.returncode, 0, result.stderr)
        written = json.loads(self.data.read_text())
        self.assertEqual(written['tracking']['workflow']['stage'], 'collect')
        self.assertEqual(written['tracking']['workflow']['updated_at'], '2020-01-01T00:00:00Z')
        self.assertEqual(written['tracking']['cycle'], 1)

    def test_audit_flags_stale_and_duplicate_without_writing(self):
        self.document.setdefault('tracking', {})['verification_run'] = {'build':'ACU 0.2.120'}
        self.document['findings'][0]['details'] = {'Latest test':'ACU 0.2.101 is installed', 'Copy':'Same result', 'Repeat':'Same result'}
        self.data.write_text(json.dumps(self.document))
        before = self.data.read_bytes()
        result = self.cli('audit', self.data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({x['kind'] for x in json.loads(result.stdout)}, {'possibly_stale', 'duplicate_detail'})
        self.assertEqual(self.data.read_bytes(), before)

    def test_compact_is_idempotent_and_preserves_findings_and_statuses(self):
        doc = self.document
        item = doc['findings'][0]
        item['details'] = {'Current step':'fixing', 'Earlier verification (0.2.101)':'old',
                           **{f'Fact {i}':str(i) for i in range(8)}, 'Fact 8':'x' * 700}
        item['verification_refs'] = ['reference-neutral']
        def evidence(outcome, day, reference):
            return {'check':'test', 'outcome':outcome, 'build':'ACU 1.2.3', 'session':'run',
                    'timestamp':f'2026-11-{day:02d}T00:00:00Z', 'reference':reference}
        item['verification_evidence'] = [evidence('passed', 1, 'pass-old'), evidence('failed', 2, 'fail-old'),
                                         evidence('passed', 3, 'pass-new'), evidence('failed', 4, 'fail-new'),
                                         evidence('reviewed', 5, 'reference-neutral')]
        doc['tracking'] = {'verification_history': [
            {'id':'first','status':'complete','checks':{'ISSUE-01':{'state':'blocked','evidence':'observed state'}}},
            {'id':'repeat','status':'complete','checks':{'ISSUE-01':{'state':'inconclusive','evidence':'observed state'}}},
            {'id':'empty-interrupted','status':'interrupted','checks':{'ISSUE-02':{
                'state':'blocked','evidence':'Not checked during this installation; earlier evidence preserved'}}},
            {'id':'no-result-interrupted','status':'interrupted','checks':{
                'ISSUE-02':{'state':'checking'}, 'ISSUE-03':{'state':'pending'}}},
            {'id':'carryover','status':'complete','checks':{'ISSUE-02':{
                'state':'inconclusive','evidence':'Prerequisite or evidence unchanged from the preceding installed run: no new observation'}}},
            {'id':'fresh-no-check','status':'complete','checks':{'ISSUE-02':{
                'state':'blocked','evidence':'The fresh Calculator run did not invoke Ledger preference recall.'}}},
            {'id':'no-task-after-install','status':'complete','checks':{'ISSUE-02':{
                'state':'blocked','evidence':'No task run after install, so the requested flow was not exercised.'}}},
            {'id':'useful','status':'complete','checks':{'ISSUE-02':{'state':'fixed','evidence':'verified result'}}}],
            'archived_findings':[{'id':'ARCHIVE-01'}]}
        self.data.write_text(json.dumps(doc))
        before_statuses = [(x['id'], x['status']) for x in doc['findings']]
        page = self.root / 'compact.html'
        result = self.cli('compact', self.data, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'\d+ -> \d+ bytes')
        compacted = json.loads(self.data.read_text())
        self.assertEqual([(x['id'], x['status']) for x in compacted['findings']], before_statuses)
        self.assertNotIn('archived_findings', compacted['tracking'])
        history = compacted['tracking']['verification_history']
        self.assertEqual([run['id'] for run in history], ['first','repeat','carryover','fresh-no-check','no-task-after-install','useful'])
        self.assertEqual(compacted['tracking']['verification_history'][1]['checks'], {})
        self.assertEqual(history[2]['checks'], {})
        self.assertEqual(history[3]['checks'], {})
        self.assertEqual(history[4]['checks'], {})
        result_item = compacted['findings'][0]
        self.assertEqual(len(result_item['details']), 8)
        self.assertNotIn('Current step', result_item['details'])
        self.assertNotIn('Earlier verification (0.2.101)', result_item['details'])
        self.assertEqual(len(result_item['details']['Fact 8']), 600)
        self.assertEqual([r['reference'] for r in result_item['verification_evidence']],
                         ['pass-new','fail-new','reference-neutral'])
        first_bytes = self.data.read_bytes()
        again = self.cli('compact', self.data)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.data.read_bytes(), first_bytes)

    def test_plan_fix_cannot_restart_before_current_introspect(self):
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','name':'Plain group','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','worktree':'fix-a','codex':{'implementer_session':'writer','reviewer_session':'reviewer'}}]}}
        self.data.write_text(json.dumps(self._path1_pre()))
        before = self.data.read_bytes()
        ops = self.root / 'round.json'
        ops.write_text(json.dumps([plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Plan'}]))
        result = self.cli('batch', self.data, ops, '--render', self.root / 'round.html')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record this round's introspect", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_set_activity_marks_group_and_renders_single_now_line(self):
        operations, page = self.root / 'activity.json', self.root / 'activity.html'
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer'}}]}}
        operations.write_text(json.dumps([plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],
                                           'now':'Inspecting the shared root cause'}]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        self.assertEqual(doc['findings'][0]['status'], 'in_progress')
        activity = doc['tracking']['workflow']['now']
        self.assertRegex(activity, r'^\d{4}-\d\d-\d\dT.*Z — Inspecting the shared root cause$')
        html = page.read_text()
        self.assertIn('class="fresh">Updated', html)
        self.assertNotIn('class="workflow-now"', html)
        self.assertNotIn('id="verification-progress"', html)

    def test_batch_rollover_requires_adopted_live_list_and_preserves_source(self):
        original = self.data.read_bytes()
        operations = self.root / 'rollover.json'
        operations.write_text(json.dumps([{'action':'rollover','source_pr':96,'worktree':'next-wt',
            'pull_request':{'label':'PR #98','url':'https://example.test/pull/98'}}]))
        result = self.cli('batch', self.data, operations, '--force')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('adopted live tracker', result.stderr)
        self.assertEqual(self.data.read_bytes(), original)

    def test_report_rollover_gate_and_force_logging(self):
        page, operations = self.root / 'report-gate.html', self.root / 'report-gate.json'
        operations.write_text(json.dumps([{'action':'set-stage','stage':'report_rollover'}]))
        original = self.data.read_bytes()
        blocked = self.cli('batch', self.data, operations, '--render', page)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('1 of 15 verified_fixed', blocked.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        forced = self.cli('batch', self.data, operations, '--render', page, '--force', '--reason', 'test override')
        self.assertEqual(forced.returncode, 0, forced.stderr)
        tracking = json.loads(self.data.read_text())['tracking']
        self.assertEqual(tracking['rollover']['forced_gate_overrides'][0]['cycle'], 1)
        self.assertEqual(tracking['rollover']['forced_gate_overrides'][0]['threshold'], 15)

    def test_rollover_gate_uses_configured_threshold_or_no_open_findings(self):
        page, operations = self.root / 'threshold.html', self.root / 'threshold.json'
        document = json.loads(self.data.read_text())
        document['tracking'] = {'rollover_threshold':1}
        self.data.write_text(json.dumps(document))
        operations.write_text(json.dumps([{'action':'set-stage','stage':'report_rollover'}]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.data.read_text())
        for item in document['findings']:
            item.update(status='verified_fixed', verification='Verified.', verification_refs=['receipt'])
        document['tracking'] = {'rollover_threshold':15}
        self.data.write_text(json.dumps(document))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_cycle_needs_introspect_before_report_small_cycle_does_not(self):
        page = self.root / 'gate.html'
        def batch(*items, force=False):
            ops = self.root / 'gate.json'
            ops.write_text(json.dumps(list(items)))
            args = ['batch', self.data, ops, '--render', page]
            if force:
                args.extend(['--force', '--reason', 'test override'])
            return self.cli(*args)
        document = json.loads(self.data.read_text())
        document.setdefault('tracking', {})['plan'] = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}
        document['tracking']['stages_reached'] = ['verify']
        self.data.write_text(json.dumps(document))
        result = batch({'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'no issues'}, {'action':'set-stage','stage':'report_rollover'}, force=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(batch({'action':'set-stage','stage':'install'}).returncode, 0)
        blocked = batch({'action':'set-stage','stage':'report_rollover'})
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('gate not met', blocked.stderr)
        original = self.data.read_bytes()
        rollover = batch({'action':'rollover','source_pr':1,'worktree':'w','pull_request':{'label':'PR #2','url':'https://example.test/pull/2'}})
        self.assertNotEqual(rollover.returncode, 0)
        self.assertIn('adopted live tracker', rollover.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        self.assertNotEqual(batch({'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'two\nlines'}).returncode, 0)
        self.assertEqual(batch({'action':'start-verification','run':{'id':'r1','build':'b1','revision':'abc','installed_at':'2026-11-12T10:00:00+05:30','install_evidence':'log'}}).returncode, 0)
        result = batch({'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'no issues'}, {'action':'set-stage','stage':'report_rollover'}, force=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())['tracking']['introspect']['result'], 'no issues')
        original = self.data.read_bytes()
        result = batch({'action':'rollover','source_pr':1,'worktree':'w','pull_request':{'label':'PR #2','url':'https://example.test/pull/2'}}, force=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('adopted live tracker', result.stderr)
        self.assertEqual(self.data.read_bytes(), original)

    def test_introspect_records_history_not_finding(self):
        page, ops = self.root / 'one.html', self.root / 'one.json'
        def batch(*items):
            ops.write_text(json.dumps(list(items)))
            return self.cli('batch', self.data, ops, '--render', page)
        none = {'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}}
        doc = json.loads(self.data.read_text())
        doc.setdefault('tracking', {})['stages_reached'] = ['collect', 'present', 'plan_fix', 'install', 'verify']
        doc['tracking']['plan'] = {'round': 1, 'cycle': 1, 'groups': [{
            'id': 'A', 'finding_ids': ['ISSUE-01'], 'codex': {
                'implementer_session': 'writer', 'reviewer_session': 'reviewer', 'review_status': 'passed'}}]}
        self.data.write_text(json.dumps(doc), encoding='utf-8')
        self.assertNotEqual(batch({'action':'introspect','result':'no issues'}).returncode, 0)
        self.assertNotEqual(batch({'action':'introspect','issues':[{'issue':'x','evidence':'e','outcome':'fixed'}]}).returncode, 0)
        self.assertNotEqual(batch({'action':'introspect','sections':none,'issues':[{'issue':'x','evidence':'e','outcome':'fixed'}]}).returncode, 0)
        before = len(json.loads(self.data.read_text())['findings'])
        # missing section
        incomplete = dict(none); del incomplete['automate']
        self.assertNotEqual(batch({'action':'introspect','sections':incomplete}).returncode, 0)
        # linked issue must already exist
        bad_link = dict(none)
        bad_link['delete'] = {'items':[{'item':'fold merge guards','evidence':'log','outcome':'issue','issue_id':'BCL-99'}]}
        self.assertNotEqual(batch({'action':'introspect','sections':bad_link}).returncode, 0)
        add = batch({'action':'add','entry':{'id':'BCL-02','title':'Store verify command','source':'Agent reported','status':'untested','observed':'o','recommendation':'r'}})
        self.assertEqual(add.returncode, 0, add.stderr)
        sections = dict(none)
        sections['delete'] = {'items':[{'item':'drop unused hook path','evidence':'hook.log','outcome':'fixed inline','proof':'python3 test_hooks.py'}]}
        sections['simplify'] = {'items':[{'item':'store the verify command','evidence':'plan.log','outcome':'issue','issue_id':'BCL-02'}]}
        result = batch({'action':'introspect','sections':sections})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        self.assertEqual(len(doc['findings']), before + 1)  # BCL-02 only
        record = doc['tracking']['introspect']
        self.assertNotIn('task', record)
        self.assertEqual(record['sections']['delete']['items'][0]['outcome'], 'fixed inline')
        self.assertEqual(record['sections']['simplify']['items'][0]['issue_id'], 'BCL-02')
        self.assertEqual(doc['tracking']['introspect_history'][-1], record)
        self.assertFalse(any(item['id'] == 'WF-01' for item in doc['findings']))
        self.assertIn('introspect-sections', page.read_text())
        self.assertEqual(doc['tracking']['plan']['status'] if 'plan' in doc.get('tracking',{}) else 'closed', 'closed')

    def test_introspect_leaves_active_verification_checks_unchanged(self):
        page, ops = self.root / 'run.html', self.root / 'run.json'
        def batch(*items):
            ops.write_text(json.dumps(list(items)))
            return self.cli('batch', self.data, ops, '--render', page)
        none = {'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}}
        doc = json.loads(self.data.read_text())
        doc.setdefault('tracking', {})['stages_reached'] = ['collect', 'present', 'plan_fix', 'install', 'verify']
        doc['tracking']['plan'] = {'round': 1, 'cycle': 1, 'groups': [{
            'id': 'A', 'finding_ids': ['ISSUE-01'], 'codex': {
                'implementer_session': 'writer', 'reviewer_session': 'reviewer', 'review_status': 'passed'}}]}
        self.data.write_text(json.dumps(doc), encoding='utf-8')
        run = {'id':'r9','build':'b9','revision':'abc','installed_at':'2026-11-14T12:05:10+05:30','install_evidence':'log'}
        self.assertEqual(batch({'action':'start-verification','run':run}).returncode, 0)
        before = json.loads(self.data.read_text())['tracking']['verification_run']['checks']
        result = batch({'action':'introspect','sections':none})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        self.assertEqual(doc['tracking']['verification_run']['checks'], before)
        self.assertEqual(len(doc['tracking']['introspect_history']), 1)
        self.assertEqual(set(doc['tracking']['verification_run']['checks']),
                         {item['id'] for item in doc['findings']})

    def test_start_verification_skips_archived(self):
        document = json.loads(self.data.read_text())
        archived, current = document['findings'][:2]
        archived.update(status='verified_fixed', verification='Verified.', verification_refs=['receipt'])
        current.update(id='ISSUE-02', status='untested')
        document['findings'] = [archived, current]
        document.setdefault('tracking', {})['rollover'] = {'excluded_ids': [archived['id']]}
        self.data.write_text(json.dumps(document), encoding='utf-8')
        operations = self.root / 'archive-start.json'
        page = self.root / 'archive-start.html'
        operations.write_text(json.dumps([
            {'action': 'archive'},
            {'action': 'start-verification', 'run': {
                'id': 'after-archive', 'build': '1.2.3', 'revision': 'abc',
                'installed_at': '2026-11-18T09:00:00Z', 'install_evidence': 'install.log'}},
        ]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual([item['id'] for item in saved['findings']], ['ISSUE-02'])
        self.assertEqual(set(saved['tracking']['verification_run']['checks']), {'ISSUE-02'})
        self.assertEqual(json.loads((self.data.parent / 'archive.json').read_text())[0]['id'], archived['id'])

    def test_verify_and_status_writes_sync_plan_group_verify(self):
        page, ops = self.root / 'sync.html', self.root / 'sync.json'
        def batch(*items):
            ops.write_text(json.dumps(list(items)))
            return self.cli('batch', self.data, ops, '--render', page)
        added = batch({'action':'add','entry':{'id':'ISSUE-03','title':'Third issue','source':'Agent reported','status':'untested','observed':'o','recommendation':'r'}})
        self.assertEqual(added.returncode, 0, added.stderr)
        plan = {'round':1,'groups':[
            {'id':'E','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','verify':{'status':'pending'}},
            {'id':'AC','finding_ids':['ISSUE-02','ISSUE-03'],'depends_on':[],'status':'pending','verify':{'status':'pending'}},
        ]}
        planned = batch({'action':'set-plan','plan':plan})
        self.assertEqual(planned.returncode, 0, planned.stderr)
        # One verified, one still open in AC; E fully verified
        proof = {'status':'verified_fixed','verification':'Verified on the installed build.','verification_refs':['Example receipt']}
        updated = batch({'action':'update','id':'ISSUE-01','patch':proof}, {'action':'update','id':'ISSUE-02','patch':proof})
        self.assertEqual(updated.returncode, 0, updated.stderr)
        groups = {g['id']: g for g in json.loads(self.data.read_text())['tracking']['plan']['groups']}
        self.assertEqual(groups['E']['verify']['status'], 'verified')
        self.assertEqual(groups['AC']['verify']['status'], 'partial')
        self.assertEqual(groups['AC']['verify']['fixed'], 1)
        self.assertEqual(groups['AC']['verify']['total'], 2)
        self.assertEqual(batch({'action':'update','id':'ISSUE-03','patch':{'status':'failing'}}).returncode, 0)
        groups = {g['id']: g for g in json.loads(self.data.read_text())['tracking']['plan']['groups']}
        self.assertEqual(groups['AC']['verify']['status'], 'partial')
        page_text = page.read_text()
        self.assertTrue('Failed' in page_text or 'failed' in page_text or 'partial' in page_text)

    def test_verifying_lane_closes_from_fixed_or_failed_result(self):
        page, ops = self.root / "lane-result.html", self.root / "lane-result.json"
        for finding_status, expected_lane_status in (("verified_fixed", "verified"), ("failing", "failed")):
            with self.subTest(finding_status=finding_status):
                document = copy.deepcopy(self.document)
                finding = document["findings"][0]
                finding["status"] = "in_progress"
                tracking = document.setdefault("tracking", {})
                tracking.update(cycle=1, round=1, stages_reached=["collect", "present", "plan_fix", "install", "verify"],
                    workflow={"stage":"verify", "updated_at":"2026-11-18T00:00:00Z"},
                    plan={"cycle":1, "round":1, "groups":[{"id":"A", "finding_ids":[finding["id"]],
                        "depends_on":[], "status":"verifying", "phase":"verify", "verify":{"status":"pending"}}]})
                self.data.write_text(json.dumps(document))
                patch = {"status":finding_status}
                if finding_status == "verified_fixed":
                    patch.update(verification="The behavior was fixed.", verification_refs=["Verification receipt"])
                ops.write_text(json.dumps([{"action":"update", "id":finding["id"], "patch":patch}]))
                result = self.cli("batch", self.data, ops, "--render", page)
                self.assertEqual(result.returncode, 0, result.stderr)
                group = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]
                self.assertEqual(group["status"], expected_lane_status)
                self.assertEqual(group["phase"], "verify")

    def test_set_activity_rejects_non_iso_timestamp(self):
        result = self._replay_batch(self.document, {
            "action":"set-activity", "stage":"collect", "ids":[], "now":"Collecting",
            "timestamp":"yesterday",
        })
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("set-activity timestamp must be an ISO 8601 instant with timezone", result.stderr)

    def test_set_activity_requires_reopen_for_closed_findings(self):
        operations, page = self.root / 'closed-activity.json', self.root / 'closed-activity.html'
        document = json.loads(self.data.read_text())
        document['tracking'] = {'plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-02'],'depends_on':[],'status':'pending'}]}}
        self.data.write_text(json.dumps(document))
        for status in ('verified_fixed', 'closed'):
            document = json.loads(self.data.read_text())
            document['findings'][1]['status'] = status
            self.data.write_text(json.dumps(document))
            operation = {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-02'],
                         'now':'Rechecking the closed finding'}
            operations.write_text(json.dumps([operation]))
            before = self.data.read_bytes()
            result = self.cli('batch', self.data, operations, '--render', page)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('pass "reopen": true', result.stderr)
            self.assertEqual(self.data.read_bytes(), before)
            operations.write_text(json.dumps([{**operation,'reopen':True}]))
            result = self.cli('batch', self.data, operations, '--render', page)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(self.data.read_text())['findings'][1]['status'], 'in_progress')

    def test_check_records_use_structured_provenance_not_wording(self):
        operations, page = self.root / 'guard.json', self.root / 'guard.html'
        run = {'id':'guard-run','build':'ACU','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        operations.write_text(json.dumps([{'action':'start-verification','run':run}]))
        self.assertEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
        before = self.data.read_bytes()
        operations.write_text(json.dumps([{'action':'start-check','id':'ISSUE-01','run_id':'guard-run'}]))
        self.assertNotEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        start = {'action':'start-check','id':'ISSUE-01','run_id':'guard-run',
                 'observation':'did not reproduce after 20 attempts','observed_at':'2026-11-06T00:01:00Z'}
        missing_stamp = {k:v for k,v in start.items() if k!='observed_at'}
        operations.write_text(json.dumps([missing_stamp]))
        missing_result = self.cli('batch', self.data, operations, '--render', page)
        self.assertNotEqual(missing_result.returncode, 0)
        self.assertIn('observed_at', missing_result.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        for invalid in ({**start,'carried_over':True}, {**start,'no_new_observation':True},
                        {**start,'new_observation':False}, {**start,'observed_at':'2026-11-05T23:59:00Z'}):
            operations.write_text(json.dumps([invalid]))
            self.assertNotEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
            self.assertEqual(self.data.read_bytes(), before)
        operations.write_text(json.dumps([start]))
        self.assertEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
        for marker in ({'carried_over':True}, {'no_new_observation':True}, {'new_observation':False}):
            operations.write_text(json.dumps([{'action':'finish-check','id':'ISSUE-01','run_id':'guard-run',
                                               'outcome':'inconclusive','evidence':'Checked: issue behavior; Expected: reproduce; Observed: did not reproduce after 20 attempts',
                                               **marker}]))
            before = self.data.read_bytes()
            self.assertNotEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
            self.assertEqual(self.data.read_bytes(), before)
        finish = {'action':'finish-check','id':'ISSUE-01','run_id':'guard-run',
                  'outcome':'inconclusive','evidence':'Checked: issue behavior; Expected: reproduce; Observed: did not reproduce after 20 attempts'}
        operations.write_text(json.dumps([finish]))
        self.assertEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-01']['observed_at'], start['observed_at'])
        repeat = {**start,'recheck':True,'observed_at':'2026-11-06T00:02:00Z'}
        operations.write_text(json.dumps([repeat]))
        self.assertEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
        operations.write_text(json.dumps([finish]))
        self.assertEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)

    def test_compact_and_audit_use_check_provenance_not_evidence_wording(self):
        wording = 'No new issue-specific live observation; prior evidence is preserved.'
        def run(identifier, observed_at, carried_over=False):
            check = {'state':'inconclusive','evidence':wording,'observed_at':observed_at}
            if carried_over:
                check['carried_over'] = True
            return {'id':identifier,'status':'complete','build':'ACU','revision':'abc',
                    'installed_at':'2026-11-06T00:00:00Z','checks':{'ISSUE-01':check}}
        document = self.document
        document['tracking'] = {'verification_history':[
            run('fresh-1','2026-11-06T00:01:00Z'), run('fresh-2','2026-11-06T00:02:00Z')]}
        self.data.write_text(json.dumps(document))
        compact = self.cli('compact', self.data)
        self.assertEqual(compact.returncode, 0, compact.stderr)
        history = json.loads(self.data.read_text())['tracking']['verification_history']
        self.assertEqual(len(history), 2)
        self.assertEqual(self.cli('audit', self.data).returncode, 0)
        history.append(run('marked','2026-11-06T00:03:00Z', carried_over=True))
        document = json.loads(self.data.read_text())
        document['tracking']['verification_history'] = history
        self.data.write_text(json.dumps(document))
        audit = self.cli('audit', self.data)
        self.assertEqual(audit.returncode, 1)
        self.assertIn('carryover_check', audit.stdout)

    def test_detail_guards_and_audit_budget(self):
        entry = self.root / 'entry.json'
        entry.write_text(json.dumps({'id':'ISSUE-03','details':{'Current step':'fixing'}}))
        before = self.data.read_bytes()
        self.assertNotEqual(self.cli('add', self.data, entry).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        patch = self.root / 'patch.json'
        patch.write_text(json.dumps({'details':{'Too long':'x' * 601}}))
        self.assertNotEqual(self.cli('update', self.data, 'ISSUE-01', patch).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        operations = self.root / 'batch.json'
        operations.write_text(json.dumps([{'action':'update','id':'ISSUE-01',
                                           'patch':{'details':{'Evaluation workflow':'trial'}}}]))
        self.assertNotEqual(self.cli('batch', self.data, operations).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        doc = json.loads(self.data.read_text())
        doc['findings'][0]['details']['Current step'] = 'fixing'
        self.data.write_text(json.dumps(doc))
        audit = self.cli('audit', self.data)
        self.assertEqual(audit.returncode, 1)
        self.assertIn('denied_detail_key', audit.stdout)
        doc['padding'] = 'x' * 400_000
        self.data.write_text(json.dumps(doc))
        audit = self.cli('audit', self.data)
        self.assertEqual(audit.returncode, 1)
        self.assertIn('tracker_over_budget', audit.stdout)
        self.assertIn('run manage_findings.py compact', audit.stdout)

    def test_record_evidence_keeps_latest_classes_singleton_outcomes_and_references(self):
        item = self.document['findings'][0]
        item['verification_refs'] = ['keep this record']
        def evidence(outcome, day, reference):
            return {'checked':'test behavior','expected':'test result', 'observed':reference,
                    'verdict':outcome,'build':'ACU 1.2.3','session':f'run-{day}',
                    'timestamp':f'2026-11-{day:02d}T00:00:00Z','reference':reference}
        item['verification_evidence'] = [
            evidence('passed', 1, 'old-pass'), evidence('failed', 2, 'old-fail'),
            evidence('blocked', 3, 'old-blocked'), evidence('inconclusive', 4, 'only-inconclusive'),
            evidence('passed', 5, 'pass-new'), evidence('failed', 6, 'fail-new'),
            evidence('blocked', 7, 'latest-blocked'), evidence('reviewed', 8, 'keep this record'),
            evidence('manual review', 9, 'old-manual-outcome'),
            evidence('manual review', 10, 'latest-manual-outcome')]
        self.data.write_text(json.dumps(self.document))
        new_record = self.root / 'evidence.json'
        record = evidence('passed', 11, 'new-pass')
        record['check_id'] = 'check-11'
        new_record.write_text(json.dumps(record))
        result = self.cli('record-evidence', self.data, 'ISSUE-01', new_record)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(self.data.read_text())['findings'][0]['verification_evidence']
        self.assertEqual([row['reference'] for row in records],
                         ['only-inconclusive','fail-new','latest-blocked',
                          'keep this record','latest-manual-outcome','new-pass'])

    def test_completed_check_correction_preserves_original(self):
        run = {'id':'install-1', 'build':'ACU 1', 'revision':'abc', 'installed_at':'2026-11-06T00:00:00Z', 'install_evidence':'receipt'}
        ops, page = self.root / 'ops.json', self.root / 'report.html'
        def batch(*items):
            ops.write_text(json.dumps(items))
            result = self.cli('batch', self.data, ops, '--render', page)
            self.assertEqual(result.returncode, 0, result.stderr)
        batch({'action':'start-verification','run':run})
        for issue, outcome in [('ISSUE-01','inconclusive'), ('ISSUE-02','fixed')]:
            batch({'action':'start-check','id':issue,'run_id':'install-1','observation':f'{issue} initial state','observed_at':'2026-11-06T00:01:00Z'})
            batch({'action':'finish-check','id':issue,'run_id':'install-1','outcome':outcome,'evidence':'Checked: issue behavior; Expected: outcome; Observed: original evidence'})
        batch({'action':'complete-verification','run_id':'install-1'})
        batch({'action':'correct-check','id':'ISSUE-02','run_id':'install-1','outcome':'inconclusive','reason':'Checked: issue behavior; Expected: outcome; Observed: proof did not establish the outcome'})
        doc = json.loads(self.data.read_text())
        check = doc['tracking']['verification_run']['checks']['ISSUE-02']
        self.assertEqual(check['state'], 'inconclusive')
        self.assertEqual(check['corrections'][0]['previous']['evidence'], 'Checked: issue behavior; Expected: outcome; Observed: original evidence')
        self.assertEqual(doc['findings'][1]['status'], 'investigation')
        self.assertIn('proof did not establish the outcome', page.read_text())

    def test_install_verification_keeps_fixed_until_a_new_failure(self):
        ops = self.root / 'verify.json'
        page = self.root / 'verify.html'
        run = {'id':'install-1','build':'ACU 1','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        def batch(items):
            ops.write_text(json.dumps(items))
            return self.cli('batch', self.data, ops, '--render', page)
        self.assertEqual(batch([{'action':'start-verification','run':run}]).returncode, 0)
        doc = json.loads(self.data.read_text())
        self.assertEqual(doc['findings'][1]['status'], 'verified_fixed')
        self.assertEqual(doc['tracking']['verification_run']['checks']['ISSUE-02']['state'], 'pending')
        before = self.data.read_bytes()
        self.assertNotEqual(batch([{'action':'start-check','id':'ISSUE-02','run_id':'wrong','observation':'wrong-run state','observed_at':'2026-11-06T00:01:00Z'}]).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        self.assertEqual(batch([{'action':'start-check','id':'ISSUE-02','run_id':'install-1','observation':'first issue 2 state','observed_at':'2026-11-06T00:01:00Z'}]).returncode, 0)
        self.assertEqual(batch([{'action':'finish-check','id':'ISSUE-02','run_id':'install-1','outcome':'blocked','blocker':'Device access','evidence':'Checked: issue behavior; Expected: device available; Observed: no device'}]).returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())['findings'][1]['status'], 'verified_fixed')
        self.assertNotEqual(batch([{'action':'complete-verification','run_id':'install-1'}]).returncode, 0)
        self.assertEqual(batch([{'action':'start-check','id':'ISSUE-01','run_id':'install-1','observation':'first issue 1 state','observed_at':'2026-11-06T00:01:00Z'}]).returncode, 0)
        self.assertEqual(batch([{'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'not_fixed','evidence':'Checked: issue behavior; Expected: fix; Observed: observed failure'}]).returncode, 0)
        self.assertEqual(batch([{'action':'complete-verification','run_id':'install-1'}]).returncode, 0)
        doc = json.loads(self.data.read_text())
        self.assertEqual(doc['findings'][0]['status'], 'failing')
        self.assertEqual(doc['findings'][1]['status'], 'verified_fixed')
        run['id'] = 'install-2'
        self.assertEqual(batch([{'action':'start-verification','run':run}]).returncode, 0)
        doc = json.loads(self.data.read_text())
        self.assertEqual(doc['tracking']['verification_history'][0]['id'], 'install-1')
        self.assertEqual(doc['tracking']['verification_run']['checks']['ISSUE-02']['state'], 'pending')
        self.assertEqual(doc['findings'][1]['status'], 'verified_fixed')
        self.assertEqual(batch([{'action':'start-check','id':'ISSUE-02','run_id':'install-2','observation':'second install issue 2 state','observed_at':'2026-11-06T00:01:00Z'}]).returncode, 0)
        self.assertEqual(batch([{'action':'finish-check','id':'ISSUE-02','run_id':'install-2','outcome':'not_fixed','evidence':'Checked: issue behavior; Expected: fixed; Observed: failure recurred'}]).returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())['findings'][1]['status'], 'failing')

    def test_verification_completion_ignores_findings_added_after_start(self):
        ops, page = self.root / 'cohort.json', self.root / 'cohort.html'
        def batch(*actions):
            ops.write_text(json.dumps(actions))
            return self.cli('batch', self.data, ops, '--render', page)
        run = {'id':'cohort-run','build':'ACU 1','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(batch({'action':'start-verification','run':run}).returncode, 0)
        added = copy.deepcopy(json.loads(self.data.read_text())['findings'][0])
        added.update(id='ISSUE-03', title='Added after start')
        self.assertEqual(batch({'action':'add','entry':added,'new':True}).returncode, 0)
        self.assertIn('Not checked this install', page.read_text())
        for issue in ('ISSUE-01', 'ISSUE-02'):
            self.assertEqual(batch({'action':'start-check','id':issue,'run_id':'cohort-run','observation':'Initial state','observed_at':'2026-11-06T00:01:00Z'}).returncode, 0)
            self.assertEqual(batch({'action':'finish-check','id':issue,'run_id':'cohort-run','outcome':'inconclusive','evidence':'Checked: issue behavior; Expected: known; Observed: unclear'}).returncode, 0)
        result = batch({'action':'complete-verification','run_id':'cohort-run'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())['tracking']['verification_run']['status'], 'complete')
        self.assertIn('Not checked this install', page.read_text())

    def test_add_does_not_reopen_completed_verification_run_for_new_finding(self):
        ops, page = self.root / 'completed-add.json', self.root / 'completed-add.html'
        def batch(*actions):
            ops.write_text(json.dumps(actions))
            return self.cli('batch', self.data, ops, '--render', page)
        run = {'id':'completed-run','build':'ACU 1','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(batch({'action':'start-verification','run':run}).returncode, 0)
        for issue in ('ISSUE-01', 'ISSUE-02'):
            self.assertEqual(batch({'action':'start-check','id':issue,'run_id':'completed-run','observation':'Initial state','observed_at':'2026-11-06T00:01:00Z'}).returncode, 0)
            self.assertEqual(batch({'action':'finish-check','id':issue,'run_id':'completed-run','outcome':'inconclusive','evidence':'Checked: issue behavior; Expected: known; Observed: unclear'}).returncode, 0)
        self.assertEqual(batch({'action':'complete-verification','run_id':'completed-run'}).returncode, 0)
        added = copy.deepcopy(json.loads(self.data.read_text())['findings'][0])
        added.update(id='ISSUE-03', title='Added after completion')
        result = batch({'action':'add','entry':added,'new':True})
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())['tracking']
        self.assertEqual(tracking['verification_run']['status'], 'complete')
        self.assertNotIn('ISSUE-03', tracking['verification_run']['checks'])
        self.assertIn('Not checked this install', page.read_text())

    def test_update_plan_group_accepts_quota_wait_until_and_rejects_bad_value(self):
        ops, page = self.root / 'quota.json', self.root / 'quota.html'
        def batch(*actions):
            ops.write_text(json.dumps(actions))
            return self.cli('batch', self.data, ops, '--render', page)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'running','phase':'fix'}]}
        self.assertEqual(batch({'action':'set-plan','plan':plan}).returncode, 0)
        result = batch({'action':'update-plan-group','id':'A','patch':{'quota_wait_until':'2026-11-14T12:00:00Z'}})
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(self.data.read_text())['tracking']['plan']['groups'][0]
        self.assertEqual(group['quota_wait_until'], '2026-11-14T12:00:00Z')
        before = self.data.read_bytes()
        result = batch({'action':'update-plan-group','id':'A','patch':{'quota_wait_until':'not-a-time'}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('quota_wait_until must be an ISO 8601 timestamp', result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_update_plan_group_cannot_start_lane_phase(self):
        ops, page = self.root / 'phase.json', self.root / 'phase.html'
        def batch(*actions):
            ops.write_text(json.dumps(actions))
            return self.cli('batch', self.data, ops, '--render', page)
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'running','phase':'fix'}]}
        self.assertEqual(batch({'action':'set-plan','plan':plan}).returncode, 0)
        result = batch({'action':'update-plan-group','id':'A','merge_codex':True,'patch':{'status':'running','codex':{'status':'running'}}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('use bcl job', result.stderr)

    def test_restore_main_guard_covers_group_membership_and_finding_updates(self):
        for marker in ("yes", "true"):
            with self.subTest(marker=marker):
                document = copy.deepcopy(self.document)
                document["findings"][1]["details"] = {"regression_against_main": marker}
                group = {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "deferred"}
                result = self._replay_batch(document,
                    {"action": "set-plan", "plan": {"round": 1, "groups": [group]}})
                self.assertEqual(result.returncode, 0, result.stderr)
                before = json.dumps(json.loads(self.data.read_bytes())).encode()
                result = self._replay_batch(json.loads(before), {"action": "update-plan-group", "id": "A",
                    "patch": {"finding_ids": ["ISSUE-01", "ISSUE-02"], "status": "planned"}})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("restore main", result.stderr)
                self.assertEqual(self.data.read_bytes(), before)

        document = copy.deepcopy(self.document)
        group = {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "planned"}
        self.assertEqual(self._replay_batch(document, {"action": "set-plan", "plan": {"round": 1, "groups": [group]}}).returncode, 0)
        before = json.dumps(json.loads(self.data.read_bytes())).encode()
        result = self._replay_batch(json.loads(before), {"action": "update", "id": "ISSUE-01",
            "patch": {"details": {"regression_against_main": "yes"}}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore main", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_blocked_lane_status_and_lane_history_survive_plan_edits(self):
        group = {"id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"], "depends_on": [], "status": "blocked",
                 "phase": "fix", "blocked_reason": "Second failure", "failure_block_reason": "failed",
                 "failure_history": {"ISSUE-01": [{"attempt_id": "a", "source": "review"}],
                                     "ISSUE-02": [{"attempt_id": "b", "source": "review"}]},
                 "plan_review_history": {"ISSUE-01": [{"job_id": "p", "after_failure": "b"}]}}
        result = self._replay_batch(copy.deepcopy(self.document), {"action": "set-plan", "plan": {"round": 1, "groups": [
            {"id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"], "depends_on": [], "status": "planned"}]}})
        self.assertEqual(result.returncode, 0, result.stderr)
        blocked = json.loads(self.data.read_bytes())
        # Gates come from job and verify writers; a plan write cannot claim them (BCL-145), so seed the stored lane.
        blocked["tracking"]["plan"]["groups"][0].update(copy.deepcopy(group))
        for changed in (
            {**copy.deepcopy(group), "finding_ids": ["ISSUE-01"]},
            {**copy.deepcopy(group), "id": "RENAMED"},
        ):
            result = self._replay_batch(blocked, {"action": "set-plan", "plan": {"round": 1, "groups": [changed]}})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("gated lane", result.stderr)
            self.assertEqual(json.loads(self.data.read_bytes()), blocked)
        result = self._replay_batch(blocked, {"action": "update-plan-group", "id": "A", "patch": {"status": "planned"}})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.data.read_bytes()), blocked)
        reordered = copy.deepcopy(blocked["tracking"]["plan"]["groups"][0])
        reordered["finding_ids"].reverse()
        result = self._replay_batch(blocked, {"action": "set-plan", "plan": {"round": 1, "groups": [reordered]}})
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(self.data.read_bytes())["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "blocked")
        self.assertEqual([row["attempt_id"] for row in group["failure_history"]], ["a", "b"])
        self.assertEqual(group["plan_review_history"][0]["after_failure"], "b")

    def test_bcl180_blocked_lane_can_only_be_parked_with_reason_and_keeps_gate(self):
        blocked = copy.deepcopy(self.document)
        blocked["tracking"] = {"cycle": 1, "round": 1, "plan": {"cycle": 1, "round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "status": "blocked", "phase": "fix",
            "failure_history": [{"attempt_id": "failure-1", "at": "2026-11-21T00:00:00Z"},
                                {"attempt_id": "failure-2", "at": "2026-11-21T00:01:00Z"}],
            "plan_review_history": [{"job_id": "review", "after_failure": "failure-2"}],
            "blocked_reason": "two counted failures"}]}}
        for patch in ({"status": "planned", "reason": "later"}, {"status": "parked"}):
            result = self._replay_batch(blocked, {"action": "update-plan-group", "id": "A", "patch": patch})
            self.assertNotEqual(result.returncode, 0)
        parked = self._replay_batch(blocked, {"action": "update-plan-group", "id": "A",
                                               "patch": {"status": "parked", "parked_to": 2,
                                                         "reason": "defer to next round"}})
        self.assertEqual(parked.returncode, 0, parked.stderr)
        current = json.loads(self.data.read_text())
        current["tracking"]["plan"]["groups"].append({"id": "B", "finding_ids": ["ISSUE-02"], "status": "verified"})
        self.assertTrue(manage_findings.release_check_payload(current)["clear"])
        next_round = [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]
        manage_findings.carry_plan_runtime_state(next_round, current["tracking"]["plan"], False)
        self.assertEqual(next_round[0]["status"], "blocked")
        self.assertEqual([row["attempt_id"] for row in next_round[0]["failure_history"]], ["failure-1", "failure-2"])

    def test_gated_lane_carries_its_gate_across_round_re_record(self):
        prior = {"groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "blocked",
                             "failure_history": [{"attempt_id": "failed", "at": "2026-11-21T00:00:00Z"}],
                             "plan_review_history": [{"job_id": "review", "after_failure": "failed"}],
                             "unblock_history": [{"at": "2026-11-21T00:00:01Z", "reason": "owner"}]}]}
        groups = [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "planned"}]
        manage_findings.carry_plan_runtime_state(groups, prior, False)
        self.assertEqual(groups[0]["status"], "blocked")
        self.assertEqual(groups[0]["failure_history"][0]["attempt_id"], "failed")
        self.assertEqual(groups[0]["plan_review_history"][0]["after_failure"], "failed")
        self.assertEqual(groups[0]["unblock_history"][0]["reason"], "owner")

    def test_new_round_keeps_verified_lanes_verified_and_carried_lanes_as_requested(self):
        prior = {"groups": [
            {"id": "V", "finding_ids": ["ISSUE-01"], "status": "verified", "phase": "done",
             "failure_history": [{"attempt_id": "old", "source": "review", "at": "2026-11-21T00:00:00Z"}],
             "verify": {"status": "passed", "summary": "named tests passed"}},
            {"id": "C", "finding_ids": ["ISSUE-02"], "status": "running", "phase": "review",
             "failure_history": [{"attempt_id": "f1", "source": "review", "at": "2026-11-21T00:00:00Z"}]}]}
        groups = [{"id": "C", "finding_ids": ["ISSUE-02"], "status": "deferred", "phase": "deferred"}]
        manage_findings.carry_plan_runtime_state(groups, prior, False)  # V omitted: allowed in a new round
        self.assertEqual((groups[0]["status"], groups[0]["phase"]), ("deferred", "deferred"))
        self.assertEqual(groups[0]["failure_history"][0]["attempt_id"], "f1")
        kept = [{"id": "V", "finding_ids": ["ISSUE-01"], "status": "planned"},
                {"id": "C", "finding_ids": ["ISSUE-02"], "status": "planned"}]
        manage_findings.carry_plan_runtime_state(kept, prior, False)
        self.assertEqual((kept[0]["status"], kept[0]["verify"]["status"]), ("verified", "passed"))
        with self.assertRaisesRegex(ValueError, "gated lane V"):
            manage_findings.carry_plan_runtime_state([], prior, True)  # same round still protects it

    def test_one_failure_refuses_set_plan_membership_or_id_change(self):
        previous = {"groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "status": "running",
                                "failure_history": [{"attempt_id": "failed", "source": "review"}]}]}
        for replacement in (
            {"id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"]},
            {"id": "A", "finding_ids": []},
            {"id": "RENAMED", "finding_ids": ["ISSUE-01"]},
        ):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, "gated lane"):
                manage_findings.carry_plan_runtime_state([replacement], previous, True)

    def test_update_plan_group_cannot_detach_a_finding_from_gated_lane(self):
        document = copy.deepcopy(self.document)
        document["tracking"] = {"cycle": 1, "round": 1, "plan": {"cycle": 1, "round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"], "status": "running",
            "failure_history": [{"attempt_id": "one", "source": "review"}]}]}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, {"action": "update-plan-group", "id": "A",
                                              "patch": {"finding_ids": ["ISSUE-02"]}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gated lane", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_checking_must_be_saved_before_finishing(self):
        ops = self.root / 'verify.json'
        run = {'id':'install-1','build':'ACU','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        ops.write_text(json.dumps([{'action':'start-verification','run':run}]))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', self.root / 'verify.html').returncode, 0)
        before = self.data.read_bytes()
        ops.write_text(json.dumps([{'action':'start-check','id':'ISSUE-01','run_id':'install-1','observation':'checking begins','observed_at':'2026-11-06T00:01:00Z'},
                                   {'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'not_fixed','evidence':'Checked: issue behavior; Expected: fixed; Observed: failed'}]))
        self.assertNotEqual(self.cli('batch', self.data, ops, '--render', self.root / 'verify.html').returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)

    def test_batch_updates_and_renders_once_and_rejects_invalid_timestamp(self):
        operations = self.root / 'batch.json'
        page = self.root / 'batch.html'
        record = {'checked':'client behavior', 'expected':'passes', 'observed':'passed',
                  'verdict':'Fixed because the check passed', 'check_id':'client-check-1','build':'1', 'session':'run',
                  'timestamp':'2026-11-06T00:00:00Z', 'reference':'receipt'}
        operations.write_text(json.dumps([
            {'action':'update','id':'ISSUE-01','patch':{'verification':'Checked'}},
            {'action':'update','id':'ISSUE-02','patch':{'verification':'Also checked'}},
            {'action':'record-evidence','id':'ISSUE-01','record':record},
        ]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data['findings'][0]['verification'], 'Checked')
        self.assertEqual(data['findings'][0]['verification_evidence'], [record])
        self.assertIn('Also checked', page.read_text())
        before, html = self.data.read_bytes(), page.read_bytes()
        record['timestamp'] = '2026-11-06T05:57:07:00Z'
        operations.write_text(json.dumps([{'action':'update','id':'ISSUE-02','patch':{'title':'changed'}},
                                          {'action':'record-evidence','id':'ISSUE-01','record':record}]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        self.assertEqual(page.read_bytes(), html)
        single = self.root / 'invalid-record.json'
        single.write_text(json.dumps(record))
        self.assertNotEqual(self.cli('record-evidence', self.data, 'ISSUE-01', single).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)

    def test_batch_add_delete_and_render_failure_reports_committed_json(self):
        operations = self.root / 'batch.json'
        entry = dict(self.document['findings'][0], id='ISSUE-03')
        operations.write_text(json.dumps([{'action':'add','entry':entry,'new':True},
                                          {'action':'delete','id':'ISSUE-02'}]))
        result = self.cli('batch', self.data, operations, '--render', self.root / 'missing' / 'page.html')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('JSON updated but render failed', result.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual([row['id'] for row in data['findings']], ['ISSUE-01', 'ISSUE-03'])

    def test_structured_evidence_appends_without_promoting_and_rejects_invalid(self):
        document = json.loads(self.data.read_text())
        document['findings'][0]['verification_refs'] = ['receipt']
        self.data.write_text(json.dumps(document))
        record = self.root / 'record.json'
        record.write_text(json.dumps({'checked':'issue behavior', 'expected':'button remains visible',
                                      'observed':'button remained visible', 'verdict':'Fixed because behavior matched',
                                      'check_id':'run-1-check-1',
                                      'build':'0.2.94', 'session':'run-1', 'timestamp':'2026-11-06T00:00:00Z', 'reference':'receipt'}))
        for _ in range(2):
            result = self.cli('record-evidence', self.data, 'ISSUE-01', record)
            self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.data.read_text())['findings'][0]
        self.assertEqual(len(item['verification_evidence']), 1)
        self.assertEqual(item['status'], self.document['findings'][0]['status'])
        page = self.root / 'page.html'
        result = subprocess.run([sys.executable, str(RENDERER), str(self.data), str(page)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verification by version', page.read_text())
        before = self.data.read_bytes()
        record.write_text(json.dumps({'checked':'issue behavior', 'expected':'', 'observed':'passed',
                                      'verdict':'Fixed', 'check_id':'run-2-check-1', 'build':'0.2.94', 'session':'run-2',
                                      'timestamp':'2026-11-06T00:00:00Z', 'reference':'receipt'}))
        self.assertNotEqual(self.cli('record-evidence', self.data, 'ISSUE-01', record).returncode, 0)
        self.assertEqual(before, self.data.read_bytes())
        self.assertNotEqual(self.cli('record-evidence', self.data, 'missing', record).returncode, 0)

    def test_finish_check_reason_parts_are_nonempty_and_evidence_merges_by_check_id(self):
        operations, page = self.root / 'ops.json', self.root / 'page.html'
        def apply(*items):
            operations.write_text(json.dumps(items))
            return self.cli('batch', self.data, operations, '--render', page)
        run = {'id':'run-id','build':'0.2.94','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(apply({'action':'start-verification','run':run}).returncode, 0)
        self.assertEqual(apply({'action':'start-check','id':'ISSUE-01','run_id':'run-id',
                                'observation':'rechecking writers','observed_at':'2026-11-06T00:01:00Z'}).returncode, 0)
        before = self.data.read_bytes()
        invalid = apply({'action':'finish-check','id':'ISSUE-01','run_id':'run-id','outcome':'inconclusive',
                         'evidence':'Checked:; Expected:; Observed:'})
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn('nonempty Checked, Expected and Observed', invalid.stderr)
        self.assertEqual(self.data.read_bytes(), before)
        reason = 'Checked: both writer updates; Expected: both updates survive; Observed: both survived on the installed copy'
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-01','run_id':'run-id','outcome':'not_fixed',
                                'evidence':reason}).returncode, 0)
        data = json.loads(self.data.read_text())
        check = data['tracking']['verification_run']['checks']['ISSUE-01']
        record = {'check_id':check['check_id'],'checked':'both writer updates','expected':'both updates survive',
                  'observed':'both survived on the installed copy','verdict':'Failing because both updates should survive',
                  'build':run['build'],'session':run['id'],'timestamp':check['finished_at'],'reference':'receipt'}
        self.assertEqual(apply({'action':'record-evidence','id':'ISSUE-01','record':record}).returncode, 0)
        data = json.loads(self.data.read_text())
        item = data['findings'][0]
        check = data['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(item.get('verification_evidence', []), [])
        self.assertEqual({key:check[key] for key in record}, record)
        rows = manage_findings.RENDERER.version_evidence(item, [data['tracking']['verification_run']])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['verdict'], 'Failing because both survived on the installed copy')

    def test_distinct_evidence_ids_survive_matching_build_session_and_timestamp(self):
        records = self.root / 'records'
        def record(check_id, observed):
            return {'check_id':check_id,'checked':'writer updates','expected':'both survive','observed':observed,
                    'verdict':f'Failing because {observed}','build':'BCL 0.2.147','session':'same-session',
                    'timestamp':'2026-11-16T10:00:00Z','reference':check_id}
        for check_id, observed in [('check-1','only one survived'),('check-2','both survived')]:
            path = self.root / f'{check_id}.json'
            path.write_text(json.dumps(record(check_id, observed)))
            result = self.cli('record-evidence', self.data, 'ISSUE-01', path)
            self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.data.read_text())['findings'][0]
        self.assertEqual([row['check_id'] for row in item['verification_evidence']], ['check-1','check-2'])
        rows = manage_findings.RENDERER.version_evidence(item, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['observed'], 'both survived')
        self.assertEqual(len(rows[0]['history']), 1)

    def test_rewrite_evidence_converts_one_existing_log_record(self):
        legacy = {'check':'installed-cli-suite','outcome':'commit:abc installed-cli-suite passed',
                  'build':'0.2.94','session':'run-1','timestamp':'2026-11-06T00:00:00Z',
                  'reference':'backup=/tmp/backup receipt=/tmp/receipt elapsed=4s'}
        document = json.loads(self.data.read_text())
        document['findings'][0]['verification_evidence'] = [legacy]
        self.data.write_text(json.dumps(document))
        record = self.root / 'rewrite.json'
        record.write_text(json.dumps({'checked':'both tracker updates', 'expected':'both survive',
            'observed':'both survived on worktree and installed copy',
            'verdict':'Fixed because both updates survived', 'check_id':'run-1-check-1', 'build':'0.2.94', 'session':'run-1',
            'timestamp':'2026-11-06T00:00:00Z', 'reference':'backup=/tmp/backup receipt=/tmp/receipt elapsed=4s'}))
        result = self.cli('rewrite-evidence', self.data, 'ISSUE-01', record)
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.data.read_text())['findings'][0]
        self.assertEqual(len(item['verification_evidence']), 1)
        self.assertEqual(item['verification_evidence'][0]['checked'], 'both tracker updates')
        self.assertEqual(self.cli('rewrite-evidence', self.data, 'ISSUE-01', record).returncode, 0)
        self.assertEqual(len(json.loads(self.data.read_text())['findings'][0]['verification_evidence']), 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.data = self.root / "findings.json"
        self.document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.data.write_text(json.dumps(self.document), encoding="utf-8")

    def cli(self, *args, env=None):
        if len(args) > 2 and args[0] == 'batch':
            operations_path = Path(args[2])
            operations = json.loads(operations_path.read_text())
            tracker = json.loads(Path(args[1]).read_text())
            tracking = tracker.get("tracking", {})
            for index, operation in enumerate(operations):
                if operation.get('action') != 'introspect' or 'steps' in operation or not isinstance(operation.get('sections'), dict):
                    continue
                steps = [item.get('item') or item.get('step') for section in operation['sections'].values()
                         for item in section.get('items', []) if isinstance(item, dict)]
                prior_tracking = copy.deepcopy(tracking)
                for prior in operations[:index]:
                    if prior.get("action") == "set-plan":
                        prior_tracking["plan"] = prior.get("plan")
                        prior_tracking.setdefault("stages_reached", [])
                        if "plan_fix" not in prior_tracking["stages_reached"]:
                            prior_tracking["stages_reached"].append("plan_fix")
                    if prior.get("action") in ("set-stage", "set-activity"):
                        prior_tracking.setdefault("stages_reached", [])
                        stage = prior.get("stage")
                        if isinstance(stage, str) and stage not in prior_tracking["stages_reached"]:
                            prior_tracking["stages_reached"].append(stage)
                recorded = check_introspect.performed_round_steps(prior_tracking)
                steps = list(dict.fromkeys([*steps, *recorded]))
                if not steps:
                    steps = ['review the recorded round']
                    operation['none'] = steps.copy()
                else:
                    operation['none'] = recorded
                operation['steps'] = steps
            operations_path.write_text(json.dumps(operations))
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              text=True, capture_output=True,
                              env={**os.environ, "BCL_STATE_DIR": str(self.state), **(env or {})})

    def test_only_registered_live_mutations_create_unsnapshotted_marker(self):
        self.state.mkdir()
        live = self.state / '110.json'
        live.write_bytes(self.data.read_bytes())
        (self.state / 'registry.json').write_text(json.dumps({'repo/110': {'live':str(live), 'home':str(self.root),
            'snapshot':'snapshot.json', 'html':'report.html'}}))
        patch = self.root / 'patch.json'
        patch.write_text(json.dumps({'status':'in_progress'}))
        result = self.cli('update', live, 'ISSUE-01', patch)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = live.with_suffix('.unsnapshotted')
        self.assertEqual(marker.read_text().strip(), hashlib.sha256(live.read_bytes()).hexdigest())
        result = self.cli('update', self.data, 'ISSUE-01', patch)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.data.with_suffix('.unsnapshotted').exists())
        unregistered = self.state / '111.json'
        unregistered.write_bytes(self.data.read_bytes())
        before = unregistered.read_bytes()
        result = self.cli('update', unregistered, 'ISSUE-01', patch)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not registered', result.stderr)
        self.assertEqual(unregistered.read_bytes(), before)

    def test_add_update_and_batch_preserve_changes_but_fail_when_capture_missing(self):
        home = self.root / "project"
        base = home / "docs/triage"
        capture = base / "captures/proof.png"
        capture.parent.mkdir(parents=True)
        capture.write_bytes(b"not a real image; render only embeds bytes")
        self.state.mkdir()
        live = self.state / "example-project" / "110.json"
        live.parent.mkdir()
        document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        document["findings"][0]["evidence"] = [{"src": "captures/proof.png", "alt": "proof", "caption": "proof"}]
        live.write_text(json.dumps(document), encoding="utf-8")
        snapshot = base / "110-findings.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        registry = {"example/project/110": {"live": str(live), "home": str(home),
                    "snapshot": str(snapshot.relative_to(home)), "html": "docs/triage/110.html",
                    "repo": "example/project", "id": "110"}}
        (self.state / "registry.json").write_text(json.dumps(registry))
        patch = self.root / "patch.json"
        patch.write_text(json.dumps({"status": "in_progress"}))
        updated = self.cli("update", live, "ISSUE-01", patch)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(json.loads(live.read_text())["findings"][0]["status"], "in_progress")

        capture.unlink()
        added = self.root / "new-finding.json"
        added.write_text(json.dumps({"id": "ISSUE-03", "title": "New", "evidence": [
            {"src": "captures/missing.png", "alt": "missing", "caption": "missing"}]}))
        add_result = self.cli("add", live, added)
        self.assertNotEqual(add_result.returncode, 0)
        self.assertIn("JSON saved but report capture is missing", add_result.stderr)
        self.assertTrue(live.with_suffix(".unsnapshotted").exists())
        self.assertTrue(any(item["id"] == "ISSUE-03" for item in json.loads(live.read_text())["findings"]))

        operations = self.root / "ops.json"
        operations.write_text(json.dumps([{"action": "set-activity", "stage": "collect", "ids": [], "now": "Collecting"}]))
        page = base / "110.html"
        batch = self.cli("batch", live, operations, "--render", page)
        self.assertNotEqual(batch.returncode, 0)
        self.assertIn("JSON saved but report capture is missing", batch.stderr)
        self.assertFalse(page.exists())

    def test_list_and_get_return_only_requested_records(self):
        listing = self.cli("list", self.data)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        rows = json.loads(listing.stdout)
        self.assertEqual([row["id"] for row in rows], ["ISSUE-01", "ISSUE-02"])
        result = self.cli("get", self.data, "ISSUE-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["title"], "Icon appears too small")

    def test_readable_fields_store_and_summary_warnings_cover_all_commands(self):
        entry = self.root / 'entry.json'
        entry.write_text(json.dumps({'id':'ISSUE-03','title':'A control does not open','source':'User reported',
            'status':'untested','observed':'The control stays closed.','recommendation':'Restore the open action.',
            'impact':'Readers cannot continue.','done_when':'The control opens when selected.'}))
        added = self.cli('add', self.data, entry, '--new')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn('add summary, impact and done_when in plain words', added.stderr)
        patch = self.root / 'patch.json'
        patch.write_text(json.dumps({'summary':'trace_id=abc failed','impact':'The flow stops.','done_when':'The control opens.'}))
        updated = self.cli('update', self.data, 'ISSUE-03', patch)
        self.assertNotEqual(updated.returncode, 0)
        self.assertIn('move them to reference or details', updated.stderr)
        patch.write_text(json.dumps({'summary':'trace=abc failed','impact':'The flow stops.','done_when':'The control opens.'}))
        updated = self.cli('update', self.data, 'ISSUE-03', patch)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn('summary looks like log text', updated.stderr)
        self.assertEqual(json.loads(self.data.read_text())['findings'][-1]['summary'], 'trace=abc failed')

        operations = self.root / 'operations.json'
        operations.write_text(json.dumps([
            {'action':'add','entry':{'id':'ISSUE-04','title':'A row is missing','source':'User reported',
                'status':'untested','observed':'The row is missing.','recommendation':'Show the row.',
                'summary':'trace=abc failed','impact':'Information is hidden.','done_when':'The row appears.'}},
            {'action':'update','id':'ISSUE-03','patch':{'summary':'trace=abc failed','impact':'The task is blocked.'}}
        ]))
        batched = self.cli('batch', self.data, operations)
        self.assertEqual(batched.returncode, 0, batched.stderr)
        self.assertGreaterEqual(batched.stderr.count('summary looks like log text'), 2)
        findings = {item['id']:item for item in json.loads(self.data.read_text())['findings']}
        self.assertEqual(findings['ISSUE-04']['done_when'], 'The row appears.')
        self.assertEqual(findings['ISSUE-03']['impact'], 'The task is blocked.')

    def test_add_update_delete_and_render_empty(self):
        entry = self.root / "entry.json"
        entry.write_text(json.dumps({
            "id": "ISSUE-03", "title": "Island is too narrow", "source": "User reported",
            "status": "untested", "observed": "The default pill looks smaller than before.",
            "recommendation": "Restore the prior 74 pt idle width.", "details": {"Build": "Unknown"}
        }), encoding="utf-8")
        added = self.cli("add", self.data, entry)
        self.assertEqual(added.returncode, 0, added.stderr)

        patch = self.root / "patch.json"
        patch.write_text(json.dumps({
            "status": "verified_fixed", "verification": "Installed layout measures 74 pt.",
            "verification_refs": ["Live layout capture"], "details": {"Build": "0.2.92"}
        }), encoding="utf-8")
        updated = self.cli("update", self.data, "ISSUE-03", patch)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        saved = json.loads(self.data.read_text(encoding="utf-8"))
        issue = next(item for item in saved["findings"] if item["id"] == "ISSUE-03")
        self.assertEqual(issue["details"], {"Build": "0.2.92"})
        updated_page = self.root / "updated.html"
        rendered = subprocess.run([sys.executable, str(RENDERER), str(self.data), str(updated_page)],
                                  text=True, capture_output=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("0.2.92", updated_page.read_text(encoding="utf-8"))
        fetched = self.cli("get", updated_page, "ISSUE-03")
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(json.loads(fetched.stdout)["id"], "ISSUE-03")

        self.assertEqual(self.cli("delete", self.data, "ISSUE-01").returncode, 0)
        self.assertEqual(self.cli("delete", self.data, "ISSUE-02").returncode, 0)
        self.assertEqual(self.cli("delete", self.data, "ISSUE-03").returncode, 0)
        empty = json.loads(self.data.read_text(encoding="utf-8"))
        self.assertEqual(empty["findings"], [])
        page = self.root / "empty.html"
        rendered = subprocess.run([sys.executable, str(RENDERER), str(self.data), str(page)],
                                  text=True, capture_output=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("No tracked issues.", page.read_text(encoding="utf-8"))

    def test_failed_duplicate_or_invalid_update_preserves_source(self):
        original = self.data.read_bytes()
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(json.dumps(self.document["findings"][0]), encoding="utf-8")
        result = self.cli("add", self.data, duplicate)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.data.read_bytes(), original)

        invalid = self.root / "invalid.json"
        invalid.write_text(json.dumps({"status": "fixed"}), encoding="utf-8")
        result = self.cli("update", self.data, "ISSUE-01", invalid)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.data.read_bytes(), original)

    def test_identity_cannot_change(self):
        patch = self.root / "patch.json"
        patch.write_text(json.dumps({"id": "OTHER-ID"}), encoding="utf-8")
        result = self.cli("update", self.data, "ISSUE-01", patch)
        self.assertEqual(result.returncode, 2)
        self.assertIn("immutable", result.stderr)

    def test_html_collection_access_and_mutation_preserve_other_page_data(self):
        page = self.root / "legacy.html"
        primary = [{"id": "USER-01", "title": "Contains ] bracket", "status": "untested"}]
        issues = [{"id": "OBS-01", "title": "Telemetry lead"}]
        before = f"<script>const primary={json.dumps(primary)};const issues={json.dumps(issues)};</script>"
        page.write_text(before, encoding="utf-8")

        fetched = self.cli("get", page, "USER-01", "--collection", "primary")
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(json.loads(fetched.stdout)["title"], "Contains ] bracket")

        patch = self.root / "html-patch.json"
        patch.write_text(json.dumps({"status": "in_progress"}), encoding="utf-8")
        updated = self.cli("update", page, "USER-01", patch, "--collection", "primary")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        text = page.read_text(encoding="utf-8")
        self.assertIn(f"const issues={json.dumps(issues)}", text)
        self.assertEqual(json.loads(text.split("const primary=", 1)[1].split(";", 1)[0])[0]["status"], "in_progress")

        added_entry = self.root / "html-entry.json"
        added_entry.write_text(json.dumps({"id": "USER-02", "title": "New issue"}), encoding="utf-8")
        added = self.cli("add", page, added_entry, "--collection", "primary")
        self.assertEqual(added.returncode, 0, added.stderr)
        deleted = self.cli("delete", page, "USER-01", "--collection", "primary")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        listing = self.cli("list", page, "--collection", "primary")
        self.assertEqual([row["id"] for row in json.loads(listing.stdout)], ["USER-02"])

    def test_get_hides_inline_images_unless_explicitly_requested(self):
        page = self.root / "evidence.html"
        finding = [{"id": "USER-01", "screenshots": [
            {"src": "data:image/png;base64,ZmFrZQ==", "alt": "crop"}
        ]}]
        page.write_text(f"<script>const primary={json.dumps(finding)};</script>", encoding="utf-8")

        compact = self.cli("get", page, "USER-01", "--collection", "primary")
        self.assertEqual(compact.returncode, 0, compact.stderr)
        self.assertNotIn("ZmFrZQ==", compact.stdout)
        self.assertIn("use --include-evidence", compact.stdout)

        full = self.cli("get", page, "USER-01", "--collection", "primary", "--include-evidence")
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn("data:image/png;base64,ZmFrZQ==", full.stdout)


    def _replay_batch(self, document, *items, force=False):
        self.data.write_text(json.dumps(document), encoding="utf-8")
        page, ops = self.root / "replay.html", self.root / "replay.json"
        ops.write_text(json.dumps(list(items)))
        args = ["batch", self.data, ops, "--render", page]
        if force:
            args.append("--force")
        return self.cli(*args)

    def test_install_readiness_is_release_check_for_named_lanes(self):
        document = copy.deepcopy(self.document)
        document["findings"] = document["findings"][:2]
        for index, finding in enumerate(document["findings"], 1):
            finding.update(id=f"ISSUE-0{index}", status="in_progress")
        ready = {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review", "phase": "review",
                 "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                           "fix_status": "passed", "review_status": "passed"}, "presuite": {"status": "passed"}}
        fixing = {"id": "B", "finding_ids": ["ISSUE-02"], "depends_on": [], "status": "running", "phase": "fix",
                  "codex": {"implementer_session": "writer", "fix_status": "running"}}
        document.setdefault("tracking", {}).update(cycle=1, round=1, stages_reached=["collect", "present", "plan_fix", "review"],
                                                   plan={"cycle": 1, "round": 1, "groups": [ready, fixing]})
        install = {"action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Installing"}
        refused = self._replay_batch(document, install)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("install requires a clear bcl release-check", refused.stderr)
        self.assertIn("not ready: B", refused.stderr)
        fixing.update(status="deferred", phase="deferred")
        allowed = self._replay_batch(document, install)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        ready["presuite"] = {"status": "failed"}
        self.assertIn("not ready: A", self._replay_batch(document, install).stderr)
    def test_add_refuses_near_duplicate_unless_new_or_into(self):
        document = copy.deepcopy(self.document)
        original = document["findings"][0]
        original["status"] = "untested"
        twin = {key: original[key] for key in ("title", "summary", "impact", "recommendation", "done_when",
                                              "priority", "source", "observed") if key in original}
        twin.update(id="ISSUE-99", status="untested")
        refused = self._replay_batch(document, {"action": "add", "entry": twin})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(f"looks like open {original['id']}", refused.stderr)
        added = self._replay_batch(document, {"action": "add", "entry": twin, "new": True})
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("ISSUE-99", [item["id"] for item in json.loads(self.data.read_text())["findings"]])
        count = len(document["findings"])
        appended = self._replay_batch(document, {"action": "add", "entry": twin, "into": original["id"]})
        self.assertEqual(appended.returncode, 0, appended.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual(len(saved["findings"]), count)
        target = next(item for item in saved["findings"] if item["id"] == original["id"])
        self.assertIn(original["title"], target["details"]["also reported as ISSUE-99"])

    def test_add_accepts_unrelated_entry_and_ignores_closed_twins(self):
        document = copy.deepcopy(self.document)
        original = document["findings"][0]
        twin = {key: original[key] for key in ("title", "summary", "impact", "recommendation", "done_when",
                                              "priority", "source") if key in original}
        twin.update(id="ISSUE-98", status="untested")
        original["status"] = "verified_fixed"
        self.assertEqual(manage_findings.near_duplicates([original], twin), [])
        unrelated = {"id": "ISSUE-97", "title": "Voice input drops the first syllable",
                     "summary": "Microphone capture starts late after wake.", "recommendation": "Prime the audio buffer."}
        self.assertEqual(manage_findings.near_duplicates(document["findings"][1:], unrelated), [])

    def _blocked_lane(self):
        document = copy.deepcopy(self.document)
        document["findings"] = document["findings"][:2]
        for index, finding in enumerate(document["findings"], 1):
            finding.update(id=f"ISSUE-0{index}", status="blocked")
        failures = [{"attempt_id": "attempt-1", "source": "review", "reason": "Review incomplete"},
                    {"attempt_id": "attempt-2", "source": "review", "reason": "Changes requested"}]
        document["tracking"] = {"cycle": 1, "round": 1, "plan": {"cycle": 1, "round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01", "ISSUE-02"], "status": "blocked", "phase": "fix",
            "blocked_reason": "Second failure", "failure_block_reason": "review failed",
            "codex": {"review_status": "blocked", "fix_status": "failed"},
            "failure_history": {"ISSUE-01": copy.deepcopy(failures), "ISSUE-02": copy.deepcopy(failures)},
        }]}}
        return document

    def test_unblock_requires_reason_and_preserves_tracker_bytes(self):
        document = self._blocked_lane()
        before = json.dumps(document).encode()
        result = self._replay_batch(document, {"action": "unblock-lane", "ids": ["ISSUE-01"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unblock override requires a one-line nonblank --reason", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_unblock_tool_fault_is_excluded_and_plan_review_is_still_required(self):
        document = self._blocked_lane()
        result = self._replay_batch(document, {"action": "unblock-lane", "ids": ["ISSUE-01"],
            "reason": "0.1.52 misparsed a valid PASS", "tool_faults": ["attempt-2"],
            "timestamp": "2026-11-21T12:00:00Z"})
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.data.read_bytes())
        group = document["tracking"]["plan"]["groups"][0]
        self.assertEqual((group["status"], group["phase"]), ("running", "fix"))
        self.assertNotIn("blocked_reason", group)
        self.assertNotIn("failure_block_reason", group)
        self.assertEqual([item["status"] for item in document["findings"]], ["in_progress", "in_progress"])
        self.assertEqual(len(group["failure_history"]), 2)
        self.assertTrue(group["failure_history"][1]["tool_fault"])
        self.assertEqual(group["failure_history"][1]["tool_fault_reason"],
                         "0.1.52 misparsed a valid PASS")
        self.assertEqual(group["unblock_history"][0]["tool_faults"], ["attempt-2"])
        self.assertEqual(len(manage_findings._counted_failures(group["failure_history"])), 1)
        with self.assertRaisesRegex(ValueError, "passed plan_review"):
            manage_findings.job_transition(document, {"action": "start-job", "phase": "fix", "ids": ["ISSUE-01"],
                "cycle": 1, "round": 1, "timestamp": "2026-11-21T12:01:00Z",
                "job": {"id": "fix-early", "plan_sha256": manage_findings.plan_candidate_sha256(document["tracking"]["plan"])}})
        manage_findings.job_transition(document, {"action": "start-job", "phase": "plan_review", "ids": ["ISSUE-01"],
            "cycle": 1, "round": 1, "timestamp": "2026-11-21T12:02:00Z",
            "job": {"id": "plan-review", "plan_sha256": manage_findings.plan_candidate_sha256(document["tracking"]["plan"])}})
        manage_findings.job_transition(document, {"action": "finish-job", "phase": "plan_review", "ids": ["ISSUE-01"],
            "cycle": 1, "round": 1, "timestamp": "2026-11-21T12:03:00Z", "job_id": "plan-review",
            "exit_code": 0, "plan_sha256": manage_findings.plan_candidate_sha256(document["tracking"]["plan"]),
            "reply": "VERDICT: PASS\n## Must-fix\nnone\n## ROOT CAUSE\nparser fault\n## FAILING TEST\ntest_parse\n"})
        self.assertEqual(group["plan_review_history"][-1]["after_failure"], "attempt-1")
        manage_findings.job_transition(document, {"action": "start-job", "phase": "fix", "ids": ["ISSUE-01"],
            "cycle": 1, "round": 1, "timestamp": "2026-11-21T12:04:00Z",
            "job": {"id": "fix-after-review", "plan_sha256": manage_findings.plan_candidate_sha256(document["tracking"]["plan"])}})

    def test_unblock_refuses_nonblocked_lane_and_unknown_attempt_without_writing(self):
        for blocked, fault in ((False, "attempt-1"), (True, "missing")):
            with self.subTest(blocked=blocked, fault=fault):
                document = self._blocked_lane()
                if not blocked:
                    group = document["tracking"]["plan"]["groups"][0]
                    group["status"] = "running"
                    group["failure_history"] = [{"attempt_id": "attempt-1", "source": "review"}]
                before = json.dumps(document).encode()
                result = self._replay_batch(document, {"action": "unblock-lane", "ids": ["ISSUE-01"],
                    "reason": "owner reviewed the blocked lane", "tool_faults": [fault],
                    "timestamp": "2026-11-21T12:00:00Z"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.data.read_bytes(), before)
                self.assertIn("blocked lane" if not blocked else "unknown attempt ID", result.stderr)

    def test_unblock_without_tool_fault_records_owner_override(self):
        document = self._blocked_lane()
        result = self._replay_batch(document, {"action": "unblock-lane", "ids": ["ISSUE-01"],
            "reason": "Owner override: proceed after reviewing both failures.",
            "timestamp": "2026-11-21T12:00:00Z"})
        self.assertEqual(result.returncode, 0, result.stderr)
        history = json.loads(self.data.read_bytes())["tracking"]["plan"]["groups"][0]["unblock_history"]
        self.assertTrue(history[-1]["owner_override"])

    def test_unblock_tool_fault_cannot_replace_owner_override_for_two_remaining_failures(self):
        document = self._blocked_lane()
        document["tracking"]["plan"]["groups"][0]["failure_history"]["ISSUE-01"].append(
            {"attempt_id": "attempt-3", "source": "review", "reason": "third failure"})
        before = json.dumps(document).encode()
        result = self._replay_batch(document, {"action": "unblock-lane", "ids": ["ISSUE-01"],
            "reason": "mark parser attempt", "tool_faults": ["attempt-1"],
            "timestamp": "2026-11-21T12:00:00Z"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner override requires no --tool-fault", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def _path1_pre(self):
        return {
            "title": "Product issues",
            "findings": [
                {
                    "id": "ISSUE-01",
                    "title": "The selected item does not open",
                    "source": "User reported",
                    "status": "in_progress",
                    "summary": "Selecting the item does not open its details.",
                    "impact": "Readers cannot review the selected item's information.",
                    "done_when": "Selecting the item opens its details in the installed view.",
                    "observed": "The user reported that selecting the item had no effect.",
                    "recommendation": "Restore the selection action and verify it on the installed build.",
                    "details": {
                        "Expected": "Selecting the item opens its details.",
                        "Verification steps": "Select the item and check its details open.",
                        "Build and time": "Unknown",
                        "Provenance": "User report",
                    },
                    "evidence": [],
                },
                {
                    "id": "ISSUE-02",
                    "title": "Icon appears too small",
                    "source": "Agent reported",
                    "status": "verified_fixed",
                    "observed": "The previous icon was too small.",
                    "recommendation": "Keep the updated icon framing.",
                    "verification": "The installed build displayed the corrected icon.",
                    "verification_refs": ["Example live capture, build 1.2.3"],
                    "details": {"Build and time": "Example build 1.2.3"},
                    "evidence": [],
                },
            ],
            "tracking": {
                "rollover_threshold": 2,
                "cycle": 1,
                "round": 1,
                "workflow": {
                    "stage": "verify",
                    "updated_at": "2026-11-17T04:36:28Z",
                    "now": "2026-11-17T04:36:28Z — Verified icon",
                },
                "stages_reached": ["collect", "present", "plan_fix", "install", "verify"],
                "pull_request": {"label": "PR #111", "url": "https://example.test/pull/111"},
                "plan": {
                    "round": 1,
                    "cycle": 1,
                    "groups": [{
                        "id": "A",
                        "name": "Group A",
                        "finding_ids": ["ISSUE-01"],
                        "depends_on": [],
                        "status": "pending",
                        "worktree": "fix-a",
                        "codex": {"implementer_session": "writer", "reviewer_session": "reviewer"},
                        "verify": {
                            "command": "true",
                            "summary": "placeholder check",
                            "status": "pending",
                            "fixed": 0,
                            "failed": 0,
                            "total": 1,
                        },
                    }],
                },
            },
        }

    def _path2_pre(self):
        return {
            "title": "Product issues",
            "findings": [
                {
                    "id": "ISSUE-01",
                    "title": "The selected item does not open",
                    "source": "User reported",
                    "status": "verified_fixed",
                    "observed": "The user reported that selecting the item had no effect.",
                    "recommendation": "Restore the selection action and verify it on the installed build.",
                    "verification": "Selecting the item opened its details.",
                    "verification_refs": ["Example live capture, build 1.2.3"],
                    "details": {"Build and time": "Example build 1.2.3"},
                    "evidence": [],
                },
                {
                    "id": "ISSUE-02",
                    "title": "Icon appears too small",
                    "source": "Agent reported",
                    "status": "verified_fixed",
                    "observed": "The previous icon was too small.",
                    "recommendation": "Keep the updated icon framing.",
                    "verification": "The installed build displayed the corrected icon.",
                    "verification_refs": ["Example live capture, build 1.2.3"],
                    "details": {"Build and time": "Example build 1.2.3"},
                    "evidence": [],
                },
                {
                    "id": "WF-01",
                    "title": "Introspect 2026-11-17: round 1 introspect",
                    "source": "Agent reported",
                    "status": "verified_fixed",
                    "observed": "round 1 introspect",
                    "recommendation": "None; all items were fixed inline or none.",
                    "verification": "Clean cycle; every section is none with a reason.",
                    "details": {"fixed_in": "Introspect"},
                    "verification_refs": ["Introspect"],
                    "evidence": [],
                },
            ],
            "tracking": {
                "rollover_threshold": 2,
                "cycle": 1,
                "round": 2,
                "workflow": {
                    "stage": "verify",
                    "updated_at": "2026-11-17T04:36:28Z",
                    "now": "2026-11-17T04:36:28Z — Verified round 2",
                },
                "stages_reached": ["collect", "present", "plan_fix", "install", "verify"],
                "pull_request": {"label": "PR #111", "url": "https://example.test/pull/111"},
                "introspect": {
                    "result": "round 1 introspect",
                    "at": "2026-11-17T04:36:28Z",
                    "task": "WF-01",
                    "round": 1,
                    "cycle": 1,
                },
                "plan": {
                    "round": 2,
                    "cycle": 1,
                    "status": "closed",
                    "groups": [{
                        "id": "A",
                        "name": "Group A",
                        "finding_ids": ["ISSUE-01"],
                        "depends_on": [],
                        "status": "verified",
                        "worktree": "fix-a",
                        "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                                  "review_status": "passed"},
                    }],
                },
                "round_history": [{"round": 1, "fixed": 1, "ended_at": "2026-11-17T04:36:28Z"}],
            },
        }

    def test_statechart_path1_refuses_planning_next_round_without_this_rounds_introspect(self):
        # Mini /tmp/bcl-statechart-replay/path1-skip-introspect-then-plan: on 0.1.22,
        # set-activity plan_fix after Install/Verify with no Introspect advanced the round.
        document = self._path1_pre()
        self.data.write_text(json.dumps(document), encoding="utf-8")
        original = self.data.read_bytes()
        result = self._replay_batch(json.loads(original), {
            "action": "set-activity",
            "stage": "plan_fix",
            "ids": ["ISSUE-01"],
            "now": "Plan round 2 without introspect",
        })
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("this round's introspect", result.stderr)
        self.assertIn("returning to plan_fix", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        tracking = json.loads(original)["tracking"]
        self.assertEqual(tracking["round"], 1)
        self.assertEqual(tracking["workflow"]["stage"], "verify")
        self.assertIsNone(tracking.get("introspect"))

    def test_set_plan_requires_this_rounds_introspect_when_returning_from_verify(self):
        document = self._path1_pre()
        self.data.write_text(json.dumps(document), encoding="utf-8")
        original = self.data.read_bytes()
        plan = {"action": "set-plan", "plan": {"round": 2, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "pending",
        }]}}
        result = self._replay_batch(json.loads(original), plan)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("this round's introspect", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        current = json.loads(original)
        current["findings"][0]["status"] = "failing"
        current["tracking"]["plan"]["groups"][0].update(status="failed", phase="verify")
        current["tracking"]["plan"]["groups"][0]["verify"].update(status="failed", failed=1, total=1)
        workflow_before_edit = current["tracking"]["workflow"].copy()
        result = self._replay_batch(current, {"action": "set-plan", "plan": current["tracking"]["plan"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())["tracking"]["workflow"], workflow_before_edit)
        before_next_plan = self.data.read_bytes()
        next_plan_ops = self.root / "next-plan.json"
        next_plan_ops.write_text(json.dumps([{
            "action": "set-activity", "stage": "plan_fix", "ids": ["ISSUE-01"],
            "now": "Try next plan without introspect",
        }]))
        result = self.cli("batch", self.data, next_plan_ops, "--render", self.root / "next-plan.html")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("this round's introspect", result.stderr)
        self.assertEqual(self.data.read_bytes(), before_next_plan)
        none = {key: {"none": f"No {key} changes."} for key in ("delete", "simplify", "optimize", "automate")}
        result = self._replay_batch(current, {"action": "introspect", "sections": none}, plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())["tracking"]["introspect"]["round"], 1)
        self.assertEqual(json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]["id"], "A")
        present = json.loads(original)
        present["tracking"]["workflow"]["stage"] = "present"
        present["tracking"]["stages_reached"] = ["collect", "present"]
        result = self._replay_batch(present, {"action": "set-plan", "plan": {
            "round": 1, "groups": [{"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "pending"}],
        }})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_introspect_cannot_authorize_plan_or_report_before_verify(self):
        document = self._path1_pre()
        document["tracking"]["stages_reached"].remove("verify")
        self.data.write_text(json.dumps(document), encoding="utf-8")
        original = self.data.read_bytes()
        none = {key: {"none": f"No {key} changes."} for key in ("delete", "simplify", "optimize", "automate")}
        result = self._replay_batch(json.loads(original), {"action": "introspect", "sections": none})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("requires verify", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        premature_plan = {"action": "set-plan", "plan": {"round": 2, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "pending",
        }]}}
        result = self._replay_batch(json.loads(original), {"action": "set-stage", "stage": "report_rollover"})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("this round's introspect", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        document["tracking"]["stages_reached"].append("verify")
        result = self._replay_batch(document, {"action": "introspect", "sections": none}, premature_plan)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot start round 2: round 1 has open lanes", result.stderr)
        self.assertEqual(json.loads(self.data.read_text())["tracking"]["round"], 1)

    def test_verify_stage_guards_follow_the_declared_prerequisite(self):
        prerequisite = workflow_definition.TRANSITIONS["verify"][0]
        for action in ("set-stage", "set-activity"):
            with self.subTest(action=action):
                document = self._path1_pre()
                document["tracking"]["stages_reached"].remove(prerequisite)
                original = json.dumps(document).encode()
                operation = {"action": action, "stage": "verify"}
                if action == "set-activity":
                    operation.update(ids=["ISSUE-01"], now="Verify")
                result = self._replay_batch(document, operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"verify requires {prerequisite}", result.stderr)
                self.assertEqual(self.data.read_bytes(), original)

    def test_statechart_path2_refuses_report_rollover_on_earlier_rounds_introspect(self):
        # Mini /tmp/bcl-statechart-replay/path2-stale-introspect-then-report: on 0.1.22,
        # set-stage report_rollover succeeded because an earlier round's record was present.
        document = self._path2_pre()
        self.data.write_text(json.dumps(document), encoding="utf-8")
        original = self.data.read_bytes()
        result = self._replay_batch(json.loads(original), {"action": "set-stage", "stage": "report_rollover"})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("this round's introspect", result.stderr)
        self.assertIn("report_rollover", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        tracking = json.loads(original)["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["introspect"]["round"], 1)
        self.assertEqual(tracking["workflow"]["stage"], "verify")

    def test_this_rounds_introspect_allows_next_plan_and_report(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        completed_round = self._path1_pre()
        completed_round["tracking"]["plan"]["groups"][0].update(status="blocked", blocked_reason="test lane complete")
        next_plan = copy.deepcopy(completed_round["tracking"]["plan"])
        next_plan["round"] = 2
        next_plan["groups"][0].update(status="pending")
        path1 = self._replay_batch(completed_round,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": next_plan})
        self.assertEqual(path1.returncode, 0, path1.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["workflow"]["stage"], "plan_fix")
        self.assertEqual(tracking["plan"]["groups"][0]["status"], "blocked")
        self.assertEqual(tracking["introspect"]["round"], 1)
        self.assertEqual(tracking["introspect"]["cycle"], 1)
        path2 = self._replay_batch(self._path2_pre(), {"action": "introspect", "sections": none, "result": "round 2 done"}, {
            "action": "set-stage", "stage": "report_rollover",
        })
        self.assertEqual(path2.returncode, 0, path2.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["workflow"]["stage"], "report_rollover")
        self.assertEqual(tracking["introspect"]["round"], 2)
        self.assertEqual(tracking["introspect"]["cycle"], 1)
        self.assertEqual(tracking["introspect"]["result"], "round 2 done")

    def test_new_plan_after_completed_round_advances_once(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        document = self._path1_pre()
        document["tracking"]["plan"]["groups"][0].update(status="blocked", blocked_reason="test lane complete")
        next_plan = copy.deepcopy(document["tracking"]["plan"])
        next_plan["round"] = 2
        next_plan["groups"][0].update(status="pending", phase="fix")
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": next_plan})
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["plan"]["round"], 2)
        self.assertEqual(tracking["plan"]["groups"][0]["status"], "blocked")
        self.assertEqual(tracking["round_history"][0]["round"], 1)
        self.assertEqual(len(tracking["round_history"]), 1)

    def test_new_round_keeps_requested_status_for_gated_lane(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        document = self._path1_pre()
        for number in (3, 4):
            finding = copy.deepcopy(document["findings"][0])
            finding.update(id=f"ISSUE-0{number}", status="in_progress")
            document["findings"].append(finding)
        plan = document["tracking"]["plan"]
        plan["groups"] = [
            {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "deferred",
             "codex": {"review_status": "changes_requested"},
             "failure_history": [{"attempt_id": "failed-a", "source": "review", "reason": "Changes requested"}]},
            {"id": "B", "finding_ids": ["ISSUE-02"], "depends_on": [], "status": "verified"},
        ]
        next_plan = {"round": 2, "groups": [
            {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "deferred"},
            {"id": "C", "finding_ids": ["ISSUE-03"], "depends_on": [], "status": "pending"},
            {"id": "D", "finding_ids": ["ISSUE-04"], "depends_on": [], "status": "pending"},
        ]}
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": next_plan})
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())["tracking"]["plan"]
        lane_a = next(group for group in saved["groups"] if group["id"] == "A")
        self.assertEqual(lane_a["status"], "deferred")
        self.assertEqual(lane_a["parked_to"], 3)
        self.assertEqual(len(manage_findings._counted_failures(lane_a["failure_history"])), 1)

        pending_plan = copy.deepcopy(next_plan)
        pending_plan["groups"][0]["status"] = "pending"
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": pending_plan})
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())["tracking"]["plan"]
        lane_a = next(group for group in saved["groups"] if group["id"] == "A")
        self.assertEqual(lane_a["status"], "planned")
        self.assertEqual(len(manage_findings._counted_failures(lane_a["failure_history"])), 1)

        dropped_plan = {"round": 2, "groups": [group for group in next_plan["groups"] if group["id"] != "A"]}
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": dropped_plan})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot change gated lane A", result.stderr)

    def test_set_plan_rejects_round_mismatch_after_introspect(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        document = self._path1_pre()
        document["tracking"]["plan"]["groups"][0].update(status="blocked", blocked_reason="test lane complete")
        current_round_plan = copy.deepcopy(document["tracking"]["plan"])
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": current_round_plan})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("set-plan plan.round must be 2; got 1", result.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 1)
        self.assertEqual(tracking["plan"]["round"], 1)
        self.assertEqual(tracking["workflow"]["stage"], "verify")
        self.assertNotIn("introspect", tracking)

    def test_failed_lane_counts_as_closed_when_next_round_plan_is_recorded(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        document = self._path1_pre()
        document["tracking"]["plan"]["groups"][0].update(status="failed", phase="verify")
        document["findings"][0]["status"] = "failing"
        next_plan = copy.deepcopy(document["tracking"]["plan"])
        next_plan["round"] = 2
        next_plan["groups"][0].update(status="planned", phase="fix")
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": next_plan})
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["plan"]["round"], 2)
        self.assertEqual(tracking["round_history"][0]["round"], 1)

    def test_set_plan_refuses_next_round_while_lanes_are_open(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        document = self._path1_pre()
        next_plan = copy.deepcopy(document["tracking"]["plan"])
        next_plan["round"] = 2
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 reviewed"},
            {"action": "set-plan", "plan": next_plan})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot start round 2: round 1 has open lanes: A (planned)", result.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 1)
        self.assertEqual(tracking["plan"]["round"], 1)

    def test_review_requires_implementer_session(self):
        with self.assertRaisesRegex(ValueError, "implementer_session"):
            check_install_sessions.validate_review_sessions({"groups": [{
                "id": "A", "finding_ids": ["ISSUE-01"], "codex": {}}]}, ["ISSUE-01"])

    def test_install_requires_a_passing_recorded_review(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        document.setdefault("tracking", {})["stages_reached"] = ["plan_fix", "review"]
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
            "codex": {"implementer_session": "writer", "reviewer_session": "reviewer"},
        }]}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install requires a clear bcl release-check", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_job_receipt_cannot_be_patched_directly(self):
        document = copy.deepcopy(self.document)
        document.setdefault("tracking", {})["plan"] = {"round": 1, "cycle": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "status": "review", "phase": "review",
            "codex": {"implementer_session": "writer", "reviewer_session": "new-reviewer"}}]}
        update = {"action": "update-plan-group", "id": "A", "merge_codex": True,
                  "patch": {"codex": {"job": {"pid": 42, "phase": "review", "exit_code": 0}}}}
        result = self._replay_batch(document, update)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only change through bcl job", result.stderr)

    def test_set_plan_cannot_claim_one_file_review_skip(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        document.setdefault("tracking", {})["stages_reached"] = ["plan_fix", "review"]
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
            "phase": "review", "codex": {"implementer_session": "writer", "one_file": True, "review_status": "skipped_one_file"}, "presuite": {"status": "passed"},
        }]}}
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot claim completed job state", result.stderr)

    def test_set_plan_cannot_claim_review_pass_for_one_lane(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        document["findings"][1]["status"] = "untested"
        document.setdefault("tracking", {})["stages_reached"] = ["plan_fix", "review"]
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [
            {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
             "phase": "review", "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                                             "review_status": "passed"}, "presuite": {"status": "passed"}},
            {"id": "B", "finding_ids": ["ISSUE-02"], "depends_on": [], "status": "planned", "phase": "fix"},
        ]}}
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install A",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot claim completed job state", result.stderr)

    def test_set_stage_install_checks_every_active_group(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        document.setdefault("tracking", {})["stages_reached"] = ["plan_fix", "review"]
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review", "phase": "review",
        }]}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, plan, {"action": "set-stage", "stage": "install"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install requires a clear bcl release-check", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_job_review_statuses_allow_defer_but_not_install_or_unknown_state(self):
        for action in ("update-plan-group", "set-plan"):
            with self.subTest(action=action):
                document = copy.deepcopy(self.document)
                document["findings"] = document["findings"][:1]
                group = {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [],
                         "status": "review", "phase": "review",
                         "codex": {"review_status": "changes_requested", "reviewer_session": "reviewer"},
                         "failure_history": [{"attempt": 1, "status": "failed"}]}
                tracking = document.setdefault("tracking", {})
                tracking.update(cycle=1, round=1, stages_reached=["plan_fix", "review"])
                if action == "update-plan-group":
                    tracking["plan"] = {"cycle": 1, "round": 1, "groups": [copy.deepcopy(group)]}
                    operation = {"action": action, "id": "A", "patch": {"status": "deferred", "phase": "deferred"}}
                else:
                    group.update(status="deferred", phase="deferred")
                    tracking["plan"] = {"cycle": 1, "round": 1, "groups": [copy.deepcopy(group)]}
                    operation = {"action": action, "plan": {"round": 1, "groups": [group]}}
                result = self._replay_batch(document, operation)
                self.assertEqual(result.returncode, 0, result.stderr)
                saved = json.loads(self.data.read_text())
                saved_group = saved["tracking"]["plan"]["groups"][0]
                self.assertEqual((saved_group["status"], saved_group["phase"]), ("deferred", "deferred"))
                self.assertEqual(saved_group["codex"]["review_status"], "changes_requested")
                self.assertEqual(saved_group["failure_history"], [{"attempt": 1, "status": "failed"}])
                self.assertEqual(manage_findings.release_check_payload(saved)["lanes"], [])
                ready = manage_findings.release_check_payload(saved, ["ISSUE-01"], require_presuite=True)
                self.assertEqual(ready.get("included"), [])

        document = copy.deepcopy(self.document)
        with self.assertRaisesRegex(ValueError, "codex.review_status must be one of"):
            check_install_sessions.validate_group_codex({"id": "A", "codex": {"review_status": "complete"}})

    def test_plan_rejects_invalid_one_file_and_empty_session_fields(self):
        for codex, message in (({"one_file": 1}, "one_file"),
                               ({"implementer_session": " "}, "implementer_session")):
            with self.subTest(codex=codex):
                plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
                    "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "planned", "codex": codex,
                }]}}
                before = self.data.read_bytes()
                result = self._replay_batch(json.loads(before), plan)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.data.read_bytes(), before)

    def test_introspect_rejects_uncovered_steps_and_out_of_order_sections(self):
        document = self._path1_pre()
        recorded = check_introspect.performed_round_steps(document["tracking"])
        sections = {name: {"none": f"No {name} changes."}
                    for name in ("delete", "simplify", "optimize", "automate")}
        original = json.dumps(document).encode()
        result = self._replay_batch(document, {"action": "introspect", "sections": sections,
                                                "steps": ["Stage: Verify"], "none": ["Stage: Verify"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Parallel group count", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)
        shuffled = {name: sections[name] for name in ("simplify", "delete", "optimize", "automate")}
        result = self._replay_batch(document, {"action": "introspect", "sections": shuffled,
                                                "steps": recorded, "none": recorded})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("order", result.stderr)
        self.assertEqual(self.data.read_bytes(), original)

    def test_introspect_records_complete_orchestration_coverage(self):
        document = self._path1_pre()
        before = len(document['findings'])
        steps = [*check_introspect.performed_round_steps(document["tracking"]), "polling", "rework", "focused tests"]
        sections = {
            "delete": {"items": [{"item": steps[0], "evidence": "Fix diff recorded.", "outcome": "fixed inline", "proof": "python3 test_workflow.py"}]},
            "simplify": {"none": "No simplification warranted."},
            "optimize": {"none": "No optimization warranted."},
            "automate": {"none": "No additional automation warranted."},
        }
        none = steps[1:]
        result = self._replay_batch(document, {"action": "introspect", "sections": sections,
                                                "steps": steps, "none": none})
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        record = saved["tracking"]["introspect_history"][-1]
        self.assertEqual(len(saved['findings']), before)
        self.assertEqual(record["steps"], steps)
        self.assertIn("Parallel group count: 1", record["steps"])
        self.assertIn("Codex write: A", record["steps"])
        self.assertIn("Codex review: A", record["steps"])
        self.assertEqual(record["none"], none)

    def test_merged_stage_uses_the_declared_report_transition(self):
        document = copy.deepcopy(self.document)
        document["tracking"] = {"stages_reached": [], "workflow": {
            "stage": "verify", "updated_at": "2026-11-17T00:00:00Z"}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, {"action": "set-stage", "stage": "merged", "merged": {
            "commit_sha": "a" * 40, "merged_at": "2026-11-17T00:00:00Z",
            "stacked_prs": {"status": "complete"},
        }})
        self.assertNotEqual(result.returncode, 0)
        required = workflow_definition.TRANSITIONS["merged"][0]
        self.assertIn(f"merged requires {required}", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_merging_blocks_15_verified_fixes_and_accepts_16(self):
        def merge(count):
            document = copy.deepcopy(self.document)
            template = document["findings"][0]
            document["findings"] = []
            for i in range(count):
                finding = copy.deepcopy(template)
                finding.update(id=f"ISSUE-{i + 1:02}", status="verified_fixed", verification="Verified", verification_refs=["receipt"])
                document["findings"].append(finding)
            document["tracking"] = {"stages_reached": ["report_rollover"]}
            return self._replay_batch(document, {"action": "set-stage", "stage": "merged", "merged": {
                "commit_sha": "a" * 40, "merged_at": "2026-11-17T00:00:00Z",
                "stacked_prs": {"status": "complete"},
            }})
        blocked = merge(15)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("merge requires 16 verified_fixed findings; count is 15", blocked.stderr)
        self.assertEqual(merge(16).returncode, 0)

    def test_completed_check_pass_clears_checks_pending_block(self):
        document = json.loads(self.data.read_text())
        document["findings"][0]["status"] = "verified_fixed"
        document["findings"][0]["verification"] = "Checked on the installed build."
        document["findings"][0]["verification_refs"] = ["receipt"]
        document.setdefault("tracking", {})["stages_reached"] = ["collect", "present", "plan_fix", "install", "verify"]
        document["tracking"]["plan"] = {
            "round": 1,
            "cycle": 1,
            "groups": [{
                "id": "CONTROL",
                "finding_ids": ["ISSUE-01"],
                "depends_on": [],
                "status": "blocked",
                "blocked_reason": "Individual issue checks pending",
            }],
        }
        document["tracking"]["plan"]["groups"].append({
            "id": "UNRELATED", "finding_ids": ["ISSUE-01"], "depends_on": [],
            "status": "blocked", "blocked_reason": "Waiting for an external dependency",
        })
        self.data.write_text(json.dumps(document), encoding="utf-8")
        page, ops = self.root / "checks.html", self.root / "checks.json"
        ops.write_text(json.dumps([{"action": "set-stage", "stage": "verify"}]))
        # ISSUE-01 is verified_fixed, so set-stage verify may fail for in_progress; use a no-op batch touch via update
        ops.write_text(json.dumps([{
            "action": "update", "id": "ISSUE-01",
            "patch": {"status": "verified_fixed", "verification": "Checked on the installed build.", "verification_refs": ["receipt"]},
        }]))
        result = self.cli("batch", self.data, ops, "--render", page)
        self.assertEqual(result.returncode, 0, result.stderr)
        group = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][0]
        self.assertEqual(group["status"], "verified")
        self.assertNotIn("blocked_reason", group)
        unrelated = json.loads(self.data.read_text())["tracking"]["plan"]["groups"][1]
        self.assertEqual(unrelated["status"], "blocked")
        self.assertEqual(unrelated["blocked_reason"], "Waiting for an external dependency")
        # Re-render a still-pending checks lane to confirm the page does not say Blocked
        pending = json.loads(self.data.read_text())
        pending["findings"][1]["status"] = "untested"
        pending["tracking"]["plan"]["groups"].append({
            "id": "DISPLAY",
            "finding_ids": ["ISSUE-02"],
            "depends_on": [],
            "status": "blocked",
            "blocked_reason": "Remaining issue checks pending",
        })
        self.data.write_text(json.dumps(pending), encoding="utf-8")
        # Trigger sync + render without mutating statuses unexpectedly: compact or batch empty? use list via render through a harmless update of ISSUE-02 observed
        ops.write_text(json.dumps([{
            "action": "update", "id": "ISSUE-02",
            "patch": {"observed": pending["findings"][1].get("observed") or "The previous icon was too small."},
        }]))
        result = self.cli("batch", self.data, ops, "--render", page)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        display = next(g for g in doc["tracking"]["plan"]["groups"] if g["id"] == "DISPLAY")
        self.assertEqual(display["status"], "verifying")
        html = page.read_text()
        self.assertIn("Checks pending", html)
        self.assertNotIn("Blocked · Remaining issue checks pending", html)



if __name__ == "__main__":
    unittest.main()
