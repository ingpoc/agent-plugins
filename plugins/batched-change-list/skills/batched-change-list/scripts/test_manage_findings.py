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

try:
    import fcntl
except ImportError:
    fcntl = None

SCRIPT = Path(__file__).with_name("manage_findings.py")
EXAMPLE = Path(__file__).parents[1] / "examples/findings.json"
RENDERER = Path(__file__).with_name("render.py")


class ManageFindingsTests(unittest.TestCase):
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

    def test_reopened_prerequisite_blocks_running_dependent(self):
        ops, page = self.root / 'reopen.json', self.root / 'reopen.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        proof = {'status':'verified_fixed','verification':'Checked on installed build','verification_refs':['receipt']}
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'verified'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'},
                {'id':'C','finding_ids':['ISSUE-03'],'depends_on':['A'],'status':'verified'}]}
        entry = {'id':'ISSUE-03','title':'Dependent issue','source':'Agent reported','status':'untested',
                 'observed':'An issue exists.','recommendation':'Investigate.'}
        self.assertEqual(apply({'action':'add','entry':entry}, {'action':'update','id':'ISSUE-01','patch':proof},
                               {'action':'update','id':'ISSUE-03','patch':proof}, {'action':'set-plan','plan':plan}).returncode, 0)
        result = apply({'action':'update','id':'ISSUE-01','patch':{'status':'failing'}})
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.data.read_text())
        self.assertEqual(document['findings'][0]['status'], 'failing')
        groups = {group['id']:group for group in document['tracking']['plan']['groups']}
        self.assertEqual(groups['A']['status'], 'blocked')
        self.assertEqual(groups['B']['status'], 'blocked')
        self.assertEqual(groups['C']['status'], 'blocked')
        dependent = document['findings'][2]
        self.assertEqual(dependent['status'], 'blocked')
        self.assertEqual(dependent['verification_refs'], ['receipt'])
        self.assertEqual(groups['B']['blocked_reason'], 'Prerequisite verification changed')
        self.assertIn('Prerequisite verification changed', page.read_text())

    def test_current_recheck_invalidates_verified_prerequisite(self):
        ops, page = self.root / 'recheck-plan.json', self.root / 'recheck-plan.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        proof = {'status':'verified_fixed','verification':'Checked on installed build','verification_refs':['receipt']}
        plan = {'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'verified'},
                {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'running'}]}
        self.assertEqual(apply({'action':'update','id':'ISSUE-01','patch':proof}, {'action':'set-plan','plan':plan}).returncode, 0)
        run = {'id':'run-1','build':'ACU 1.2.3','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(apply({'action':'start-verification','run':run}).returncode, 0)
        result = apply({'action':'start-check','id':'ISSUE-01','run_id':'run-1',
                        'observation':'Observed again','observed_at':'2026-11-06T00:01:00Z'})
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.data.read_text())
        self.assertEqual(document['tracking']['plan']['groups'][1]['status'], 'blocked')

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

    def test_plan_check_caps_group_size_without_rejecting_effort(self):
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
            "groups": [{"id": "HARD", "finding_ids": ["A"], "status": "pending",
                        "codex": {"model": "gpt-6-luna", "effort": "max"}}],
        })
        manage_findings.validate_plan_groups({
            "round": 2,
            "groups": [{"id": "OLD", "finding_ids": ["A", "B", "C", "D", "E"], "status": "deferred",
                        "codex": {"effort": "max"}}],
        })

    def test_recheck_preserves_previous_result_and_other_checks(self):
        ops, page = self.root / 'ops.json', self.root / 'page.html'
        def apply(*rows):
            ops.write_text(json.dumps(rows))
            return self.cli('batch', self.data, ops, '--render', page)
        run = {'id':'install-1','build':'ACU','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        self.assertEqual(apply({'action':'start-verification','run':run}).returncode, 0)
        start = {'action':'start-check','id':'ISSUE-01','run_id':'install-1','observation':'initial visible state','observed_at':'2026-11-06T00:01:00Z'}
        self.assertEqual(apply(start).returncode, 0)
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'inconclusive','evidence':'first result'}).returncode, 0)
        before = json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-02']
        self.assertNotEqual(apply(start).returncode, 0)
        self.assertEqual(apply({**start, 'recheck':True, 'observation':'new visible state','observed_at':'2026-11-06T00:02:00Z'}).returncode, 0)
        current = json.loads(self.data.read_text())['tracking']['verification_run']['checks']
        self.assertEqual(current['ISSUE-01']['state'], 'checking')
        self.assertEqual(current['ISSUE-01']['previous_checks'][0]['evidence'], 'first result')
        self.assertEqual(current['ISSUE-02'], before)
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'not_fixed','evidence':'second result'}).returncode, 0)
        current = json.loads(self.data.read_text())['tracking']['verification_run']['checks']['ISSUE-01']
        self.assertEqual(current['evidence'], 'second result')
        self.assertEqual(current['previous_checks'][0]['evidence'], 'first result')
        start2 = {'action':'start-check','id':'ISSUE-02','run_id':'install-1','observation':'second issue visible state','observed_at':'2026-11-06T00:03:00Z'}
        self.assertEqual(apply(start2).returncode, 0)
        self.assertEqual(apply({'action':'finish-check','id':'ISSUE-02','run_id':'install-1','outcome':'inconclusive','evidence':'other'}).returncode, 0)
        self.assertEqual(apply({'action':'complete-verification','run_id':'install-1'}).returncode, 0)
        self.assertEqual(apply({**start, 'recheck':True, 'observation':'third visible state','observed_at':'2026-11-06T00:04:00Z'}).returncode, 0)
        run = json.loads(self.data.read_text())['tracking']['verification_run']
        self.assertEqual(run['status'], 'active')
        self.assertEqual(run['checks']['ISSUE-02']['evidence'], 'other')

    def test_each_phase_updates_the_timeline_and_can_be_cleared(self):
        ops, page = self.root / 'phase.json', self.root / 'report.html'
        for stage in ('collect', 'present', 'plan_fix', 'install', 'verify', 'introspect', 'gate', 'report_rollover', None):
            pre = [{'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'no issues'}] if stage == 'report_rollover' else []
            if stage in ('plan_fix', 'install', 'verify'):
                pre.insert(0, {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer','review_status':'passed'}}]}})
                if stage == 'verify':
                    pre.insert(1, {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'})
            if stage in ('plan_fix', 'install', 'verify'):
                pre.append({'action':'set-activity','stage':stage,'ids':['ISSUE-01'],'now':'Starting the checked stage'})
            ops.write_text(json.dumps(pre + [{'action':'set-stage','stage':stage}]))
            args = ['batch', self.data, ops, '--render', page]
            if stage == 'report_rollover':
                args.append('--force')
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(self.data.read_text())
            self.assertEqual(doc['tracking']['workflow']['stage'], stage)
            html = page.read_text()
            current_steps = 1 if stage in ('collect', 'present', 'report_rollover') else 0
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
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer','review_status':'passed'}}]}}
        result = run(plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Starting a fix cycle'})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data['findings'][0]['status'], 'in_progress')
        self.assertEqual(data['tracking']['workflow']['stage'], 'plan_fix')
        html = page.read_text()
        self.assertIn('class="fresh">Updated', html)
        self.assertNotIn('class="workflow-now"', html)
        self.assertIn('Plan &amp; fix', html)
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer','review_status':'passed'}}]}}
        self.assertEqual(run(plan).returncode, 0)
        self.assertEqual(run({'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'}).returncode, 0)
        before = self.data.read_bytes()
        self.assertNotEqual(run({'action':'set-stage','stage':'made-up'}).returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)
        self.assertEqual(run({'action':'set-stage','stage':None}).returncode, 0)
        self.assertNotIn('aria-current="step"', page.read_text())

    def test_fix_stages_reject_empty_activity_or_unstarted_tracker(self):
        for stage in ('plan_fix', 'install', 'verify'):
            for operation in (
                {'action':'set-stage','stage':stage},
                {'action':'set-activity','stage':stage,'ids':[],'now':'Starting work'},
            ):
                before = self.data.read_bytes()
                operations = self.root / 'guard.json'
                operations.write_text(json.dumps([operation]))
                result = self.cli('batch', self.data, operations, '--render', self.root / 'guard.html')
                self.assertNotEqual(result.returncode, 0, (stage, operation))
                self.assertTrue('in_progress' in result.stderr or 'recorded plan' in result.stderr or 'requires install' in result.stderr or 'requires a plan' in result.stderr)
                self.assertEqual(self.data.read_bytes(), before)
        operations = self.root / 'guard.json'
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer','review_status':'passed'}}]}}
        operations.write_text(json.dumps([plan, {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'}, {'action':'set-activity','stage':'verify','ids':['ISSUE-01'],'now':'Starting verification'}]))
        result = self.cli('batch', self.data, operations, '--render', self.root / 'guard.html')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())['findings'][0]['status'], 'in_progress')
        before = self.data.read_bytes()
        operations.write_text(json.dumps([{'action':'set-activity','stage':'install','ids':[],'now':'No selected finding'}]))
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

    def test_plan_fields_and_loopback_increment_round_history(self):
        ops, page = self.root / 'round.json', self.root / 'round.html'
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','name':'Plain group','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','worktree':'fix-a','codex':{'job_id':'job-1','status':'done','started_at':'2026-11-14T09:00:00Z','ended_at':'2026-11-14T09:05:00Z','implementer_session':'writer','reviewer_session':'reviewer','review_status':'passed'},'verify':{'summary':'Focused check passed','status':'pending'}}],'merge':{'status':'done','ref':'abc'}}}
        none = {'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}}
        ops.write_text(json.dumps([plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Plan'}, {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Install'}, {'action':'set-activity','stage':'verify','ids':['ISSUE-01'],'now':'Verify'}, {'action':'introspect','sections':none,'result':'round 1 done'}, {'action':'set-activity','stage':'gate','ids':[],'now':'Gate'}, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Repeat'}]))
        result = self.cli('batch', self.data, ops, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())['tracking']
        self.assertEqual(tracking['round'], 2)
        self.assertEqual(tracking['round_history'][0]['round'], 1)
        self.assertEqual(tracking['plan']['round'], 2)
        self.assertEqual(tracking['workflow']['stage'], 'plan_fix')
        self.assertIn('plan_fix', tracking['stages_reached'])
        self.assertNotIn('install', tracking['stages_reached'])
        self.assertNotIn('verify', tracking['stages_reached'])
        self.assertNotIn('introspect', tracking['stages_reached'])
        self.assertNotIn('gate', tracking['stages_reached'])
        group = tracking['plan']['groups'][0]
        self.assertEqual((group['name'], group['worktree'], group['codex']['job_id'], group['codex']['duration_s']), ('Plain group','fix-a','job-1',300))

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
        forced = self.cli('batch', self.data, operations, '--render', page, '--force')
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
                args.append('--force')
            return self.cli(*args)
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','codex':{'implementer_session':'writer','reviewer_session':'reviewer','review_status':'passed'}}]}}
        install = {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'}
        result = batch(plan, install, {'action':'set-activity','stage':'verify','ids':['ISSUE-01'],'now':'Starting verification'}, {'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'no issues'}, {'action':'set-stage','stage':'report_rollover'}, force=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(batch(plan, {'action':'set-stage','stage':'install'}).returncode, 0)
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

    def test_introspect_requires_four_sections_and_creates_one_verified_task(self):
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
        self.assertNotEqual(batch({'action':'add','entry':{'id':'WF-09','title':'t','source':'Agent reported','status':'untested','observed':'o','recommendation':'r'}}).returncode, 0)
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
        sections['delete'] = {'items':[{'item':'drop unused hook path','evidence':'hook.log','outcome':'fixed inline'}]}
        sections['simplify'] = {'items':[{'item':'store the verify command','evidence':'plan.log','outcome':'issue','issue_id':'BCL-02'}]}
        result = batch({'action':'introspect','sections':sections})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        self.assertEqual(len(doc['findings']), before + 2)  # BCL-02 + WF-01
        task = next(f for f in doc['findings'] if f['id']=='WF-01')
        self.assertEqual((task['id'], task['status'], task['details']['fixed_in'], doc['tracking']['introspect']['task']), ('WF-01', 'verified_fixed', 'Introspect', 'WF-01'))
        self.assertEqual(task['sections']['delete']['items'][0]['outcome'], 'fixed inline')
        self.assertEqual(task['sections']['simplify']['items'][0]['issue_id'], 'BCL-02')
        self.assertEqual(task.get('issues') or [], [])
        self.assertIn('introspect-sections', page.read_text())
        self.assertEqual(doc['tracking']['plan']['status'] if 'plan' in doc.get('tracking',{}) else 'closed', 'closed')

    def test_introspect_task_joins_an_active_verification_run(self):
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
        result = batch({'action':'introspect','sections':none})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        task = doc['tracking']['introspect']['task']
        self.assertEqual(doc['tracking']['verification_run']['checks'][task], {'state': 'pending'})
        self.assertEqual(next(f for f in doc['findings'] if f['id']==task)['status'], 'verified_fixed')

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
                                               'outcome':'inconclusive','evidence':'did not reproduce after 20 attempts',
                                               **marker}]))
            before = self.data.read_bytes()
            self.assertNotEqual(self.cli('batch', self.data, operations, '--render', page).returncode, 0)
            self.assertEqual(self.data.read_bytes(), before)
        finish = {'action':'finish-check','id':'ISSUE-01','run_id':'guard-run',
                  'outcome':'inconclusive','evidence':'did not reproduce after 20 attempts'}
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
            return {'check':'test','outcome':outcome,'build':'ACU 1.2.3','session':'run',
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
        new_record.write_text(json.dumps(evidence('passed', 11, 'new-pass')))
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
            batch({'action':'finish-check','id':issue,'run_id':'install-1','outcome':outcome,'evidence':'original evidence'})
        batch({'action':'complete-verification','run_id':'install-1'})
        batch({'action':'correct-check','id':'ISSUE-02','run_id':'install-1','outcome':'inconclusive','reason':'proof did not establish the outcome'})
        doc = json.loads(self.data.read_text())
        check = doc['tracking']['verification_run']['checks']['ISSUE-02']
        self.assertEqual(check['state'], 'inconclusive')
        self.assertEqual(check['corrections'][0]['previous']['evidence'], 'original evidence')
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
        self.assertEqual(batch([{'action':'finish-check','id':'ISSUE-02','run_id':'install-1','outcome':'blocked','evidence':'no device'}]).returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())['findings'][1]['status'], 'verified_fixed')
        self.assertNotEqual(batch([{'action':'complete-verification','run_id':'install-1'}]).returncode, 0)
        self.assertEqual(batch([{'action':'start-check','id':'ISSUE-01','run_id':'install-1','observation':'first issue 1 state','observed_at':'2026-11-06T00:01:00Z'}]).returncode, 0)
        self.assertEqual(batch([{'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'not_fixed','evidence':'observed failure'}]).returncode, 0)
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
        self.assertEqual(batch([{'action':'finish-check','id':'ISSUE-02','run_id':'install-2','outcome':'not_fixed','evidence':'failure recurred'}]).returncode, 0)
        self.assertEqual(json.loads(self.data.read_text())['findings'][1]['status'], 'failing')

    def test_checking_must_be_saved_before_finishing(self):
        ops = self.root / 'verify.json'
        run = {'id':'install-1','build':'ACU','revision':'abc','installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt'}
        ops.write_text(json.dumps([{'action':'start-verification','run':run}]))
        self.assertEqual(self.cli('batch', self.data, ops, '--render', self.root / 'verify.html').returncode, 0)
        before = self.data.read_bytes()
        ops.write_text(json.dumps([{'action':'start-check','id':'ISSUE-01','run_id':'install-1','observation':'checking begins','observed_at':'2026-11-06T00:01:00Z'},
                                   {'action':'finish-check','id':'ISSUE-01','run_id':'install-1','outcome':'not_fixed','evidence':'failed'}]))
        self.assertNotEqual(self.cli('batch', self.data, ops, '--render', self.root / 'verify.html').returncode, 0)
        self.assertEqual(self.data.read_bytes(), before)

    def test_batch_updates_and_renders_once_and_rejects_invalid_timestamp(self):
        operations = self.root / 'batch.json'
        page = self.root / 'batch.html'
        record = {'check':'client', 'outcome':'passed', 'build':'1', 'session':'run',
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
        operations.write_text(json.dumps([{'action':'add','entry':entry},
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
        record.write_text(json.dumps({'check':'client', 'outcome':'passed', 'build':'0.2.94',
                                      'session':'run-1', 'timestamp':'2026-11-06T00:00:00Z', 'reference':'receipt'}))
        for _ in range(2):
            result = self.cli('record-evidence', self.data, 'ISSUE-01', record)
            self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.data.read_text())['findings'][0]
        self.assertEqual(len(item['verification_evidence']), 2)
        self.assertEqual(item['status'], self.document['findings'][0]['status'])
        page = self.root / 'page.html'
        result = subprocess.run([sys.executable, str(RENDERER), str(self.data), str(page)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verification by version', page.read_text())
        before = self.data.read_bytes()
        record.write_text('{}')
        self.assertNotEqual(self.cli('record-evidence', self.data, 'ISSUE-01', record).returncode, 0)
        self.assertEqual(before, self.data.read_bytes())
        self.assertNotEqual(self.cli('record-evidence', self.data, 'missing', record).returncode, 0)

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
        added = self.cli('add', self.data, entry)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn('add summary, impact and done_when in plain words', added.stderr)
        patch = self.root / 'patch.json'
        patch.write_text(json.dumps({'summary':'trace_id=abc failed','impact':'The flow stops.','done_when':'The control opens.'}))
        updated = self.cli('update', self.data, 'ISSUE-03', patch)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn('summary looks like log text', updated.stderr)
        self.assertEqual(json.loads(self.data.read_text())['findings'][-1]['summary'], 'trace_id=abc failed')

        operations = self.root / 'operations.json'
        operations.write_text(json.dumps([
            {'action':'add','entry':{'id':'ISSUE-04','title':'A row is missing','source':'User reported',
                'status':'untested','observed':'The row is missing.','recommendation':'Show the row.',
                'summary':'python3 check.py failed','impact':'Information is hidden.','done_when':'The row appears.'}},
            {'action':'update','id':'ISSUE-03','patch':{'summary':'git status failed','impact':'The task is blocked.'}}
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
                        "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                                  "review_status": "passed"},
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
        result = self._replay_batch(json.loads(original), {"action": "introspect", "sections": none}, plan)
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
        self.assertEqual(result.returncode, 0, result.stderr)

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
        path1 = self._replay_batch(self._path1_pre(), {"action": "introspect", "sections": none, "result": "round 1 done"}, {
            "action": "set-activity",
            "stage": "plan_fix",
            "ids": ["ISSUE-01"],
            "now": "Plan round 2 after this round's introspect",
        })
        self.assertEqual(path1.returncode, 0, path1.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["workflow"]["stage"], "plan_fix")
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

    def test_set_plan_after_current_round_introspect_advances_round(self):
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
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": next_plan})
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["plan"]["round"], 2)
        self.assertEqual(tracking["round_history"][0]["round"], 1)

    def test_set_plan_after_introspect_advances_even_when_plan_says_current_round(self):
        none = {
            "delete": {"none": "No deletions this cycle."},
            "simplify": {"none": "No simplifications this cycle."},
            "optimize": {"none": "No optimizations this cycle."},
            "automate": {"none": "No automations this cycle."},
        }
        document = self._path1_pre()
        current_round_plan = copy.deepcopy(document["tracking"]["plan"])
        result = self._replay_batch(document,
            {"action": "introspect", "sections": none, "result": "round 1 done"},
            {"action": "set-plan", "plan": current_round_plan})
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())["tracking"]
        self.assertEqual(tracking["round"], 2)
        self.assertEqual(tracking["plan"]["round"], 2)
        self.assertEqual(tracking["round_history"][0]["round"], 1)
        self.assertEqual(tracking["workflow"]["stage"], "plan_fix")

    def test_install_requires_implementer_and_leaves_document_unchanged(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review", "phase": "review",
        }]}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("implementer_session", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_install_requires_reviewer_for_multifile_or_unmarked_group(self):
        for codex in ({"implementer_session": "writer"},
                      {"implementer_session": "writer", "one_file": False}):
            with self.subTest(codex=codex):
                document = copy.deepcopy(self.document)
                document["findings"][0]["status"] = "in_progress"
                plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
                    "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
                    "phase": "review", "codex": codex,
                }]}}
                before = json.dumps(document).encode()
                result = self._replay_batch(document, plan, {
                    "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reviewer_session", result.stderr)
                self.assertEqual(self.data.read_bytes(), before)

    def test_install_requires_a_passing_recorded_review(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
            "codex": {"implementer_session": "writer", "reviewer_session": "reviewer"},
        }]}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review_status='passed'", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_install_allows_one_file_without_reviewer(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
            "phase": "review", "codex": {"implementer_session": "writer", "one_file": True},
        }]}}
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install", json.loads(self.data.read_text())["tracking"]["stages_reached"])

    def test_install_allows_both_sessions_and_ignores_unselected_group(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        document["findings"][1]["status"] = "untested"
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [
            {"id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review",
             "phase": "review", "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                                             "review_status": "passed"}},
            {"id": "B", "finding_ids": ["ISSUE-02"], "depends_on": [], "status": "planned", "phase": "fix"},
        ]}}
        result = self._replay_batch(document, plan, {
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install A",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        self.assertEqual(saved["tracking"]["plan"]["groups"][1]["status"], "planned")

    def test_set_stage_install_checks_every_active_group(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        plan = {"action": "set-plan", "plan": {"round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "review", "phase": "review",
        }]}}
        before = json.dumps(document).encode()
        result = self._replay_batch(document, plan, {"action": "set-stage", "stage": "install"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("implementer_session", result.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_set_activity_install_dispatches_through_declared_workflow_check(self):
        document = copy.deepcopy(self.document)
        document["findings"][0]["status"] = "in_progress"
        plan = {"cycle": 1, "round": 1, "groups": [{
            "id": "A", "finding_ids": ["ISSUE-01"], "depends_on": [], "status": "running",
            "codex": {"implementer_session": "writer", "reviewer_session": "reviewer",
                      "review_status": "passed"},
        }]}
        document["tracking"] = {"cycle": 1, "round": 1, "stages_reached": ["plan_fix"], "plan": plan}
        self.data.write_text(json.dumps(document), encoding="utf-8")
        operations = self.root / "install.json"
        operations.write_text(json.dumps([{
            "action": "set-activity", "stage": "install", "ids": ["ISSUE-01"], "now": "Install",
        }]), encoding="utf-8")
        with patch.object(workflow_definition, "run_check", side_effect=ValueError("declared install check")) as run_check:
            errors = io.StringIO()
            with redirect_stderr(errors), self.assertRaises(SystemExit) as result:
                manage_findings.main(["batch", str(self.data), str(operations), "--render", str(self.root / "page.html")])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("declared install check", errors.getvalue())
        run_check.assert_called_once_with("install", plan, ["ISSUE-01"])

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
        steps = [*check_introspect.performed_round_steps(document["tracking"]), "polling", "rework", "focused tests"]
        sections = {
            "delete": {"items": [{"item": steps[0], "evidence": "Fix diff recorded.", "outcome": "fixed inline"}]},
            "simplify": {"none": "No simplification warranted."},
            "optimize": {"none": "No optimization warranted."},
            "automate": {"none": "No additional automation warranted."},
        }
        none = steps[1:]
        result = self._replay_batch(document, {"action": "introspect", "sections": sections,
                                                "steps": steps, "none": none})
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.data.read_text())
        task = next(item for item in saved["findings"] if item["id"] == "WF-01")
        self.assertEqual(task["steps"], steps)
        self.assertIn("Parallel group count: 1", task["steps"])
        self.assertIn("Codex write: A", task["steps"])
        self.assertIn("Codex review: A", task["steps"])
        self.assertEqual(saved["tracking"]["introspect"]["none"], none)

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
