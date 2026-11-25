import hashlib, json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
import statechart
import workflow_definition

SKILL=Path(__file__).resolve().parents[1]/'SKILL.md'; ROOT=SKILL.parent; REFS=ROOT/'references'
EXPECTED={'collect.md':'New list or feedback','present.md':'After collection','plan-fix.md':'After Present or gate loop-back','review.md':"After a group's fix",'install.md':'After group review','verify.md':'After its install','introspect.md':"After every round's Verify",'gate.md':'After Introspect or checking rollover','report-rollover.md':'When gate is met','tracker.md':'Recovery','viewer.md':'Viewer use','release.md':'Before a release or after shipping'}
STEPS=[stage["label"] for stage in workflow_definition.STAGES]

class SkillLayoutTests(unittest.TestCase):
    def test_skill_body_contains_only_always_rules_router_and_validation(self):
        text=SKILL.read_text(); body=text.split('---',2)[2]
        self.assertNotRegex(body,r'(?m)^Workflow:|^\d+\. '); self.assertNotRegex(body,r'\b(?:15|16)\b')
        self.assertEqual(sum(line=='| Step | When | Load | Run |' for line in body.splitlines()),1); self.assertEqual(sum(line=='| When | Load |' for line in body.splitlines()),1)
        self.assertIn('\n\nSelf-validate:',body)
        for phrase in ('so the fix and its review stay separate','so model and effort stay in one place','so the list and its history have one writer','Editing this skill? Read [principles.md](principles.md) first','Every “already present” item needs its own named test','so the gate count is the number the owner sees','Run Introspect after every Verify','keeps its first port','python3 scripts/statechart.py','python3 -m unittest discover'):
            self.assertIn(phrase,body)

    def test_router_names_each_step_and_side_reference_once_and_all_references(self):
        text=SKILL.read_text(); rows=statechart.router(text); side=statechart.side_references(text)
        self.assertEqual([r['step'] for r in rows],STEPS)
        self.assertEqual([r['path'] for r in rows],list(EXPECTED)[:9]+[''])
        self.assertEqual({r['path']:r['trigger'] for r in rows+side if r['path']},EXPECTED)
        self.assertEqual(rows[2]['run'],'`bcl plan`, `bcl job --phase fix`')
        for p in REFS.glob('*.md'): self.assertEqual(sum(r['path']==p.name for r in rows+side),1,p.name)

    def test_description_covers_every_use_case(self):
        desc=SKILL.read_text().split('description:',1)[1].splitlines()[0].lower()
        for term in ('one at a time','build-install-test','rollover','merging','view the change-list page'): self.assertIn(term,desc)

    def test_after_chart_uses_suggested_verdict_and_defaults_to_keep(self):
        chart=statechart.render(suggested_verdicts={'collect':'delete'})
        self.assertEqual(chart.count('data-id="collect"'),2)
        self.assertEqual(chart.count('>delete</text>'),2)
        self.assertEqual(chart.count('>keep</text>'),18)

    def test_merge_chart_uses_merge_run_and_report_reference(self):
        chart=statechart.render()
        self.assertIn('bcl merged</tspan>',chart)
        self.assertIn('bcl install-local</tspan>',chart)
        self.assertNotIn('`',chart)
        self.assertIn('report-rollover.md',chart)

    def test_flow_chart_has_svg_edges_and_current_after_toggle(self):
        chart=statechart.render()
        for term in ('Collect','Present','Plan &amp; fix','Review','Pre-push suite','under 15 verified','15+ verified','Report &amp; roll over','Merge previous PR','16+ verified for Merge previous PR','marker-end="url(#arrow)"','data-view="current"','data-view="after"','class="badge verdict"','keep','bcl plan</tspan>','bcl job --phase fix</tspan>','bcl job --phase review','bcl merged</tspan>','bcl install-local</tspan>'):
            self.assertIn(term,chart)

    def test_evals_shape_and_router_paths_cover_three_scenarios(self):
        path=ROOT/'evals/evals.json'; data=json.loads(path.read_text()); self.assertEqual(set(data),{'evals'}); rows=data['evals']; self.assertEqual(len(rows),3)
        self.assertEqual([r['reference'] for r in rows],['references/collect.md','references/gate.md','references/plan-fix.md'])
        for row in rows: self.assertTrue(row['prompt'] and row['trigger'] and row['next'])
        self.assertIn('15',rows[1]['prompt']); self.assertIn('depends',rows[2]['prompt'])

    def test_comparison_helper_detects_regression_without_live_model(self):
        frozen=Path('/Users/gurusharan/.agents/artifacts/bcl-115/frozen-0.1.37')
        if not frozen.exists(): self.skipTest('frozen 0.1.37 unavailable')
        p=subprocess.run([sys.executable,str(ROOT/'scripts/compare_skill.py'),str(ROOT),str(frozen)],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_comparison_helper_fails_on_removed_router_and_evals(self):
        frozen=Path('/Users/gurusharan/.agents/artifacts/bcl-115/frozen-0.1.37')
        if not frozen.exists(): self.skipTest('frozen 0.1.37 unavailable')
        import compare_skill
        with tempfile.TemporaryDirectory() as tmp:
            regressed=Path(tmp)/'skill'; shutil.copytree(ROOT,regressed)
            skill=regressed/'SKILL.md'; skill.write_text(skill.read_text().replace('| Step | When | Load | Run |','| When | Load | Run |',1))
            (regressed/'evals/evals.json').unlink()
            self.assertLess(compare_skill.score(regressed),compare_skill.score(ROOT))
            result=subprocess.run([sys.executable,str(ROOT/'scripts/compare_skill.py'),str(regressed),str(frozen)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)

    def test_shape_tool_lookup_uses_skill_refine_install(self):
        tool=Path(os.environ.get('SKILL_SHAPE_PATH', Path.home()/'.agents/skills/skill-refine/scripts/skill_shape.py'))
        if not tool.exists(): self.skipTest('skill_shape.py unavailable')
        p=subprocess.run([sys.executable,str(tool),'--strict',str(ROOT)],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_generated_chart_hashes_and_missing_or_wrong_hash_are_stale(self):
        chart=ROOT/'statechart.html'; rendered=statechart.render(); self.assertTrue(statechart.is_current(chart))
        self.assertRegex(rendered,r'data-source-hash="[0-9a-f]{64}"'); self.assertRegex(rendered,r'data-definition-hash="[0-9a-f]{64}"')
        digest=hashlib.sha256(b'SKILL.md\n'+SKILL.read_bytes()+b''.join(p.name.encode()+b'\n'+p.read_bytes() for p in sorted(REFS.glob('*.md')))).hexdigest(); self.assertIn(f'data-source-hash="{digest}"',rendered)
        dh=hashlib.sha256((ROOT/'scripts/workflow_definition.json').read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'chart.html'; p.write_text(rendered.replace(f'data-definition-hash="{dh}"','data-definition-hash="'+'0'*64+'"')); self.assertFalse(statechart.is_current(p))
            p.write_text(re.sub(r' data-definition-hash="[^"]+"','',rendered)); self.assertFalse(statechart.is_current(p))
            self.assertFalse(statechart.is_current(Path(tmp)/'missing.html'))

if __name__=='__main__': unittest.main()
