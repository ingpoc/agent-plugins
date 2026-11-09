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
    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_version_timeline_shows_status_and_evidence_together(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        timeline = next(line for line in template.splitlines() if line.startswith(' const records=selected.version_timeline'))
        program = "const node=(tag,text)=>({tag,text,children:[],append(...nodes){this.children.push(...nodes)}});"
        program += "const article=node('article');const selected={version_timeline:[{version:'0.2.118',state:'fixed',outcome:'Live proof',check:'check',timestamp:'today',build:'ACU'}]};"
        program += timeline + "\nconst summary=article.children[0].children[1].children[0].children[2].children[0];if(summary.children[0].tag!=='strong'||summary.children[0].text!=='Fixed: '||summary.children[1].text!=='Live proof')process.exit(1);"
        subprocess.run(['node', '-e', program], check=True)

    @unittest.skipUnless(shutil.which('node'), 'node is needed to execute page JavaScript')
    def test_latest_check_reason_and_previous_result_are_labeled(self):
        template = (Path(__file__).parent.parent / 'templates/change-list.html').read_text()
        helper = next(line for line in template.splitlines() if line.startswith('function latestResult('))
        program = helper + "\nconst previous={version:'0.2.119',state:'fixed',outcome:'prior proof'};"
        program += "for(const state of ['fixed','not_fixed','inconclusive','blocked']){const x=latestResult({check:{state,evidence:'current reason'},version_timeline:[previous]});if(x.reason!=='current reason'||!x.label.startsWith('Latest check'))process.exit(1)}"
        program += "const p=latestResult({check:{state:'pending'},version_timeline:[previous]});if(p.reason!=='prior proof'||!p.label.includes('Previous check')||!p.label.includes('0.2.119'))process.exit(1);"
        program += "if(!latestResult({check:{state:'pending'}}).label.includes('No completed check'))process.exit(1);"
        subprocess.run(['node', '-e', program], check=True)

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

    def test_repeated_detail_is_shown_once_without_mutating_source(self):
        item = self.data['findings'][0]
        item['verification'] = 'Verified observation.'
        item['details'] = {'First': 'Same finding', 'Duplicate': 'Same finding', 'Summary again': 'Verified observation.'}
        before = copy.deepcopy(self.data)
        page = renderer.render(self.data, self.base)
        self.assertIn('"First": "Same finding"', page)
        self.assertNotIn('"Duplicate": "Same finding"', page)
        self.assertNotIn('"Summary again": "Verified observation."', page)
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
        self.assertIn("body{margin:0;background:Canvas;color:var(--ink)", page)
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


if __name__ == "__main__":
    unittest.main()
