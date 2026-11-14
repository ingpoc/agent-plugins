import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("manage_findings.py")
EXAMPLE = Path(__file__).parents[1] / "examples/findings.json"
RENDERER = Path(__file__).with_name("render.py")


class ManageFindingsTests(unittest.TestCase):
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
        self.assertEqual(batch({'action':'set-plan','plan':plan}, {'action':'update-plan-group','id':'A','patch':{'status':'done','label':'Updated label','verify_summary':'Runtime check passed'}}).returncode, 0)
        group = json.loads(self.data.read_text())['tracking']['plan']['groups'][0]
        self.assertEqual((group['status'], group['label'], group['verify_summary']), ('done','Updated label','Runtime check passed'))

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
            if stage in ('install', 'verify', 'report_rollover'):
                pre.insert(0, {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}})
                if stage in ('verify', 'report_rollover'):
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
            self.assertEqual(page.read_text().count('aria-current="step"'), 0 if stage is None else 1)

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
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.data.read_text())
        self.assertEqual(data['findings'][0]['status'], 'in_progress')
        self.assertEqual(data['tracking']['workflow']['stage'], 'plan_fix')
        self.assertIn('class="wf-step current" aria-current="step"><span class="wf-dot">', page.read_text())
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}}
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
                self.assertTrue('in_progress' in result.stderr or 'recorded plan' in result.stderr or 'requires install' in result.stderr)
                self.assertEqual(self.data.read_bytes(), before)
        operations = self.root / 'guard.json'
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}}
        operations.write_text(json.dumps([plan, {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'}, {'action':'set-activity','stage':'verify','ids':['ISSUE-01'],'now':'Starting verification'}]))
        result = self.cli('batch', self.data, operations, '--render', self.root / 'guard.html')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())['findings'][0]['status'], 'in_progress')
        before = self.data.read_bytes()
        operations.write_text(json.dumps([plan, {'action':'set-activity','stage':'install','ids':[],'now':'No selected finding'}]))
        empty = self.cli('batch', self.data, operations, '--render', self.root / 'empty.html')
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn('nonempty ID list', empty.stderr)
        self.assertEqual(self.data.read_bytes(), before)

    def test_finding_write_advances_workflow_updated_at(self):
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
        self.assertGreater(written['tracking']['workflow']['updated_at'], '2020-01-01T00:00:00Z')
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
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','name':'Plain group','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending','worktree':'fix-a','codex':{'job_id':'job-1','status':'done','started_at':'2026-11-14T09:00:00Z','ended_at':'2026-11-14T09:05:00Z'},'verify':{'summary':'Focused check passed','status':'pending'}}],'merge':{'status':'done','ref':'abc'}}}
        ops.write_text(json.dumps([plan, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Plan'}, {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Install'}, {'action':'set-activity','stage':'verify','ids':['ISSUE-01'],'now':'Verify'}, {'action':'set-activity','stage':'gate','ids':[],'now':'Gate'}, {'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],'now':'Repeat'}]))
        result = self.cli('batch', self.data, ops, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())['tracking']
        self.assertEqual(tracking['round'], 2)
        self.assertEqual(tracking['round_history'][0]['round'], 1)
        self.assertEqual(tracking['plan']['round'], 2)
        group = tracking['plan']['groups'][0]
        self.assertEqual((group['name'], group['worktree'], group['codex']['job_id'], group['codex']['duration_s']), ('Plain group','fix-a','job-1',300))

    def test_set_activity_marks_group_and_renders_single_now_line(self):
        operations, page = self.root / 'activity.json', self.root / 'activity.html'
        operations.write_text(json.dumps([{'action':'set-activity','stage':'plan_fix','ids':['ISSUE-01'],
                                           'now':'Inspecting the shared root cause'}]))
        result = self.cli('batch', self.data, operations, '--render', page)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.data.read_text())
        self.assertEqual(doc['findings'][0]['status'], 'in_progress')
        activity = doc['tracking']['workflow']['now']
        self.assertRegex(activity, r'^\d{4}-\d\d-\d\dT.*Z — Inspecting the shared root cause$')
        html = page.read_text()
        self.assertIn('class="workflow-now"', html)
        self.assertNotIn('id="verification-progress"', html)

    def test_rollover_excludes_fixed_lists_rest_and_updates_header(self):
        document = json.loads(self.data.read_text())
        document['findings'][0].update(status='verified_fixed', verification='Verified on the installed build.', verification_refs=['receipt'])
        document['findings'][1]['status'] = 'untested'
        self.data.write_text(json.dumps(document))
        operations, page = self.root / 'rollover.json', self.root / 'rollover.html'
        operations.write_text(json.dumps([{'action':'rollover','source_pr':96,'worktree':'next-wt',
            'pull_request':{'label':'PR #98','url':'https://example.test/pull/98'}}]))
        self.assertNotEqual(self.cli('batch', self.data, operations).returncode, 0)
        blocked = self.cli('batch', self.data, operations, '--render', page)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('--force', blocked.stderr)
        result = self.cli('batch', self.data, operations, '--render', page, '--force')
        self.assertEqual(result.returncode, 0, result.stderr)
        tracking = json.loads(self.data.read_text())['tracking']
        self.assertIn('ISSUE-01', tracking['rollover']['excluded_ids'])
        self.assertNotIn('ISSUE-01', tracking['rollover']['unresolved_ids'])
        self.assertEqual((tracking['pull_request']['label'], tracking['worktree']), ('PR #98', 'next-wt'))
        self.assertEqual(tracking['workflow']['stage'], 'collect')
        self.assertEqual(tracking['cycle'], 2)
        self.assertEqual(tracking['rollover']['forced_gate_overrides'][0]['action'], 'rollover')
        self.assertIn('PR #98', page.read_text())

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
        plan = {'action':'set-plan','plan':{'round':1,'groups':[{'id':'A','finding_ids':['ISSUE-01'],'depends_on':[],'status':'pending'}]}}
        install = {'action':'set-activity','stage':'install','ids':['ISSUE-01'],'now':'Installed'}
        self.assertEqual(batch(plan, install, {'action':'set-activity','stage':'verify','ids':['ISSUE-01'],'now':'Starting verification'}, {'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'no issues'}, {'action':'set-stage','stage':'report_rollover'}, force=True).returncode, 0)
        self.assertEqual(batch(plan, {'action':'set-stage','stage':'install'}).returncode, 0)
        blocked = batch({'action':'set-stage','stage':'report_rollover'})
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('gate not met', blocked.stderr)
        self.assertNotEqual(batch({'action':'rollover','source_pr':1,'worktree':'w','pull_request':{'label':'PR #2','url':'https://example.test/pull/2'}}).returncode, 0)
        self.assertNotEqual(batch({'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'two\nlines'}).returncode, 0)
        self.assertEqual(batch({'action':'start-verification','run':{'id':'r1','build':'b1','revision':'abc','installed_at':'2026-11-12T10:00:00+05:30','install_evidence':'log'}}).returncode, 0)
        result = batch({'action':'introspect','sections':{'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}},'result':'no issues'}, {'action':'set-stage','stage':'report_rollover'}, force=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.data.read_text())['tracking']['introspect']['result'], 'no issues')
        self.assertEqual(batch({'action':'rollover','source_pr':1,'worktree':'w','pull_request':{'label':'PR #2','url':'https://example.test/pull/2'}}, force=True).returncode, 0)
        tracking = json.loads(self.data.read_text())['tracking']
        self.assertNotIn('introspect', tracking)
        self.assertNotIn('stages_reached', tracking)
        self.assertNotIn('verification_run', tracking)

    def test_introspect_requires_four_sections_and_creates_one_verified_task(self):
        page, ops = self.root / 'one.html', self.root / 'one.json'
        def batch(*items):
            ops.write_text(json.dumps(list(items)))
            return self.cli('batch', self.data, ops, '--render', page)
        none = {'delete':{'none':'No deletions this cycle.'},'simplify':{'none':'No simplifications this cycle.'},'optimize':{'none':'No optimizations this cycle.'},'automate':{'none':'No automations this cycle.'}}
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
        self.assertEqual(groups['AC']['verify']['status'], 'failed')
        page_text = page.read_text()
        self.assertTrue('Failed' in page_text or 'failed' in page_text or 'partial' in page_text)

    def test_set_activity_requires_reopen_for_closed_findings(self):
        operations, page = self.root / 'closed-activity.json', self.root / 'closed-activity.html'
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
        self.data = self.root / "findings.json"
        self.document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.data.write_text(json.dumps(self.document), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              text=True, capture_output=True)

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


if __name__ == "__main__":
    unittest.main()
