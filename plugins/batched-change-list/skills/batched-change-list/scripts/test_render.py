import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("change_list_render", Path(__file__).with_name("render.py"))
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class RenderTests(unittest.TestCase):
    def test_checks_pending_lane_is_not_unqualified_blocked(self):
        pending = renderer.lane_issue_status(
            {"id": "CONTROL", "status": "blocked", "blocked_reason": "Individual issue checks pending"}, 1)
        self.assertTrue(pending["copy"].startswith("Checks pending"))
        self.assertEqual(pending["kind"], "run")
        self.assertEqual(pending["status"], "verifying")
        verifying = renderer.lane_issue_status(
            {"id": "DISPLAY", "status": "verifying", "blocked_reason": "Remaining issue checks pending"}, 1)
        self.assertTrue(verifying["copy"].startswith("Checks pending"))
        self.assertEqual(verifying["status"], "verifying")
        blocked = renderer.lane_issue_status(
            {"id": "A", "status": "blocked", "blocked_reason": "no mic · needs Guru"}, 1)
        self.assertTrue(blocked["copy"].startswith("Blocked ·"))
        self.assertEqual(blocked["kind"], "blk")
        self.assertEqual(blocked["status"], "blocked")

    def test_blocked_or_planned_lanes_do_not_make_recorded_stage_busy(self):
        groups = [{'id':'A', 'status':'blocked', 'phase':'fix'}, {'id':'B', 'status':'planned'}]
        page = renderer.render_workflow('plan_fix', groups, 0, 15, False, 0, 1)
        plan = page.split('>Plan &amp; fix<', 1)[0].rsplit('<li ', 1)[-1]
        self.assertIn('wf-step upcoming', plan)
        track = renderer.row_phase_track({'id':'A', 'status':'verified'}, 'verified')
        self.assertEqual(track.count('<i'), 4)
        self.assertEqual(track.count('class="d"'), 4)

    def test_planned_lane_does_not_look_active_during_introspection(self):
        page = renderer.render_workflow('introspect', [{'id':'A', 'status':'planned'}], 0, 15, False, 0, 1)
        plan = page.split('>Plan &amp; fix<', 1)[0].rsplit('<li ', 1)[-1]
        self.assertNotIn('wf-step busy', plan)
        self.assertIn('1 planned', page)

    def test_gate_uses_verified_count_and_never_shows_a_now_line(self):
        self.data['tracking'] = {'workflow': {'stage':'collect','updated_at':'2026-11-14T07:00:00Z'},
                                 'rollover_threshold': 1000}
        page = renderer.render(self.data, self.base)
        self.assertIn('data-stage="collect" class="wf-step current" aria-current="step"', page)
        self.assertIn('Updated <time datetime=', page)
        self.assertNotIn('workflow-now', page)
        self.assertNotIn('wf-now-tag', page)
        self.assertIn('.wf-step.current .wf-dot i{width:14px;height:14px;border:2.5px solid var(--active);', page)
        gate_node = page.split('data-stage="gate"', 1)[1].split('</li>', 1)[0]
        self.assertIn('class="wf-step upcoming wf-gate"', gate_node)
        self.assertIn('<svg width="22" height="22" viewBox="0 0 22 22"', gate_node)
        self.assertIn('stroke="var(--sage-line)" stroke-width="3"', gate_node)
        self.assertNotIn('var(--amber)', gate_node)
        self.assertIn('Fixes <b>1/1000</b> verified<small>0 in progress</small>', gate_node)
        self.assertNotIn('excludes completed introspection', gate_node)
        self.assertIn('Report at 1000+ verified or when no counted findings remain open.', page)
        self.assertIn('Rollover requires at least 16+ verified and the rollover threshold.', page)

    def test_gate_does_not_count_a_fixed_issue_during_an_unresolved_recheck(self):
        finding = {'id':'ISSUE-01', 'status':'verified_fixed', 'verification_refs':['old receipt']}
        tracking = {'rollover_threshold':1, 'verification_run':{'checks':{'ISSUE-01':{'state':'fixed'}}}}
        self.assertEqual(renderer.rollover_gate([finding], tracking), (1, 1, True))
        for state in ('checking', 'blocked', 'inconclusive', 'not_fixed'):
            with self.subTest(state=state):
                tracking['verification_run']['checks']['ISSUE-01']['state'] = state
                self.assertEqual(renderer.rollover_gate([finding], tracking), (0, 1, False))
        self.assertEqual(finding['verification_refs'], ['old receipt'])

        self.data['findings'][0]['status'] = 'verified_fixed'
        self.data['findings'][0]['verification'] = 'Verified on the installed build.'
        self.data['findings'][0]['verification_refs'] = ['Example receipt']
        self.data['tracking'] = {'workflow': {'stage':'collect','updated_at':'2026-11-14T07:00:00Z'}}
        self.data['tracking']['rollover_threshold'] = 1
        page = renderer.render(self.data, self.base)
        self.assertIn('data-stage="collect" class="wf-step current" aria-current="step"', page)
        gate_node = page.split('data-stage="gate"', 1)[1].split('</li>', 1)[0]
        self.assertIn('class="wf-step done wf-gate"', gate_node)
        self.assertIn('<svg width="22" height="22" viewBox="0 0 22 22"', gate_node)
        self.assertIn('stroke-width="3"', gate_node)


    def test_fresh_rollover_collect_leaves_loop_steps_empty_and_can_go_stale(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {
            'round': 1,
            'stages_reached': ['collect'],
            'workflow': {'stage': 'collect', 'updated_at': '2026-11-14T11:00:00Z'},
        }
        page = renderer.render(data, self.base, now=renderer.parse_instant('2026-11-14T11:11:00Z'))
        band = page.split('class="wf-band-steps"', 1)[1].split('</ol>', 1)[0]
        for label in ('Plan &amp; fix', 'Install', 'Verify', 'Introspect'):
            self.assertIn(f'class="wf-step upcoming"><span class="wf-dot"><i></i></span><span class="wf-label">{label}</span>', band)
        self.assertNotIn('class="wf-step done">', band)
        self.assertIn('data-stage="collect" class="wf-step current" aria-current="step"', page)
        self.assertIn('class="tag-stale" title="No update for over 10 minutes">Stale</span>', page)
        self.assertNotIn('class="tag-stale" title="No update for over 10 minutes" hidden', page)
        fresh = renderer.render(data, self.base, now=renderer.parse_instant('2026-11-14T11:05:00Z'))
        self.assertIn('class="tag-stale" title="No update for over 10 minutes" hidden', fresh)

    def test_merged_page_shows_commit_and_ist_time(self):

        sha = 'a' * 40
        self.data['tracking'] = {'workflow': {'stage':'merged','updated_at':'2026-11-14T11:44:49Z'},
                                 'merged': {'commit_sha':sha,'merged_at':'2026-11-14T11:44:49Z'}}
        page = renderer.render(self.data, self.base)
        self.assertIn('Merged / Done', page)
        self.assertIn('data-stage="merged" class="wf-step done"', page)
        self.assertNotIn('data-stage="report_rollover" class="wf-step current"', page)
        self.assertNotIn('aria-current="step"', page)
        self.assertIn(sha, page)
        self.assertIn('5:14:49 PM IST', page)
        self.assertIn('Merge commit', page)

    def test_recorded_plan_renders_deterministic_lanes_and_shared_merge(self):
        self.data['tracking'] = {'workflow': {'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}, 'plan': {'cycle': 1, 'round': 1, 'groups': [
            {'id':'A','finding_ids':['ISSUE-01'],'reason':'Shared cause','depends_on':[],'status':'running',
             'codex':{'model':'gpt-6-luna','status':'running','started_at':'2026-11-14T09:00:00Z'},
             'verify':{'method':'Live check','status':'pending'}},
            {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'pending'}]}}
        page = renderer.render(self.data, self.base)
        self.assertEqual(page, renderer.render(self.data, self.base))
        for marker in ('class="wf"', 'class="wf-band gate-pending"', 'class="wf-loop"', 'wf-gate', 'class="grp running"', 'class="grp planned"', 'after A', 'href="#ISSUE-01"', 'title="'):
            self.assertIn(marker, page)
        self.assertIn('<div class="plan-head"><span><strong>Round 1</strong> · 2 lanes · each installs and verifies when ready</span></div>', page)
        self.assertLess(page.index('class="plan-head"'), page.index('class="plan-rows"'))
        self.assertNotIn('class="tail"', page)
        self.assertNotIn('round-plan', page)
        self.assertIn('Fixing', page)
        self.assertIn('Planned', page)
        self.assertIn('No fix plan for Round 1 yet.', renderer.render({**self.data, 'tracking':{'workflow':{'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}}}, self.base))
        self.assertNotIn('class="plan-rows"', renderer.render({**self.data, 'tracking':{}}, self.base))

    def test_status_v2_renders_stale_clock_parked_copy_and_phase_tracks(self):
        data = copy.deepcopy(self.data)
        extra = copy.deepcopy(data['findings'][0])
        extra.update(id='ISSUE-03', title='Carried parked issue', status='failing', carried_from=1)
        data['findings'][0]['status'] = 'in_progress'
        data['findings'][0]['carried_from'] = 2
        data['findings'][1]['status'] = 'untested'
        data['findings'].append(extra)
        data['tracking'] = {
            'round': 1,
            'workflow': {'stage':'verify','updated_at':'2026-11-14T11:00:00Z',
                         'now':'2026-11-14T11:13:00Z — Build review'},
            'plan': {'round':1,'groups':[
                {'id':'INSTALL','name':'Renderer','finding_ids':['ISSUE-01'],'status':'installing',
                 'phase':'install','build':'BCL 0.1.18','codex':{'model':'gpt-6-luna','effort':'max','fast':True}},
                {'id':'VOICE','name':'Voice meter','finding_ids':['ISSUE-02'],'status':'blocked',
                 'phase':'verify','blocked_reason':'no mic · needs Guru'},
                {'id':'CORE','name':'Core lease','finding_ids':['ISSUE-03'],'status':'parked','parked_to':2},
            ]}}
        page = renderer.render(data, self.base, now=renderer.parse_instant('2026-11-14T11:13:00Z'))
        self.assertIn('Updated <time datetime=', page)
        self.assertIn('4:30 PM IST', page)
        self.assertIn('13 min ago', page)
        self.assertIn('class="tag-stale"', page)
        self.assertIn('data-active-lanes="true"', page)
        self.assertIn('2 lanes · each installs and verifies when ready', page)
        self.assertIn('<b>1</b> installing', page)
        self.assertIn('<b>1</b> blocked', page)
        self.assertIn('Parked for Round 2', page)
        self.assertIn('CORE · 1', page)
        self.assertIn('aria-label="Phase 3 of 4: Install"', page)
        self.assertIn('aria-label="Phase 4 of 4: Verify, blocked"', page)
        self.assertIn('In install · INSTALL', page)
        self.assertIn('Blocked · no mic · needs Guru', page)
        self.assertIn('CARRIED', page)
        self.assertIn('Fixes <b>0/15</b> verified<small>1 in progress</small>', page)
        self.assertIn('Round closes when every lane is <b>verified, blocked or parked</b> → Introspect → gate.', page)
        self.assertNotIn('Working on it', page)
        self.assertNotIn('workflow-now', page)
        self.assertIn('setInterval(formatPageTimes,60000)', page)

    def test_compact_plan_labels_duration_status_details_and_fallbacks(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'workflow': {'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}, 'plan': {'round': 1, 'groups': [
            {'id':'A','name':'Render label','finding_ids':['ISSUE-01'],'status':'done','codex':{'model':'gpt-6-luna','effort':'medium','service_tier':'priority','duration_s':300,'started_at':'2026-11-14T09:00:00Z','ended_at':'2026-11-14T09:05:00Z'},'verify':{'status':'pending','method':'Run the focused test and inspect the live page carefully'}},
            {'id':'B','finding_ids':['ISSUE-02'],'status':'done','verify':{'status':'done','method':'Verified on runtime'}},
            {'id':'C','finding_ids':['ISSUE-01'],'status':'verifying','verify':{'status':'running','method':'Verify running'}},
            {'id':'D','finding_ids':['ISSUE-02'],'status':'blocked','verify':{'status':'pending','failure':'Codex job failed tests did not pass','method':'Blocked'}}]}}
        page = renderer.render(data, self.base)
        row = page.split('<details class="grp installing">', 1)[1].split('</details>', 1)[0]
        self.assertIn('5 min', row); self.assertIn('Luna medium Fast', row)
        self.assertIn('Render label', row); self.assertIn('Install', row)
        self.assertIn('Run the focused test and inspect the live page carefully', page)
        self.assertIn('Blocked', page); self.assertIn('Codex job failed tests did not pass', page)
        self.assertIn('Verifying', page); self.assertIn('Verified', page)
        self.assertIn('2:30 PM IST – 2:35 PM IST', page)
        self.assertNotRegex(row, r'20\d\d-\d\d-\d\dT')
        self.assertIn('2:30 PM IST – 2:35 PM IST', page)
        self.assertIn('Group B', page)


    def test_viewer_footer_reports_missing_identity_deterministically(self):
        plain = renderer.render(self.data, self.base)
        self.assertEqual(plain, renderer.render(self.data, self.base))
        self.assertIn('Viewer version not recorded', plain)
        live = renderer.render(self.data, self.base, viewer={'path':'/tmp/list.json','version':'0.1.17','started':'Nov 14, 2026, 3:00 PM IST'})
        self.assertIn('reading /tmp/list.json, viewer 0.1.17, started Nov 14, 2026, 3:00 PM IST', live)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_version_timeline_shows_status_and_evidence_together(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        self.assertIn("part('Status of last verification'", template)
        self.assertIn("node('span',record.version,'version-chip')", template)
        self.assertIn("`Checked ${ist(record.timestamp)} · ${record.build}`", template)
        # Shown in the main card after Done when, never inside the collapsed Evidence section.
        self.assertLess(template.index("'Status of last verification'"), template.index("'Verification by version'"))
        self.assertLess(template.index("'Verification by version'"), template.index("node('details',null,'evidence')"))
        evidence_builder = next(line for line in template.splitlines() if line.startswith(' const detail=node(\'details\''))
        self.assertNotIn('Verification by version', evidence_builder)
        # Agent-facing fix hints live in Evidence; machine-readable state sits on the article.
        self.assertIn("evidenceRows.push(['Suggested fix · where to look',selected.recommendation])", template)
        self.assertNotIn("part('Suggested fix'", template)
        self.assertIn("article.dataset.status=selected.status", template)

    def test_optional_readable_fields_and_observed_fallbacks(self):
        data = copy.deepcopy(self.data)
        item = data['findings'][0]
        item.update(summary='A plain problem sentence.', impact='Readers cannot understand it.', done_when='The panel shows a clear result.')
        page = renderer.render(data, self.base)
        payload = page.split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0]
        rendered = json.loads(payload)[0]
        self.assertEqual((rendered['summary'], rendered['impact'], rendered['done_when'], rendered['evidence_text']),
                         ('A plain problem sentence.', 'Readers cannot understand it.', 'The panel shows a clear result.', item['observed']))
        item.pop('summary')
        item.pop('impact')
        item.pop('done_when')
        item['observed'] = 'The control does not open. The panel stays closed.'
        rendered = json.loads(renderer.render(data, self.base).split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0])[0]
        self.assertEqual((rendered['summary'], rendered['evidence_text']), ('The control does not open.', 'The panel stays closed.'))
        item['observed'] = 'trace_id=4af39b2 command.py failed. More evidence follows.'
        rendered = json.loads(renderer.render(data, self.base).split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0])[0]
        self.assertEqual((rendered['summary'], rendered['evidence_text']), ('', item['observed']))

    def test_optional_fields_validate_as_strings_and_pending_is_stripped(self):
        data = copy.deepcopy(self.data)
        item = data['findings'][0]
        item.pop('done_when', None)
        item['verification'] = 'Pending: The installed view shows the selected row.'
        page = renderer.render(data, self.base)
        payload = page.split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(payload)[0]['done_when'], 'The installed view shows the selected row.')
        for field in ('summary', 'impact', 'done_when'):
            invalid = copy.deepcopy(data)
            invalid['findings'][0][field] = 3
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                renderer.render(invalid, self.base)

    def test_done_when_falls_back_to_expected_detail_case_insensitively(self):
        data = copy.deepcopy(self.data)
        item = data['findings'][0]
        item.pop('done_when', None)
        item.pop('verification', None)
        item['details'] = {'eXpEcTeD': 'The selected row opens.'}
        payload = renderer.render(data, self.base).split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(payload)[0]['done_when'], 'The selected row opens.')

    def test_log_text_heuristic(self):
        for text in ('build=failed', 'a1b2c3d', 'At 01:05:18', '`quoted`', '{payload}',
                     'src/module.py', 'code_value', 'python3 script.py', 'x' * 241):
            with self.subTest(text=text):
                self.assertTrue(renderer.looks_like_log_text(text))
        for text in ('The button does not open.', 'A reader can understand this.'):
            with self.subTest(text=text):
                self.assertFalse(renderer.looks_like_log_text(text))

    def test_evidence_is_one_flat_disclosure(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        self.assertIn("node('summary','Details and evidence')", template)
        self.assertNotIn('Agent details', template)
        self.assertNotIn('Additional details', template)
        self.assertIn("const detail=node('details',null,'evidence')", template)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_issue_shows_outcome_separately_from_recorded_plan(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        show = template.split('function show(){', 1)[1].split('\nsearch.addEventListener', 1)[0]
        program = r"""
class Element {
 constructor(tag,text='',cls=''){this.tag=tag;this.text=text;this.cls=cls;this.children=[];this.dataset={}}
 append(...children){this.children.push(...children)}
 setAttribute(){}
 removeAttribute(){}
}
const node=(tag,text,cls)=>new Element(tag,text,cls);
const findings=[{id:'A',title:'Issue',status:'failing',details:{Build:'0.2.147'},_lane_status:{copy:'Parked → Round 2',kind:'prk',status:'parked'}}];
const labels={failing:'Failing'},location={hash:'#A'},run=null;
const nav={querySelectorAll:()=>[]},panel={replaceChildren(value){this.article=value}};
const document={createTextNode:text=>text};
function show(){
""" + show + r"""
show();
const heading=panel.article.children[0];
if(heading.children[1].text!=='Issue status: Failing')process.exit(1);
const detail=panel.article.children.find(e=>e.tag==='details'&&e.cls==='evidence');
if(!detail.children.some(e=>e.tag==='section'&&e.children[0].text==='Plan (last recorded)'&&e.children[1].text==='Parked → Round 2'))process.exit(2);
const sections=panel.article.children.filter(e=>e.tag==='section');
if(!sections.some(e=>e.children[0].text==='Build recorded with issue'&&e.children[1].text==='0.2.147'))process.exit(3);
const history=sections.find(e=>e.children[0].text==='Verification by version');
if(!sections.some(e=>e.children[0].text==='Status of last verification'&&e.children[1].text==='No verification result recorded.'))process.exit(6);
if(!history||history.children[1].text!=='No versioned verification recorded.')process.exit(4);
findings[0].version_timeline=[
 {version:'0.2.147',build:'ACU 0.2.147',state:'not_fixed',verdict:'Failing because 10/12 passed',checked:'typed session',expected:'12/12 pass',observed:'10/12 on typed session',timestamp:'2026-11-14T17:06:53Z'},
 {version:'0.2.143',build:'ACU 0.2.143',state:'not_fixed',verdict:'Failing because 6/12 passed',checked:'typed turns',expected:'12/12 pass',observed:'6/12 on typed turns',timestamp:'2026-11-14T10:30:44Z'}];
show();
const list=panel.article.children.find(e=>e.cls==='issue-part version-history').children.find(e=>e.tag==='ol');
if(!list||list.children.length!==2||list.children[0].children[0].text!=='0.2.147'||list.children[1].children[0].text!=='0.2.143')process.exit(5);
if(!panel.article.children.some(e=>e.tag==='section'&&e.children[0].text==='Status of last verification'&&e.children[1].text==='Failing'))process.exit(7);
if(list.children[0].children[1].text!=='Failing'||list.children[1].children[1].text!=='Failing')process.exit(8);

findings[0].version_timeline.push({version:'0.2.148',build:'ACU 0.2.148',state:'fixed',outcome:'fixed',timestamp:'unknown'});
show();
if(!panel.article.children.some(e=>e.tag==='section'&&e.children[0].text==='Status of last verification'&&e.children[1].text==='Latest result cannot be determined; some verification dates are unavailable. Latest dated: Failing.'))process.exit(12);
if(panel.article.children.find(e=>e.cls==='issue-part version-history').children.find(e=>e.tag==='ol').children.length!==3)process.exit(13);

if(list.children[0].children[2].children[0].text!=='Checked: typed session'||list.children[0].children[2].children[1].text!=='Expected: 12/12 pass'||list.children[0].children[2].children[2].text!=='Observed: 10/12 on typed session'||list.children[0].children[2].children[3].text!=='Verdict because: Failing because 10/12 passed')process.exit(11);
findings[0].version_timeline=[{version:'0.2.143',build:'ACU 0.2.143',state:'blocked',outcome:'blocked',timestamp:'2026-11-14T10:30:44Z'}];
show();
const blocked=panel.article.children.find(e=>e.cls==='issue-part version-history').children.find(e=>e.tag==='ol').children[0];
if(blocked.children[1].text!=='Blocked')process.exit(9);
findings[0].version_timeline[0].state='';findings[0].version_timeline[0].outcome='reviewed';
show();
const ambiguous=panel.article.children.find(e=>e.cls==='issue-part version-history').children.find(e=>e.tag==='ol').children[0];
if(ambiguous.children[1].text!=='Status not recorded'||ambiguous.children[2].children[3].text!=='Verdict because: reviewed')process.exit(10);
findings[0].version_timeline=[{version:'Version unknown',build:'unknown',state:'fixed',outcome:'fixed',timestamp:'unknown'}];
show();
const undated=panel.article.children.find(e=>e.cls==='issue-part version-history').children.find(e=>e.tag==='ol').children[0];
if(undated.children[0].text!=='Version unknown'||undated.children[1].text!=='Fixed')process.exit(14);
if(!panel.article.children.some(e=>e.tag==='section'&&e.children[0].text==='Status of last verification'&&e.children[1].text==='Fixed · Date unavailable'))process.exit(15);
"""
        subprocess.run(['node', '-e', program], check=True)

    def test_plan_keeps_verify_command_in_collapsed_details(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'workflow': {'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}, 'plan': {'round':1,'groups':[
            {'id':'A','finding_ids':['ISSUE-01'],'status':'running','verify':{'command':'python3 check.py --id ISSUE-01'}}]}}
        page = renderer.render(data, self.base)
        self.assertIn('<dt>Verify command</dt><dd>python3 check.py --id ISSUE-01</dd>', page)
        self.assertNotIn('Issue list status · derived from lane', page)

    def test_rechecks_on_one_build_keep_each_recorded_outcome(self):
        item = {'id': 'ISSUE-01'}
        runs = [{'build': 'ACU 1.2.3', 'checks': {'ISSUE-01': {
            'state': 'fixed', 'evidence': 'Display now shows the correct value', 'finished_at': '2026-11-15T02:00:00Z',
            'previous_checks': [{'state': 'not_fixed', 'evidence': 'Display showed the wrong value', 'finished_at': '2026-11-15T01:00:00Z'}]}}}]
        timeline = renderer.version_evidence(item, runs)
        self.assertEqual([(entry['state'], entry['outcome']) for entry in timeline], [
            ('fixed', 'Display now shows the correct value')])
        self.assertEqual([(entry['state'], entry['outcome']) for entry in timeline[0]['history']], [
            ('not_fixed', 'Display showed the wrong value')])

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_fingerprint_poll_reloads_once_only_after_change_and_preserves_view(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        save_view = next(line for line in template.splitlines() if line.startswith('const saveView='))
        refresh = next(line for line in template.splitlines() if line.startswith("if(location.protocol==='http:'"))
        self.assertIn("method:'HEAD'", refresh)
        self.assertIn('setInterval(refresh,5000)', refresh)
        self.assertNotIn("fetch(location.href,{cache:'no-store'})", refresh)
        program = """
let etag='one', reloads=0, saved=null, poll, offline=false;
const search={value:'query',selectionStart:2,selectionEnd:4};
const evidence={open:true,querySelector(){return null}};
const warning={hidden:true};
const document={hidden:false,activeElement:search,getElementById(){return warning}};
const location={protocol:'http:',href:'http://example.test/',hash:'',reload(){reloads++}};
const history={replaceState(_state,_title,hash){location.hash=hash}};
const selected={hash:'#ISSUE-01'};
const nav={querySelector(){return selected}};
const panel={querySelector(){return evidence}};
const window={scrollX:12,scrollY:345};
const sessionStorage={setItem(_key,value){saved=value}};
const run={status:'complete'};
const fetch=async()=>{if(offline)throw new Error('offline');return {ok:true,headers:{get(){return etag}}}};
const setInterval=fn=>{poll=fn};
""" + save_view + "\n" + refresh + "\n" + """
setImmediate(async()=>{
 await poll();
 if(reloads!==0)process.exit(1);
 await poll();
 if(reloads!==0)process.exit(2);
 offline=true;await poll();if(warning.hidden)process.exit(6);
 offline=false;await poll();if(!warning.hidden)process.exit(7);
 etag='two';
 await poll();
 if(reloads!==1)process.exit(3);
 await poll();
 if(reloads!==1)process.exit(4);
 const state=JSON.parse(saved);
 if(location.hash!=='#ISSUE-01'||state.y!==345||state.x!==12||state.query!=='query'||!state.details)process.exit(5);
});
"""
        subprocess.run(['node', '-e', program], check=True)

    def test_previous_check_summary_is_removed_and_progress_requires_active_run(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        self.assertNotIn('function latestResult(', template)
        self.assertIn("run?.status==='active'", template)
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertNotIn('id="verification-progress"', page)

    def test_inventory_override_and_recorded_fallback(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'installed_plugins': {'versions': {'batched-change-list':'0.1.2'},
                                                  'observed_at':'2026-11-10T00:00:00Z','source':'saved inventory'}}
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('batched-change-list 0.1.2', page)
        self.assertIn('As recorded', page)
        inventory = {'versions': {'batched-change-list':'0.1.3'},
                     'observed_at':'2026-11-10T01:00:00Z','source':'live inventory'}
        page = renderer.render(data, Path(__file__).parents[1], inventory)
        self.assertIn('batched-change-list 0.1.3', page)
        self.assertIn('Inventory checked', page)
        self.assertNotIn('batched-change-list 0.1.2', page)
        self.assertNotIn('Inventory supplied at render time', page)
        self.assertNotIn('Live, read from installed copies at render time', page)

    def test_workflow_times_show_one_updated_clock_and_tick_in_browser(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'verify','updated_at':'2026-11-10T10:00:00Z',
                                         'now':'2026-11-10T11:00:00Z — Checking the installed build'}}
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertEqual(page.count('<time datetime='), 1)
        self.assertIn('Updated <time datetime=', page)
        self.assertIn('3:30 PM IST', page)
        self.assertIn('1 hr ago', page)
        self.assertNotIn('workflow-now', page)
        self.assertNotIn('Last reported', page)
        self.assertNotIn('Intl.RelativeTimeFormat', page)
        self.assertIn('setInterval(formatPageTimes,60000)', page)
        data['tracking']['workflow']['now'] = '2026-11-12T10:00:00Z — Checking the installed build'
        self.assertIn('2 days ago', renderer.render(data, Path(__file__).parents[1]))

    def test_workflow_strip_shows_gate_loop_round_and_report_last_deterministically(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'gate','updated_at':'2026-11-14T07:00:00Z'},
                            'cycle':3,'round':3,'rollover_threshold':15}
        for index, item in enumerate(data['findings']):
            item.update(status='verified_fixed', verification='Verified on the installed build.',
                        verification_refs=['Example receipt'])
        for index in range(5):
            item = copy.deepcopy(data['findings'][0])
            item['id'] = f'ISSUE-{index + 3:02d}'
            data['findings'].append(item)
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes <b>7/15</b> verified', page)
        self.assertIn('Round 3', page)
        self.assertIn('under 15 verified', page)
        self.assertIn('class="wf-band gate-met"', page)
        self.assertIn('class="wf-step done wf-gate"', page)
        self.assertIn('class="wf-exit"', page)
        self.assertIn('gate-met .wf-loop', page)
        timeline = page
        stages = ['>Collect<','>Plan &amp; fix<','>Install<','>Verify<',
                  '>Introspect<','Fixes <b>7/15</b> verified','>Report &amp; roll over<','>Merge previous PR<']
        positions = [timeline.index(stage) for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('>Present<', timeline)
        self.assertEqual(page.count('aria-label="Workflow progress"'), 1)
        self.assertEqual(renderer.render(data, Path(__file__).parents[1]), page)

    def test_rollover_entry_hides_present_until_current_cycle_reaches_it(self):
        hidden = renderer.render_workflow('plan_fix', [], 0, 15, False, 0, 1, ['collect'], cycle=2)
        self.assertNotIn('>Present<', hidden)
        shown = renderer.render_workflow('present', [], 0, 15, False, 0, 1, ['collect', 'present'], cycle=2)
        self.assertIn('>Present<', shown)

    def test_install_tick_requires_review_and_a_completed_install_lane(self):
        failed = renderer.render_workflow('verify', [{
            'id':'A', 'status':'failed', 'phase':'install',
            'codex':{'review_status':'failed'},
        }], 0, 15, False, 0, 1, ['plan_fix', 'install', 'verify'])
        install = failed.split('data-stage="install"', 1)[1].split('</li>', 1)[0]
        self.assertNotIn('wf-step done', install)

        passed = renderer.render_workflow('verify', [{
            'id':'A', 'status':'verifying', 'phase':'verify',
            'codex':{'reviewer_session':'reviewer', 'review_status':'passed'},
        }], 0, 15, False, 0, 1, ['plan_fix', 'install', 'verify'])
        install = passed.split('data-stage="install"', 1)[1].split('</li>', 1)[0]
        self.assertIn('wf-step done', install)

        one_file = renderer.render_workflow('verify', [{
            'id':'A', 'status':'verifying', 'phase':'verify', 'codex':{'one_file':True},
        }], 0, 15, False, 0, 1, ['plan_fix', 'install', 'verify'])
        install = one_file.split('data-stage="install"', 1)[1].split('</li>', 1)[0]
        self.assertIn('wf-step done', install)

        failed_review = renderer.render_workflow('verify', [{
            'id':'A', 'status':'verifying', 'phase':'verify',
            'codex':{'one_file':True, 'reviewer_session':'reviewer', 'review_status':'failed'},
        }], 0, 15, False, 0, 1, ['plan_fix', 'install', 'verify'])
        install = failed_review.split('data-stage="install"', 1)[1].split('</li>', 1)[0]
        self.assertNotIn('wf-step done', install)

    def test_failed_or_blocked_lanes_never_complete_install_tick(self):
        for status in ('failed', 'blocked'):
            with self.subTest(status=status):
                page = renderer.render_workflow('verify', [{
                    'id':'A', 'status':status, 'phase':'verify',
                    'codex':{'reviewer_session':'reviewer', 'review_status':'passed'},
                }], 0, 15, False, 0, 1, ['plan_fix', 'install', 'verify'])
                install = page.split('data-stage="install"', 1)[1].split('</li>', 1)[0]
                self.assertNotIn('wf-step done', install)

    def test_merge_previous_pr_and_rollover_have_separate_thresholds(self):
        page = renderer.render_workflow('gate', [], 15, 15, True, 0, 1)
        self.assertIn('>Merge previous PR<', page)
        self.assertIn('Report at 15+ verified', page)
        self.assertIn('Rollover requires at least 16+ verified and the rollover threshold.', page)
        self.assertIn('Merge previous PR requires 16+ verified.', page)
        fifteen = [{'id': f'ISSUE-{i}', 'status': 'verified_fixed'} for i in range(15)]
        sixteen = fifteen + [{'id': 'ISSUE-16', 'status': 'verified_fixed'}]
        self.assertEqual(renderer.rollover_gate(fifteen, {}), (15, 15, True))
        self.assertEqual(renderer.merge_gate(fifteen, {}), (15, 16, False))
        self.assertEqual(renderer.merge_gate(sixteen, {}), (16, 16, True))
        merged = renderer.render_workflow('merged', [], 15, 15, True, 0, 1)
        self.assertIn('>Merged / Done<', merged)

    def test_timeline_preserves_results_in_date_order_including_runs(self):
        def evidence(build, timestamp, observed):
            return {'build':build, 'timestamp':timestamp, 'checked':'button update', 'expected':'both updates survive',
                    'observed':observed, 'verdict':'Fixed because both updates survived', 'check_id':timestamp,
                    'session':build,
                    'reference':'receipt'}
        item = {'id': 'ISSUE-01', 'verification_evidence': [
            evidence('ACU 0.2.120', '2026-11-07T12:00:00Z', 'old'),
            evidence('ACU 0.2.119', '2026-11-07T15:00:00Z', 'other'),
            evidence('ACU 0.2.120', 'unknown', 'undated')]}
        runs = [{'build':'ACU 0.2.120; Ledger 0.1.39', 'checks':{'ISSUE-01': {
            'state':'inconclusive', 'finished_at':'2026-11-08T06:00:00Z', 'evidence':'Checked: button update\nExpected: both updates survive\nObserved: latest'}}}]
        timeline = renderer.version_evidence(item, runs)
        self.assertEqual([(r['version'], r['verdict']) for r in timeline], [('0.2.120', 'Inconclusive because latest'), ('0.2.119', 'Fixed because both updates survived')])
        self.assertEqual(len(timeline[0]['history']), 2)
        self.assertEqual((timeline[0]['checked'], timeline[0]['expected'], timeline[0]['observed']),
                         ('button update', 'both updates survive', 'latest'))
        self.assertEqual(timeline[0]['state'], 'inconclusive')

    def test_verification_history_is_not_flattened_or_duplicated(self):
        item = self.data['findings'][0]
        item['verification_evidence'] = [{'checked':'Earlier check','expected':'success','observed':'Unique historical result',
            'verdict':'Fixed because it passed','check_id':'older-check','build':'ACU 1','session':'session','timestamp':'2026-11-08T00:00:00Z','reference':'receipt'}]
        before = copy.deepcopy(self.data)
        page = renderer.render(self.data, self.base)
        self.assertEqual(page.count('Unique historical result'), 1)
        for label in ("['Checked',record.checked]", "['Expected',record.expected]", "['Observed',record.observed]", "['Verdict because',record.verdict||record.outcome]"):
            self.assertIn(label, page)
        self.assertIn('Status of last verification', page)
        self.assertEqual(self.data, before)

    def test_two_checks_on_one_version_render_as_one_row_with_latest_fields(self):
        records = [
            {'checked':'both tracker updates','expected':'both survive','observed':'one survived',
             'verdict':'Failing because one update was lost','check_id':'check-1','build':'BCL 0.2.147','session':'check-1',
             'timestamp':'2026-11-15T10:00:00Z','reference':'old receipt /tmp/old'},
            {'checked':'both tracker updates','expected':'both survive','observed':'both survived on worktree and installed copy',
             'verdict':'Fixed because both updates survived','check_id':'check-2','build':'BCL 0.2.147','session':'check-2',
             'timestamp':'2026-11-16T10:00:00Z','reference':'new receipt /tmp/new'}]
        rows = renderer.version_evidence({'id':'ISSUE-01','verification_evidence':records}, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['checked'], rows[0]['expected'], rows[0]['observed'], rows[0]['verdict']),
                         ('both tracker updates','both survive','both survived on worktree and installed copy',
                          'Fixed because both updates survived'))
        self.assertEqual(len(rows[0]['history']), 1)

    def test_same_timestamp_checks_keep_unique_identity_and_latest_run_result(self):
        timestamp = '2026-11-16T10:00:00.000000Z'
        run = {'id':'run-1','build':'BCL 0.2.147','checks':{'ISSUE-01':{
            'state':'fixed','check_id':'check-new','finished_at':timestamp,
            'evidence':'Checked: two writers; Expected: both updates survive; Observed: both survived on the installed copy',
            'previous_checks':[{'state':'not_fixed','check_id':'check-old','finished_at':timestamp,
                'evidence':'Checked: two writers; Expected: both updates survive; Observed: only one survived'}]}}}
        rows = renderer.version_evidence({'id':'ISSUE-01'}, [run])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['check_id'], 'check-new')
        self.assertEqual(rows[0]['observed'], 'both survived on the installed copy')
        self.assertEqual(rows[0]['verdict'], 'Fixed because both survived on the installed copy')
        self.assertEqual([entry['check_id'] for entry in rows[0]['history']], ['check-old'])

    def test_latest_verification_uses_check_time_regardless_of_outcome(self):
        item = {'id': 'ISSUE-01'}
        runs = [
            {'build':'ACU 0.2.147', 'checks':{'ISSUE-01':{'state':'not_fixed','finished_at':'2026-11-15T10:00:00Z','evidence':'Still failing'}}},
            {'build':'ACU 0.2.148', 'checks':{'ISSUE-01':{'state':'fixed','finished_at':'2026-11-14T10:00:00Z','evidence':'Earlier pass'}}},
            {'build':'ACU 0.2.146', 'checks':{'ISSUE-01':{'state':'blocked','finished_at':'2026-11-15T09:00:00Z','evidence':'Blocked'}}},
        ]
        history = renderer.version_evidence(item, runs)
        self.assertEqual([(r['state'], r['build']) for r in history],
                         [('not_fixed','ACU 0.2.147'), ('blocked','ACU 0.2.146'), ('fixed','ACU 0.2.148')])

    def test_explicit_outcomes_and_exact_duplicate_checks(self):
        blocked = {'build':'ACU 0.2.147', 'timestamp':'2026-11-15T10:00:00Z', 'verdict':'blocked', 'checked':'Voice check', 'expected':'pass', 'observed':'blocked', 'check_id':'blocked-check', 'session':'latest'}
        older = {**blocked, 'timestamp':'2026-11-14T10:00:00Z', 'verdict':'notes without a state', 'check_id':'older-check', 'session':'older'}
        distinct = {**blocked, 'timestamp':'2026-11-13T10:00:00Z', 'reference':'different receipt', 'check_id':'distinct-check', 'session':'distinct'}
        history = renderer.version_evidence({'id':'ISSUE-01', 'verification_evidence':[blocked, blocked, distinct, older]}, [])
        self.assertEqual(len(history), 1)
        self.assertEqual([(r['state'], r['verdict']) for r in history[0]['history']], [('', 'notes without a state'), ('blocked', 'blocked')])

    def test_introspect_stage_renders_between_verify_and_report(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'introspect','updated_at':'2026-11-10T10:00:00Z'}}
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Introspect', page)
        self.assertLess(page.index('>Verify<'), page.index('>Introspect<'))
        self.assertLess(page.index('>Introspect<'), page.index('>Report &amp; roll over<'))

    def test_legacy_combined_stage_is_preserved_without_guessing_current_step(self):
        self.data.setdefault('tracking', {})['workflow'] = {'stage':'install_verify','updated_at':'2026-11-08T00:00:00Z'}
        page = renderer.render(self.data, self.base)
        self.assertIn('Install', page)
        self.assertIn('Verify', page)
        self.assertNotIn('aria-current="step"', page)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_stale_check_indicator_replaces_spinner(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        helper = next(line for line in template.splitlines() if line.startswith('function markStale('))
        program = helper + "\nconst el={textContent:'checking',classList:{remove(x){this.removed=x}}};"
        program += "markStale(el,{state:'checking',updated_at:'2000-01-01T00:00:00Z'});"
        program += "if(!el.textContent.includes('stale')||el.classList.removed!=='checking')process.exit(1);"
        subprocess.run(['node','-e',program],check=True)

    def test_rollover_excludes_only_archived_ids(self):
        original = self.data['findings'][0]
        other = copy.deepcopy(original)
        other['id'] = 'NEW-99'
        self.data['findings'].append(other)
        self.data.setdefault('tracking', {})['rollover'] = {'excluded_ids': [original['id']]}
        page = renderer.render(self.data, self.base)
        payload = page.split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0]
        ids = [item['id'] for item in json.loads(payload)]
        self.assertNotIn(original['id'], ids)
        self.assertIn('NEW-99', ids)
        self.assertIn(original, self.data['findings'])

    def setUp(self):
        self.data = json.loads((Path(__file__).parent.parent / "examples/findings.json").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_archive_and_parked_summaries_render_with_paths_and_needs(self):
        self.data["tracking"] = {
            "archive": {"count": 84, "path": "/tmp/list/archive.json"},
            "parked": {"count": 4, "path": "/tmp/list/parked.json",
                       "needs": ["real microphone", "device access"]},
        }
        page = renderer.render(self.data, self.base)
        self.assertIn("84 archived findings in /tmp/list/archive.json", page)
        self.assertIn("4 parked findings in /tmp/list/parked.json; needs: real microphone, device access", page)

    def test_installed_inventory_is_distinct_from_run_build(self):
        self.data['tracking'] = {'verification_run': {'id':'run1','build':'ACU 1','revision':'abc',
            'installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt','status':'complete',
            'checks': {'ISSUE-01': {'state':'inconclusive','evidence':'unknown','finished_at':'now'},
                       'ISSUE-02': {'state':'fixed','evidence':'passed','finished_at':'now'}}},
            'installed_plugins': {'observed_at':'today','source':'manifests','versions': {
                'batched-change-list':'0.1.17','agent-computer-use':'2','pstack':'9','remotion':'4','irctc-train-booking':'1'}}}
        page = renderer.render(self.data, self.base)
        self.assertIn('class="plugin-chip"', page)
        self.assertIn('batched-change-list 0.1.17', page)
        self.assertIn('agent-computer-use 2', page)
        self.assertNotIn('pstack', page)
        self.assertNotIn('remotion', page)
        self.assertNotIn('irctc-train-booking', page)
        self.assertNotIn('Installed plugins', page)
        self.assertNotIn('Build details', page)
        self.assertIn('ACU 1 · abc', page)
        self.assertLess(page.index('class="header-meta"'), page.index('</header>'))
        self.assertLess(page.index('plugin-chips'), page.index('</header>'))
        self.assertNotIn('{{BUILD_CARD}}', page)

    def test_valid_and_safe_serialization(self):
        self.data["title"] = '<title>{{FINDINGS_JSON}}</title>'
        self.data["findings"][0]["observed"] = '</script><script>alert(1)</script>'
        page = renderer.render(self.data, self.base)
        self.assertIn('&lt;title&gt;{{FINDINGS_JSON}}&lt;/title&gt;', page)
        self.assertIn(r'\u003c/script>', page)
        self.assertNotIn('</script><script>alert(1)</script>', page)

    def test_failing_finding_outcome_stays_separate_from_deferred_lane(self):
        finding = self.data['findings'][0]
        finding['status'] = 'failing'
        self.data['tracking'] = {'plan': {'round': 1, 'groups': [
            {'id': 'CORE', 'finding_ids': [finding['id']], 'status': 'deferred'}]}}
        page = renderer.render(self.data, self.base)
        payload = page.split('<script type="application/json" id="findings-data">', 1)[1].split('</script>', 1)[0]
        rendered = json.loads(payload)[0]
        self.assertEqual(rendered['status'], 'failing')
        self.assertEqual(rendered['_lane_status']['outcome'], 'Failing')
        self.assertEqual(rendered['_lane_status']['copy'], 'Parked → Round 2')
        status_expression = page.split('Issue status: ${', 1)[1].split('}`', 1)[0]
        program = f"const selected={{status:'failing',_lane_status:{{outcome:'Failing'}}}},labels={{failing:'Failing'}};process.stdout.write(`Issue status: ${{{status_expression}}}`)"
        self.assertEqual(subprocess.check_output(['node', '-e', program], text=True), 'Issue status: Failing')
        fallback = f"const selected={{status:'failing'}},labels={{failing:'Failing'}};process.stdout.write(`Issue status: ${{{status_expression}}}`)"
        self.assertEqual(subprocess.check_output(['node', '-e', fallback], text=True), 'Issue status: Failing')
        self.assertIn('.issue-heading{display:flex;align-items:center;gap:8px;flex-wrap:wrap}', page)

    def test_all_details_are_shown_without_mutating_source(self):
        item = self.data['findings'][0]
        item['verification'] = 'Verified observation.'
        item['details'] = {'First': 'Same finding', 'Duplicate': 'Same finding', 'Summary again': 'Verified observation.'}
        before = copy.deepcopy(self.data)
        page = renderer.render(self.data, self.base)
        self.assertIn('"First": "Same finding"', page)
        self.assertIn('"Duplicate": "Same finding"', page)
        self.assertIn('"Summary again": "Verified observation."', page)
        self.assertEqual(self.data, before)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_checking_issue_sorts_first_and_finished_issue_returns(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        sort = next(line for line in template.splitlines() if line.startswith('const findings='))
        program = '''const items=[{id:'A',status:'failing',check:{state:'not_fixed'}},
          {id:'B',status:'failing',check:{state:'checking'}},
          {id:'C',status:'verified_fixed',check:{state:'fixed'}}];
          const document={getElementById:()=>({textContent:JSON.stringify(items)})};\n'''
        program += sort + "\nif(findings.map(x=>x.id).join('')!=='BAC')process.exit(1);"
        program += "items[1].check.state='inconclusive';\n" + sort.replace('const findings=', 'const finished=')
        program += "\nif(finished.map(x=>x.id).join('')!=='ABC')process.exit(2);"
        subprocess.run(['node', '-e', program], check=True)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_in_progress_issue_sorts_after_checking_without_working_copy(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        sort = next(line for line in template.splitlines() if line.startswith('const findings='))
        program = """const items=[{id:'A',status:'failing'},{id:'B',status:'in_progress',check:{state:'not_fixed'}},
          {id:'C',status:'failing',check:{state:'checking'}},{id:'D',status:'verified_fixed'}];
          const document={getElementById:()=>({textContent:JSON.stringify(items)})};\n"""
        program += sort + "\nif(findings.map(x=>x.id).join('')!=='CBAD')process.exit(1);"
        subprocess.run(['node', '-e', program], check=True)
        self.assertIn("Number(b.status==='in_progress')", sort)
        self.assertNotIn('Working on it', template)

    def test_invalid_ids_status_and_missing_proof_fail(self):
        for mutation in ("duplicate", "status", "proof"):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(self.data)
                if mutation == "duplicate":
                    data["findings"][1]["id"] = data["findings"][0]["id"]
                elif mutation == "status":
                    data["findings"][0]["status"] = "fixed"
                else:
                    data["findings"][1]["verification_refs"] = []
                with self.assertRaises(ValueError):
                    renderer.render(data, self.base)

    def test_evidence_is_embedded_and_links_are_validated(self):
        (self.base / "crop.png").write_bytes(b"image bytes")
        self.data["findings"][0]["evidence"] = [{"src": "crop.png", "alt": "Crop", "caption": "Before", "full": "https://example.com/evidence"}]
        page = renderer.render(self.data, self.base)
        self.assertIn("data:image/png;base64,aW1hZ2UgYnl0ZXM=", page)
        self.data["findings"][0]["evidence"][0]["full"] = "javascript:alert(1)"
        with self.assertRaises(ValueError):
            renderer.render(self.data, self.base)

    def test_tracking_shows_linked_pr_and_worktree_safely(self):
        self.data["tracking"] = {
            "pull_request": {"label": "PR #87 <triage>", "url": "https://cursor.com/pr/87"},
            "worktree": "codex-samantha-issues-20261105",
        }
        page = renderer.render(self.data, self.base)
        self.assertIn("body{margin:0;background:var(--page);color:var(--ink)", page)
        self.assertIn('href="https://cursor.com/pr/87"', page)
        self.assertIn("PR #87 &lt;triage&gt;", page)
        self.assertIn('class="header-pr"', page)
        self.assertIn('class="header-worktree"', page)
        self.assertIn("codex-samantha-issues-20261105", page)
        self.assertIn('class="header-meta"', page)
        self.assertLess(page.index('header-context'), page.index('</header>'))
        self.assertGreater(page.index('</header>'), page.index('header-worktree'))
        self.assertNotIn('class="tracking"', page)
        self.assertNotIn('class="tracking-label"', page)
        self.assertNotIn('<span class="tracking-label">', page)
        self.data["tracking"]["pull_request"]["url"] = "javascript:alert(1)"
        with self.assertRaises(ValueError):
            renderer.render(self.data, self.base)

    def test_install_progress_preserves_fixed_status_and_validates_coverage(self):
        self.data['tracking'] = {'verification_run': {'id':'run1','build':'ACU 1','revision':'abc',
            'installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt','status':'active',
            'checks': {'ISSUE-01':{'state':'checking','started_at':'2026-11-06T00:00:00Z'},
                       'ISSUE-02':{'state':'pending'}}}}
        page = renderer.render(self.data, self.base)
        self.assertIn('Checking this install', page)
        self.assertIn('prefers-reduced-motion:reduce', page)
        self.assertIn("Number(a.status==='verified_fixed')", page)
        self.assertIn('"state": "pending"', page)
        self.data['tracking']['verification_run']['checks'].pop('ISSUE-02')
        with self.assertRaises(ValueError):
            renderer.render(self.data, self.base)


    def test_discovery_precedence_semver_and_nested_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots = {key: root / key for key in ('agents_plugins', 'codex_cache', 'agents_skills', 'cursor_cache', 'cursor_marketplaces')}
            def manifest(path, name, version):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'name': name, 'version': version}))
            manifest(roots['agents_plugins'] / 'demo/plugin.json', 'demo', '1.0.0')
            manifest(roots['codex_cache'] / 'vendor/demo/1.8.0/plugin.json', 'demo', '1.8.0')
            manifest(roots['codex_cache'] / 'vendor/demo/1.10.0/plugin.json', 'demo', '1.10.0')
            manifest(roots['agents_skills'] / 'only-skill/plugin.json', 'only-skill', '2.0.0')
            manifest(roots['cursor_cache'] / 'any/other/.cursor-plugin/plugin.json', 'nested-client', '9.9.9')
            now = renderer.datetime(2026, 11, 14, 7, 55, tzinfo=renderer.timezone.utc)
            inventory = renderer.discover_installed_plugins(roots, now)
            self.assertEqual(inventory['versions'], {'demo':'1.0.0', 'only-skill':'2.0.0'})
            self.assertEqual(inventory['sources']['demo'], 'agents plugins')
            self.assertEqual(inventory['observed_at'], now.isoformat())
            cache_roots = dict(roots)
            cache_roots['agents_plugins'] = root / 'empty-agents'
            cache_roots['agents_skills'] = root / 'empty-skills'
            manifest(cache_roots['codex_cache'] / 'vendor/cache-only/1.8.0/plugin.json', 'cache-only', '1.8.0')
            manifest(cache_roots['codex_cache'] / 'vendor/cache-only/1.10.0/plugin.json', 'cache-only', '1.10.0')
            manifest(cache_roots['cursor_cache'] / 'vendor/cursor-only/2.0.0/plugin.json', 'cursor-only', '2.0.0')
            manifest(cache_roots['cursor_marketplaces'] / 'marketplaces/store/plugins/market-only/plugin.json', 'market-only', '3.0.0')
            inventory = renderer.discover_installed_plugins(cache_roots, now)
            self.assertEqual(inventory['versions']['cache-only'], '1.10.0')
            self.assertEqual(inventory['sources']['cache-only'], 'codex cache')
            self.assertEqual(inventory['sources']['cursor-only'], 'cursor cache')
            self.assertEqual(inventory['sources']['market-only'], 'cursor marketplaces')

    def test_live_inventory_fills_missing_names_from_tracker(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'installed_plugins': {'versions': {'demo':'0.1.0', 'old-plugin':'3.0.0'},
            'observed_at':'2026-11-10T00:00:00Z','source':'saved inventory'}}
        data['tracking'] = {'installed_plugins': {'versions': {'batched-change-list':'0.1.0', 'pstack':'3.0.0', 'context-ledger':'3.0.0'},
            'observed_at':'2026-11-10T00:00:00Z','source':'saved inventory'}}
        live = {'versions': {'batched-change-list':'0.2.0', 'pstack':'4.0.0'}, 'sources': {'batched-change-list':'agents plugins', 'pstack':'agents plugins'},
                'observed_at':'2026-11-14T07:55:00+00:00','source':'installed copies'}
        page = renderer.render(data, Path(__file__).parents[1], live)
        self.assertIn('batched-change-list 0.2.0', page)
        self.assertIn('agents plugins', page)
        self.assertIn('context-ledger 3.0.0', page)
        self.assertIn('As recorded', page)
        self.assertNotIn('pstack', page)
        self.assertNotIn('batched-change-list 0.1.0', page)

    def test_now_argument_formats_updated_time_and_relative_age_in_ist(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'verify','updated_at':'2026-11-14T07:00:00Z',
            'now':'2026-11-14T07:30:00Z — 2026-11-14T07:35:00Z'}}
        now = renderer.datetime.fromisoformat('2026-11-14T07:55:00+00:00')
        page = renderer.render(data, Path(__file__).parents[1], now=now)
        self.assertIn('Updated <time datetime="2026-11-14T07:00:00+00:00"', page)
        self.assertIn('12:30 PM IST</time> · <span data-relative-time>55 min ago</span>', page)
        self.assertNotIn('1:25 PM IST', page)
        self.assertNotIn('workflow-now', page)
        self.assertNotIn('2026-11-14T07:35:00Z', page)
        self.assertEqual(renderer.render(data, Path(__file__).parents[1]), renderer.render(data, Path(__file__).parents[1]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'findings.json', root / 'report.html'
            source.write_text(json.dumps(data))
            subprocess.run(['python3', str(Path(__file__).with_name('render.py')), str(source), str(output),
                            '--now', '2026-11-14T07:55:00Z'], check=True, capture_output=True, text=True)
            self.assertIn('12:30 PM IST', output.read_text())
            self.assertNotIn('1:25 PM IST', output.read_text())



    def test_gate_excludes_introspection_records_and_meets_at_threshold(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'gate','updated_at':'2026-11-14T07:00:00Z'},
                            'cycle':2,'round':2,'rollover_threshold':15}
        def item(identifier, status='verified_fixed'):
            row = copy.deepcopy(data['findings'][0])
            row.update(id=identifier, status=status, verification='Verified on the installed build.',
                       verification_refs=['Example receipt'])
            return row
        # Introspection is a record, not a fix; actual page-tool fixes still count.
        introspect = item('WF-07', 'verified_fixed')
        introspect['details'] = {'fixed_in': 'Introspect'}
        data['findings'] = (
            [item(f'AGENT-{i:02d}') for i in range(1, 9)]
            + [item('PAGE-01'), item('PAGE-02'), item('TRACK-01'), item('FLOW-01'),
               item('PLAN-01'), item('RUN-01'), item('HOOK-01'), item('TEST-01'), item('BCL-01')]
            + [item('AGENT-18', 'untested'), introspect]
        )
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes <b>17/15</b> verified', page)
        self.assertIn('class="wf-band gate-met"', page)
        self.assertNotIn('class="wf-band gate-pending"', page)
        # 14 VF with an open page-tool finding stays short of the threshold and is not met.
        data['findings'] = [item(f'AGENT-{i:02d}') for i in range(1, 15)] + [item('PAGE-01', 'untested')]
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes <b>14/15</b> verified', page)
        self.assertIn('class="wf-band gate-pending"', page)
        self.assertNotIn('class="wf-band gate-met"', page)
        fixed, _, met = renderer.rollover_gate([item('WF-06', 'untested'), introspect], {})
        self.assertEqual(fixed, 0)
        self.assertFalse(met)
        # 15 product VF with leftover open page-tool and WF items: still met at the threshold.
        data['findings'] = (
            [item(f'AGENT-{i:02d}') for i in range(1, 16)]
            + [item('PAGE-01', 'untested'), item('BCL-01', 'failing'), item('WF-07', 'untested')]
        )
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes <b>15/15</b> verified', page)
        self.assertIn('class="wf-band gate-met"', page)
        self.assertNotIn('class="wf-band gate-pending"', page)
        # 16 of 15 with an extra open product finding is still met.
        data['findings'] = [item(f'ISSUE-{i:02d}') for i in range(1, 17)] + [item('ISSUE-99', 'untested')]
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes <b>16/15</b> verified', page)
        self.assertIn('class="wf-band gate-met"', page)




    def test_plan_round_fallback_and_partial_verify_status(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'workflow': {'stage':'verify','updated_at':'2026-11-14T09:00:00Z'},
                            'plan': {'round': 2, 'groups': [
                                {'id':'AC','finding_ids':['ISSUE-01','ISSUE-02'],'status':'done','verify':{'status':'partial','method':'Live check'}},
                                {'id':'E','finding_ids':['ISSUE-01'],'status':'done','verify':{'status':'verified','method':'Query'}},
                            ]}}
        page = renderer.render(data, self.base)
        self.assertIn('Round 2', page)
        plan_head = page.split('<div class="plan-head">', 1)[1].split('</div>', 1)[0]
        self.assertIn('<strong>Round 2</strong> · 1 lanes · each installs and verifies when ready', plan_head)
        self.assertIn('<b>1</b> verifying', plan_head)
        self.assertIn('class="grp verifying"', page)
        self.assertIn('Verifying', page)
        self.assertIn('class="grp verified"', page)
        self.assertIn('Verified', page)
        self.assertNotIn('No fix plan', page)


    def test_failed_lane_status_and_legacy_done_alias(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'plan': {'round': 1, 'groups': [
            {'id':'F','finding_ids':['ISSUE-01'],'status':'failed','phase':'verify'},
            {'id':'D','finding_ids':['ISSUE-02'],'status':'done','verify':{'status':'failed'}},
        ]}}
        page = renderer.render(data, self.base)
        self.assertIn('class="grp failed"', page)
        self.assertIn('Phase 4 of 4: Verify, failed', page)
        self.assertIn('Fix failed · F', page)
        self.assertIn('Fix failed · D', page)
        data['tracking']['plan']['groups'][0]['status'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'invalid status'):
            renderer.render(data, self.base)


    def test_lane_phase_comes_from_status_and_large_chips_show_one_count(self):
        group = {'id':'A','status':'installing','phase':'fix','finding_ids':['ISSUE-01']}
        self.assertEqual(renderer.plan_phase(group), 'install')
        track = renderer.row_phase_track(group, 'installing')
        self.assertIn('Phase 3 of 4: Install', track)
        workflow = renderer.render_workflow('plan_fix',
            [{**group, 'id':f'G{i}'} for i in range(6)], 0, 15, False, 0, 1)
        self.assertIn('aria-label="6 lanes"', workflow)
        self.assertIn('<span class="lc-more">6</span></span>', workflow)
        self.assertNotIn('<span class="lc-more">6</span>6</span>', workflow)


    def test_status_only_plan_update_clears_stale_phase(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'round':1,'plan':{'round':1,'groups':[
            {'id':'A','finding_ids':['ISSUE-01'],'status':'blocked','phase':'fix','blocked_reason':'no mic'}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, operations, page = root / 'findings.json', root / 'operations.json', root / 'page.html'
            source.write_text(json.dumps(data))
            operations.write_text(json.dumps([{'action':'update-plan-group','id':'A','patch':{'status':'installing'}}]))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('manage_findings.py')),
                                     'batch', str(source), str(operations), '--render', str(page)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            group = json.loads(source.read_text())['tracking']['plan']['groups'][0]
            self.assertEqual(group['status'], 'installing')
            self.assertNotIn('phase', group)


    def test_plan_rows_follow_lane_phase_and_legacy_status_aliases(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'workflow': {'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}, 'plan': {'round': 1, 'groups': [
            {'id':'A','name':'Ready group','finding_ids':['ISSUE-01'],'status':'installing','phase':'install','verify':{'status':'pending','method':'Unit tests'}},
            {'id':'B','name':'Still fixing','finding_ids':['ISSUE-02'],'status':'running','phase':'fix','codex':{'status':'running','model':'gpt-6-luna'}}]}}
        page = renderer.render(data, self.base)
        self.assertIn('class="grp installing"', page)
        self.assertIn('>Installing</span>', page)
        self.assertIn('class="grp running"', page)
        self.assertIn('Phase 3 of 4: Install', page)
        self.assertIn('data-stage="gate"', page)
        install = page.split('data-stage="install"', 1)[1].split('</li>', 1)[0]
        self.assertNotIn('wf-step done', install)
        self.assertIn('class="wf-step busy"', page)
        data['tracking']['plan']['groups'][0]['status'] = 'verified'
        data['tracking']['plan']['groups'][0].pop('phase')
        data['tracking']['plan']['groups'][0]['verify'] = {'status':'verified','method':'Unit tests'}
        page = renderer.render(data, self.base)
        self.assertIn('class="grp verified"', page)
        self.assertIn('Phase 4 of 4: Verify', page)
        self.assertIn('class="wf-step done"', page)
        data['tracking']['plan']['groups'][0].update(status='review', phase='review', verify={'status':'pending'})
        page = renderer.render(data, self.base)
        self.assertIn('class="grp review"', page)
        self.assertIn('>In review</span>', page)
        self.assertIn('Phase 2 of 4: Plan &amp; fix', page)
        data['tracking']['workflow']['stage'] = 'install'
        data['tracking']['plan']['groups'] = [
            {'id':'A','name':'Ready group','finding_ids':['ISSUE-01'],'status':'done','phase':'install','verify':{'status':'pending'}},
            {'id':'C','name':'Later group','finding_ids':['ISSUE-02'],'status':'pending','verify':{'status':'partial','fixed':0,'failed':1,'total':3}},
        ]
        page = renderer.render(data, self.base)
        self.assertIn('class="grp installing"', page)
        self.assertIn('class="grp planned"', page)
        self.assertNotIn('data-stage="install" class="wf-step current"', page)
        self.assertNotIn('data-stage="verify" class="wf-step current"', page)
        data['tracking']['plan']['groups'].append(
            {'id':'D','name':'Next-round group','finding_ids':['ISSUE-02'],'status':'deferred','phase':'deferred','depends_on':['A']}
        )
        page = renderer.render(data, self.base)
        self.assertNotIn('class="grp parked"', page)
        self.assertIn('Parked for Round 2', page)
        self.assertIn('D · 1', page)
        self.assertNotIn('>Failed</span>', page)




if __name__ == "__main__":
    unittest.main()
