#!/usr/bin/env python3
"""Generate the skill loading statechart from SKILL.md's trigger links."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
OUTPUT = ROOT / "statechart.html"
LINK = re.compile(r"\[([^\]]+)\]\(references/([^\)]+)\)")


def references(skill_text: str) -> list[dict[str, str]]:
    entries = []
    for line in skill_text.splitlines():
        if not line.startswith("|") or re.match(r"\|\s*:?-+", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0].lower() == "when":
            continue
        for path in re.findall(r"references/([A-Za-z0-9_.-]+\.md)", cells[1]):
            entries.append({"trigger": cells[0], "label": Path(path).stem.replace("-", " ").title(), "path": path})
    return entries


def source_fingerprint(skill_content: bytes | None = None) -> str:
    h = hashlib.sha256()
    h.update(b"SKILL.md\n")
    h.update(SKILL.read_bytes() if skill_content is None else skill_content)
    for path in sorted((ROOT / "references").glob("*.md")):
        h.update(path.name.encode() + b"\n")
        h.update(path.read_bytes())
    return h.hexdigest()


def workflow_steps() -> list[str]:
    definition = json.loads((ROOT / "scripts/workflow_definition.json").read_text(encoding="utf-8"))
    return [step["label"] for step in definition["entry"] + definition["loop"] + definition["exit"]]


def render(skill_content: bytes | None = None) -> str:
    skill_content = SKILL.read_bytes() if skill_content is None else skill_content
    skill_text = skill_content.decode("utf-8")
    fingerprint = source_fingerprint(skill_content)
    workflow = json.dumps(workflow_steps(), ensure_ascii=False)
    data = json.dumps(references(skill_text), ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'''<!doctype html>
<html lang="en" data-source-hash="{fingerprint}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Batched Change List skill loading statechart</title>
<style>
:root{{color-scheme:light dark;font:16px/1.5 system-ui,sans-serif;--green:#17804b;--purple:#7542a8;--muted:#77808a;--line:#aeb6bf}}
body{{max-width:1120px;margin:2rem auto;padding:0 1rem}}h1{{font-size:1.7rem}}h2{{font-size:1.1rem}}.views{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}section{{border:1px solid var(--line);border-radius:12px;padding:1rem}}.node{{border:2px solid;border-radius:9px;padding:.65rem;margin:.5rem 0}}.always{{border-color:var(--green);background:color-mix(in srgb,var(--green) 12%,transparent)}}.trigger{{border-color:var(--purple);background:color-mix(in srgb,var(--purple) 12%,transparent)}}.idle{{border-color:var(--line);color:var(--muted)}}.arrow{{color:var(--muted);margin:.2rem .7rem}}label{{display:block;margin:.7rem 0 .25rem}}select{{font:inherit;max-width:100%;padding:.4rem}}small{{color:var(--muted)}}@media(max-width:700px){{.views{{grid-template-columns:1fr}}}}
</style>
<body><h1>Batched Change List skill loading</h1><p>Green items load with the skill. Purple references load only when their trigger fires.</p><small>SKILL.md SHA-256: {fingerprint}</small>
<main class="views"><section><h2>Current</h2><div class="node always"><strong>SKILL.md</strong><br><small>Always loaded · trigger router</small></div><ol id="workflow"></ol><div class="arrow">↓ awaiting a workflow trigger</div><div id="current-refs"></div></section>
<section><h2>After trigger</h2><label for="trigger">Choose a trigger</label><select id="trigger"></select><div class="node always"><strong>SKILL.md</strong><br><small>Always loaded · trigger router</small></div><div class="arrow">↓ selected reference(s) load</div><div id="after-refs"></div></section></main>
<script type="application/json" id="references">{data}</script><script type="application/json" id="workflow">{workflow}</script>
<script>
const refs=JSON.parse(document.querySelector('#references').textContent), steps=JSON.parse(document.querySelector('#workflow').textContent), select=document.querySelector('#trigger');
for(const label of steps){{const li=document.createElement('li');li.textContent=label;document.querySelector('#workflow').append(li)}}
const triggers=[...new Set(refs.map(x=>x.trigger))];
for(const trigger of triggers){{const option=document.createElement('option');option.textContent=trigger;select.append(option)}}
function node(item,active){{const div=document.createElement('div');div.className='node '+(active?'trigger':'idle');div.textContent=item.label+' · references/'+item.path;return div}}
function update(){{const chosen=select.value;const current=document.querySelector('#current-refs'),after=document.querySelector('#after-refs');current.replaceChildren();after.replaceChildren();for(const item of refs){{current.append(node(item,false));after.append(node(item,item.trigger===chosen))}}}}
select.addEventListener('change',update);update();
</script></body></html>
'''


def is_current(path: Path = OUTPUT, expected: str | None = None) -> bool:
    return path.is_file() and path.read_text(encoding="utf-8") == (render() if expected is None else expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the generated chart is absent or stale")
    args = parser.parse_args()
    if args.check:
        if not is_current():
            print(f"stale or missing: {OUTPUT}", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
