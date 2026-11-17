#!/usr/bin/env python3
"""Generate the BCL workflow and skill loading chart."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL, OUTPUT = ROOT / "SKILL.md", ROOT / "statechart.html"


def source_fingerprint(skill_content=None):
    h = hashlib.sha256()
    h.update(b"SKILL.md\n")
    h.update(SKILL.read_bytes() if skill_content is None else skill_content)
    for path in sorted((ROOT / "references").glob("*.md")):
        h.update(path.name.encode() + b"\n" + path.read_bytes())
    return h.hexdigest()


def router(skill_text):
    rows = []
    in_router = False
    for line in skill_text.splitlines():
        if line.startswith("|---"):
            continue
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if [cell.lower() for cell in cells] == ["step", "when", "load", "run"]:
                in_router = True
                continue
            if not in_router or len(cells) != 4:
                continue
            match = re.search(r"`?(references/[\w.-]+\.md)`?", cells[2])
            run = cells[3].strip()
            if len(run) >= 2 and run[0] == run[-1] == "`" and run.count("`") == 2:
                run = run[1:-1]
            rows.append({"step": cells[0], "trigger": cells[1], "path": match.group(1).split("/", 1)[1] if match else "", "run": run})
        else:
            in_router = False
    return rows


def side_references(skill_text):
    rows = []
    in_table = False
    for line in skill_text.splitlines():
        if line.startswith("|---"):
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if [cell.lower() for cell in cells] == ["when", "load"]:
            in_table = True
            continue
        if in_table and len(cells) == 2:
            match = re.search(r"`?(references/[\w.-]+\.md)`?", cells[1])
            if match:
                rows.append({"trigger": cells[0], "path": match.group(1).split("/", 1)[1], "run": ""})
    return rows


def definition():
    return json.loads((ROOT / "scripts/workflow_definition.json").read_text(encoding="utf8"))


def render(skill_content=None, suggested_verdicts=None):
    skill = (SKILL.read_bytes() if skill_content is None else skill_content).decode()
    data = definition()
    verdicts = data.get("suggested_verdicts", {}) if suggested_verdicts is None else suggested_verdicts
    allowed = {"delete", "simplify", "optimize", "automate", "keep"}
    if not isinstance(verdicts, dict) or any(value not in allowed for value in verdicts.values()):
        raise ValueError("suggested verdicts must map node IDs to supported verdicts")
    refs = router(skill) + side_references(skill)
    definition_hash = hashlib.sha256((ROOT / "scripts/workflow_definition.json").read_bytes()).hexdigest()
    fingerprint = source_fingerprint(skill.encode())
    payload = json.dumps({"entry": data["entry"], "loop": data["loop"], "exit": data["exit"], "gate": data["gate"], "refs": refs, "suggested_verdicts": verdicts}, ensure_ascii=False).replace("<", "\\u003c")
    return f'''<!doctype html><html lang="en" data-source-hash="{fingerprint}" data-definition-hash="{definition_hash}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Batched Change List workflow statechart</title>
<style>:root{{font:15px/1.45 system-ui,sans-serif;color:#20242b;background:#f7f8fa;--grey:#9199a1}}body{{margin:2rem auto;padding:0 1rem;max-width:1200px}}h1{{margin-bottom:.25rem}}.tabs{{display:flex;gap:.5rem;margin:1rem 0}}button{{font:inherit;padding:.45rem .8rem;border:1px solid #9aa3ad;border-radius:.5rem;background:white;cursor:pointer}}button[aria-selected=true]{{background:#26364b;color:white}}.flow,.sequence,.branches,.branch{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}}.sequence{{margin:.7rem 0}}.flow-connector{{margin:.2rem 0 .2rem 1rem;color:#57616d}}.stage{{border:2px solid #66717d;background:white;border-radius:.7rem;padding:.65rem;min-width:105px;text-align:center;cursor:pointer}}.always{{border-color:#17804b;background:#e3f5ea}}.ref{{border-color:#7542a8;background:#f0e8fa;display:block;margin:.4rem auto;max-width:230px}}.script{{border-color:var(--grey);background:#e8ebee;display:block;margin:.3rem auto;font:12px/1.3 ui-monospace,monospace;max-width:240px}}.arrow{{font-size:1.4rem;color:#57616d}}.gate-step{{display:flex;flex-direction:column;align-items:center}}.branches{{flex-direction:column;align-items:flex-start;margin-left:1rem}}.branch{{border-left:2px solid #8a939d;padding:.5rem 0 .5rem .8rem}}.branch-label{{font-weight:600}}.selected{{outline:4px solid #f4bd50;outline-offset:2px}}.step{{display:inline-flex;flex-direction:column;align-items:center}}.side{{border:1px solid #c7cdd3;border-radius:1rem;padding:1rem;margin-top:1.5rem;background:#fff}}.verdict{{font-size:11px;text-transform:uppercase;color:#485362}}small{{color:#59636e}}@media(max-width:650px){{.sequence,.branches{{justify-content:center}}}}</style><body>
<h1>Batched Change List</h1><p>Green loads with SKILL.md; purple references load on their trigger; grey labels are commands and checks.</p><div class="tabs" role="tablist"><button role="tab" aria-selected="true" data-view="current">Current</button><button role="tab" aria-selected="false" data-view="after">After</button></div>
<div class="stage always" id="skill"><b>SKILL.md</b><br><small>Always loaded rules and router</small></div><main id="flow"></main><section class="side"><strong>Side references</strong><div id="side"></div></section><small>Definition SHA-256: {definition_hash} · Source SHA-256: {fingerprint}</small><script type="application/json" id="chart-data">{payload}</script>
<script>const d=JSON.parse(document.querySelector('#chart-data').textContent),flow=document.querySelector('#flow'),side=document.querySelector('#side');let view='current';const verdictKinds='delete simplify optimize automate keep';const verdicts=d.suggested_verdicts||{{}};function node(label,id,kind='stage'){{const wrap=document.createElement('div');wrap.className='step';const button=document.createElement('button');button.className=kind;button.dataset.id=id;const title=document.createElement('b');title.textContent=label;button.append(title);if(view==='after'){{const badge=document.createElement('span');badge.className='verdict';badge.textContent=verdicts[id]||'keep';button.append(document.createElement('br'),badge)}}button.onclick=()=>{{document.querySelectorAll('.selected').forEach(x=>x.classList.remove('selected'));button.classList.add('selected');document.querySelectorAll('[data-owner="'+id+'"]').forEach(x=>x.classList.add('selected'))}};wrap.append(button);return wrap}}function addStep(parent,step,referenceOverride=null){{parent.append(node(step.label,step.id));const route=d.refs.find(x=>x.step===step.label),ref=referenceOverride||route||d.refs.find(x=>x.path===step.id.replace('_','-')+'.md')||d.refs.find(x=>x.path===step.id+'.md');if(ref){{const r=node(ref.path,ref.path,'ref');r.dataset.owner=step.id;r.title=ref.trigger;parent.append(r)}}const runText=route?.run||ref?.run;if(runText){{const run=node(runText,step.id+'-run','script');run.dataset.owner=step.id;parent.append(run)}}}}function arrow(parent){{const span=document.createElement('span');span.className='arrow';span.textContent='→';parent.append(span)}}function draw(){{flow.replaceChildren();const entry=document.createElement('div');entry.className='sequence';for(let i=0;i<d.entry.length;i++){{const box=document.createElement('div');addStep(box,d.entry[i]);entry.append(box);if(i<d.entry.length-1)arrow(entry)}}flow.append(entry);const connector=document.createElement('div');connector.className='flow-connector';connector.textContent='↓ into the Plan & fix loop';flow.append(connector);const loop=document.createElement('div');loop.className='sequence';for(let i=0;i<d.loop.length-1;i++){{const box=document.createElement('div');addStep(box,d.loop[i]);loop.append(box);arrow(loop)}}const gate=d.loop[d.loop.length-1],gateBox=document.createElement('div');gateBox.className='gate-step';addStep(gateBox,gate);const branches=document.createElement('div');branches.className='branches';const back=document.createElement('div');back.className='branch';back.innerHTML='<span class="branch-label">'+d.gate.loop_label.replaceAll('{{threshold}}',d.gate.default_threshold)+'</span> ↶ Plan &amp; fix';const exit=document.createElement('div');exit.className='branch';exit.innerHTML='<span class="branch-label">'+d.gate.exit_label.replaceAll('{{threshold}}',d.gate.default_threshold)+'</span> →';for(const step of d.exit){{const box=document.createElement('div');addStep(box,step,step.id==='merged'?d.refs.find(x=>x.path==='report-rollover.md'):null);if(step.id==='merged'){{const mergeLabel=document.createElement('small');mergeLabel.className='branch-label';mergeLabel.textContent=d.gate.merge_label.replaceAll('{{threshold}}',d.gate.merge_previous_pr_threshold);box.prepend(mergeLabel)}}exit.append(box);if(step!==d.exit[d.exit.length-1])arrow(exit)}}branches.append(back,exit);gateBox.append(branches);loop.append(gateBox);flow.append(loop);side.replaceChildren();for(const ref of d.refs.filter(x=>['tracker.md','viewer.md','principles.md'].includes(x.path))){{const n=node(ref.path+' · '+ref.trigger,ref.path,'ref');n.title=ref.trigger;side.append(n)}}}}document.querySelectorAll('[role=tab]').forEach(b=>b.onclick=()=>{{view=b.dataset.view;document.querySelectorAll('[role=tab]').forEach(x=>x.setAttribute('aria-selected',x===b));draw()}});draw();</script></body></html>'''


def is_current(path=OUTPUT, expected=None):
    return path.is_file() and path.read_text(encoding="utf8") == (render() if expected is None else expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        if not is_current():
            print(f"stale or missing: {OUTPUT}", file=sys.stderr)
            return 1
    else:
        OUTPUT.write_text(render(), encoding="utf8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
