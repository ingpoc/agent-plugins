import copy
import importlib.util
import json
import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("change_list_render", Path(__file__).with_name("render.py"))
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class RenderTests(unittest.TestCase):
    def test_recorded_plan_renders_deterministic_lanes_and_shared_merge(self):
        self.data['tracking'] = {'workflow': {'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}, 'plan': {'cycle': 1, 'round': 1, 'groups': [
            {'id':'A','finding_ids':['ISSUE-01'],'reason':'Shared cause','depends_on':[],'status':'running',
             'codex':{'model':'gpt-6-luna','status':'running','started_at':'2026-11-14T09:00:00Z'},
             'verify':{'method':'Live check','status':'pending'}},
            {'id':'B','finding_ids':['ISSUE-02'],'depends_on':['A'],'status':'pending'}]}}
        page = renderer.render(self.data, self.base)
        self.assertEqual(page, renderer.render(self.data, self.base))
        self.assertNotIn('setInterval(formatPageTimes', page)
        for marker in ('class="wf"', 'class="wf-band gate-pending"', 'class="wf-loop"', 'wf-gate', 'class="grp running"', 'class="tail"', 'after A', 'href="#ISSUE-01"', 'title="'):
            self.assertIn(marker, page)
        self.assertNotIn('plan-merge', page)
        self.assertIn('Fixing', page)
        self.assertIn('Planned', page)
        self.assertIn('No fix plan for Round 1 yet.', renderer.render({**self.data, 'tracking':{'workflow':{'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}}}, self.base))
        self.assertNotIn('class="plan-rows"', renderer.render({**self.data, 'tracking':{}}, self.base))

    def test_compact_plan_labels_duration_status_details_and_fallbacks(self):
        data = copy.deepcopy(self.data)
        data['tracking'] = {'workflow': {'stage':'plan_fix','updated_at':'2026-11-14T09:00:00Z'}, 'plan': {'round': 1, 'groups': [
            {'id':'A','name':'Render label','finding_ids':['ISSUE-01'],'status':'done','codex':{'model':'gpt-6-luna','effort':'medium','service_tier':'priority','duration_s':300,'started_at':'2026-11-14T09:00:00Z','ended_at':'2026-11-14T09:05:00Z'},'verify':{'status':'pending','method':'Run the focused test and inspect the live page carefully'}},
            {'id':'B','finding_ids':['ISSUE-02'],'status':'done','verify':{'status':'done','method':'Verified on runtime'}},
            {'id':'C','finding_ids':['ISSUE-01'],'status':'running','verify':{'status':'running','method':'Verify running'}},
            {'id':'D','finding_ids':['ISSUE-02'],'status':'blocked','verify':{'status':'pending','failure':'Codex job failed tests did not pass','method':'Blocked'}}]}}
        page = renderer.render(data, self.base)
        row = page.split('<details class="grp fixed">', 1)[1].split('</details>', 1)[0]
        self.assertIn('5 min', row); self.assertIn('Luna medium Fast', row)
        self.assertIn('Render label', row); self.assertIn('Fixed', row)
        self.assertIn('Run the focused test and inspect the live page carefully', page)
        self.assertIn('Failed', page); self.assertIn('Codex job failed tests did not pass', page)
        self.assertIn('Verifying', page); self.assertIn('Verified', page)
        self.assertIn('2:30 PM IST – 2:35 PM IST', page)
        self.assertNotRegex(row, r'20\d\d-\d\d-\d\dT')
        self.assertIn('2:30 PM IST – 2:35 PM IST', page)
        self.assertIn('Group B', page)


    def test_viewer_footer_is_opt_in_and_deterministic_without_metadata(self):
        plain = renderer.render(self.data, self.base)
        self.assertEqual(plain, renderer.render(self.data, self.base))
        self.assertNotIn('<p class="viewer-footer">', plain)
        live = renderer.render(self.data, self.base, viewer={'path':'/tmp/list.json','version':'0.1.17','started':'Nov 14, 2026, 3:00 PM IST'})
        self.assertIn('reading /tmp/list.json, viewer 0.1.17, started Nov 14, 2026, 3:00 PM IST', live)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_version_timeline_shows_status_and_evidence_together(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        self.assertIn("history.append(node('h3','Verification by version'))", template)
        self.assertIn("node('span',record.version,'version-chip')", template)
        self.assertIn("`Installed ${ist(record.timestamp)} · ${record.build}`", template)
        # Shown in the main card after Done when, never inside the collapsed Evidence section.
        self.assertLess(template.index("part('Done when'"), template.index("'Verification by version'"))
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
        self.assertIn("`Evidence (${evidenceCount} ${evidenceCount===1?'row':'rows'})`", template)
        self.assertNotIn('Agent details', template)
        self.assertNotIn('Additional details', template)
        self.assertIn("const detail=node('details',null,'evidence')", template)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_fingerprint_poll_reloads_once_only_after_change_and_preserves_view(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        save_view = next(line for line in template.splitlines() if line.startswith('const saveView='))
        refresh = next(line for line in template.splitlines() if line.startswith("if(location.protocol==='http:'"))
        self.assertIn("method:'HEAD'", refresh)
        self.assertIn('setInterval(refresh,5000)', refresh)
        self.assertNotIn("fetch(location.href,{cache:'no-store'})", refresh)
        program = """
let etag='one', reloads=0, saved=null, poll;
const search={value:'query',selectionStart:2,selectionEnd:4};
const evidence={open:true,querySelector(){return null}};
const document={hidden:false,activeElement:search};
const location={protocol:'http:',href:'http://example.test/',hash:'',reload(){reloads++}};
const history={replaceState(_state,_title,hash){location.hash=hash}};
const selected={hash:'#ISSUE-01'};
const nav={querySelector(){return selected}};
const panel={querySelector(){return evidence}};
const window={scrollX:12,scrollY:345};
const sessionStorage={setItem(_key,value){saved=value}};
const run={status:'complete'};
const fetch=async()=>({ok:true,headers:{get(){return etag}}});
const setInterval=fn=>{poll=fn};
""" + save_view + "\n" + refresh + "\n" + """
setImmediate(async()=>{
 await poll();
 if(reloads!==0)process.exit(1);
 await poll();
 if(reloads!==0)process.exit(2);
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
        self.assertIn('As recorded', page)
        self.assertIn('0.1.2', page)
        inventory = {'versions': {'batched-change-list':'0.1.3'},
                     'observed_at':'2026-11-10T01:00:00Z','source':'live inventory'}
        page = renderer.render(data, Path(__file__).parents[1], inventory)
        self.assertIn('Inventory supplied at render time', page)
        self.assertIn('0.1.3', page)
        self.assertNotIn('<strong>0.1.2</strong>', page)

    def test_workflow_times_render_localization_hooks_and_now_line(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'verify','updated_at':'2026-11-10T10:00:00Z',
                                         'now':'2026-11-10T10:00:00Z — Checking the installed build'}}
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertEqual(page.count('<time datetime='), 2)
        self.assertIn('class="workflow-now"', page)
        self.assertIn('Last reported', page)
        self.assertNotIn('Intl.RelativeTimeFormat', page)
        self.assertNotIn('setInterval(formatPageTimes', page)

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
        self.assertIn('Fixes 7 of 15', page)
        self.assertIn('Round 3', page)
        self.assertIn('under 15 fixes', page)
        self.assertIn('class="wf-band gate-met"', page)
        self.assertIn('class="wf-step done wf-gate"', page)
        self.assertIn('class="wf-exit"', page)
        self.assertIn('gate-met .wf-loop', page)
        timeline = page
        stages = ['collect','present','plan_fix','install','verify','introspect','gate','report_rollover']
        positions = [timeline.index(f'data-stage="{stage}"') for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(page.count('aria-label="Workflow progress"'), 1)
        self.assertEqual(renderer.render(data, Path(__file__).parents[1]), page)

    def test_timeline_selects_latest_dated_result_per_version_including_runs(self):
        item = {'id': 'ISSUE-01', 'verification_evidence': [
            {'build':'ACU 0.2.120', 'timestamp':'2026-11-07T12:00:00Z', 'outcome':'old', 'check':'check'},
            {'build':'ACU 0.2.119', 'timestamp':'2026-11-07T15:00:00Z', 'outcome':'other', 'check':'check'},
            {'build':'ACU 0.2.120', 'timestamp':'unknown', 'outcome':'undated', 'check':'check'}]}
        runs = [{'build':'ACU 0.2.120; Ledger 0.1.39', 'checks':{'ISSUE-01': {
            'state':'inconclusive', 'finished_at':'2026-11-08T06:00:00Z', 'evidence':'latest'}}}]
        timeline = renderer.latest_version_evidence(item, runs)
        self.assertEqual([(r['version'], r['outcome']) for r in timeline], [('0.2.120', 'latest'), ('0.2.119', 'other')])
        self.assertEqual(timeline[0]['state'], 'inconclusive')

    def test_verification_history_is_not_flattened_or_duplicated(self):
        item = self.data['findings'][0]
        item['verification_evidence'] = [{'check':'Earlier check','outcome':'Unique historical result',
            'build':'ACU 1','session':'session','timestamp':'2026-11-08T00:00:00Z','reference':'receipt'}]
        before = copy.deepcopy(self.data)
        page = renderer.render(self.data, self.base)
        self.assertEqual(page.count('Unique historical result'), 1)
        self.assertIn('Verification by version', page)
        self.assertEqual(self.data, before)

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

    def test_installed_inventory_is_distinct_from_run_build(self):
        self.data['tracking'] = {'verification_run': {'id':'run1','build':'ACU 1','revision':'abc',
            'installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt','status':'complete',
            'checks': {'ISSUE-01': {'state':'inconclusive','evidence':'unknown','finished_at':'now'},
                       'ISSUE-02': {'state':'fixed','evidence':'passed','finished_at':'now'}}},
            'installed_plugins': {'observed_at':'today','source':'manifests','versions': {'ACU':'2','Ledger':'3'}}}
        page = renderer.render(self.data, self.base)
        self.assertIn('ACU 1 · abc', page)
        self.assertIn('Installed plugins', page)
        self.assertIn('Build details', page)
        self.assertIn('Ledger <strong>3</strong>', page)
        self.assertLess(page.index('Installed plugins'), page.index('</header>'))
        self.assertNotIn('{{BUILD_CARD}}', page)

    def test_valid_and_safe_serialization(self):
        self.data["title"] = '<title>{{FINDINGS_JSON}}</title>'
        self.data["findings"][0]["observed"] = '</script><script>alert(1)</script>'
        page = renderer.render(self.data, self.base)
        self.assertIn('&lt;title&gt;{{FINDINGS_JSON}}&lt;/title&gt;', page)
        self.assertIn(r'\u003c/script>', page)
        self.assertNotIn('</script><script>alert(1)</script>', page)

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
    def test_in_progress_issue_sorts_after_checking_and_shows_working(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        sort = next(line for line in template.splitlines() if line.startswith('const findings='))
        program = """const items=[{id:'A',status:'failing'},{id:'B',status:'in_progress',check:{state:'not_fixed'}},
          {id:'C',status:'failing',check:{state:'checking'}},{id:'D',status:'verified_fixed'}];
          const document={getElementById:()=>({textContent:JSON.stringify(items)})};\n"""
        program += sort + "\nif(findings.map(x=>x.id).join('')!=='CBAD')process.exit(1);"
        subprocess.run(['node', '-e', program], check=True)
        self.assertIn("const working=item.status==='in_progress'", template)
        self.assertIn("if(!working&&run?.status==='active'&&item.check)", template)

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
        self.assertIn('<span class="tracking-label">Pull request</span>', page)
        self.assertIn('<span class="tracking-label">Worktree</span>', page)
        self.assertIn("codex-samantha-issues-20261105", page)
        self.assertIn('class="tracking"', page)
        self.data["tracking"]["pull_request"]["url"] = "javascript:alert(1)"
        with self.assertRaises(ValueError):
            renderer.render(self.data, self.base)

    def test_install_progress_preserves_fixed_status_and_validates_coverage(self):
        self.data['tracking'] = {'verification_run': {'id':'run1','build':'ACU 1','revision':'abc',
            'installed_at':'2026-11-06T00:00:00Z','install_evidence':'receipt','status':'active',
            'checks': {'ISSUE-01':{'state':'checking','started_at':'2026-11-06T00:00:00Z'},
                       'ISSUE-02':{'state':'pending'}}}}
        page = renderer.render(self.data, self.base)
        self.assertIn('Verification build', page)
        self.assertIn('Checking this install', page)
        self.assertIn('prefers-reduced-motion:reduce', page)
        self.assertIn("item.status==='verified_fixed'", page)
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
        live = {'versions': {'demo':'0.2.0'}, 'sources': {'demo':'agents plugins'},
                'observed_at':'2026-11-14T07:55:00+00:00','source':'installed copies'}
        page = renderer.render(data, Path(__file__).parents[1], live)
        self.assertIn('<strong>0.2.0</strong>', page)
        self.assertIn('agents plugins', page)
        self.assertIn('<strong>3.0.0</strong> <small>As recorded</small>', page)

    def test_now_argument_formats_both_workflow_times_in_ist(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'verify','updated_at':'2026-11-14T07:00:00Z',
            'now':'2026-11-14T07:30:00Z — 2026-11-14T07:35:00Z'}}
        now = renderer.datetime.fromisoformat('2026-11-14T07:55:00+00:00')
        page = renderer.render(data, Path(__file__).parents[1], now=now)
        self.assertIn('Last reported <time datetime="2026-11-14T12:30:00+05:30"', page)
        self.assertIn('Nov 14, 2026, 12:30 PM IST (55 minutes ago)</time>', page)
        self.assertIn('<strong>Now</strong> <time datetime="2026-11-14T07:55:00+00:00">1:25 PM IST</time>', page)
        self.assertNotIn('2026-11-14T07:35:00Z', page)
        self.assertEqual(renderer.render(data, Path(__file__).parents[1]), renderer.render(data, Path(__file__).parents[1]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'findings.json', root / 'report.html'
            source.write_text(json.dumps(data))
            subprocess.run(['python3', str(Path(__file__).with_name('render.py')), str(source), str(output),
                            '--now', '2026-11-14T07:55:00Z'], check=True, capture_output=True, text=True)
            self.assertIn('1:25 PM IST', output.read_text())



    def test_gate_excludes_page_tool_prefixes_and_meets_at_threshold(self):
        data = json.loads((Path(__file__).parents[1] / 'examples/findings.json').read_text())
        data['tracking'] = {'workflow': {'stage':'gate','updated_at':'2026-11-14T07:00:00Z'},
                            'cycle':2,'round':2,'rollover_threshold':15}
        def item(identifier, status='verified_fixed'):
            row = copy.deepcopy(data['findings'][0])
            row.update(id=identifier, status=status, verification='Verified on the installed build.',
                       verification_refs=['Example receipt'])
            return row
        # 8 product VF, 9 page-tool VF, leftover open product, plus a WF VF that must not count.
        data['findings'] = (
            [item(f'AGENT-{i:02d}') for i in range(1, 9)]
            + [item('PAGE-01'), item('PAGE-02'), item('TRACK-01'), item('FLOW-01'),
               item('PLAN-01'), item('RUN-01'), item('HOOK-01'), item('TEST-01'), item('BCL-01')]
            + [item('AGENT-18', 'untested'), item('WF-07', 'verified_fixed')]
        )
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes 8 of 15', page)
        self.assertIn('class="wf-band gate-pending"', page)
        self.assertNotIn('class="wf-band gate-met"', page)
        # 15 product VF with leftover open page-tool and WF items: met, never "not met".
        data['findings'] = (
            [item(f'AGENT-{i:02d}') for i in range(1, 16)]
            + [item('PAGE-01', 'untested'), item('BCL-01', 'failing'), item('WF-07', 'untested')]
        )
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes 15 of 15', page)
        self.assertIn('class="wf-band gate-met"', page)
        self.assertNotIn('class="wf-band gate-pending"', page)
        # 16 of 15 with an extra open product finding is still met.
        data['findings'] = [item(f'ISSUE-{i:02d}') for i in range(1, 17)] + [item('ISSUE-99', 'untested')]
        page = renderer.render(data, Path(__file__).parents[1])
        self.assertIn('Fixes 16 of 15', page)
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
        self.assertIn('class="grp partial"', page)
        self.assertIn('Partial', page)
        self.assertIn('class="grp verified"', page)
        self.assertIn('Verified', page)
        self.assertNotIn('No fix plan', page)



if __name__ == "__main__":
    unittest.main()
