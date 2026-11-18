#!/usr/bin/env python3
"""Generate the BCL workflow and skill loading chart."""
from __future__ import annotations

import argparse
import hashlib
import html
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


def definition(path=None):
    return json.loads((path or ROOT / "scripts/workflow_definition.json").read_text(encoding="utf8"))


def plain(value):
    return html.escape(re.sub(r"[`*]", "", str(value)))


def chart_svg(data, refs, verdicts, view):
    positions = {step["id"]: (40 + index * 240, 210) for index, step in enumerate(data["entry"])}
    positions.update({step["id"]: (40 + index * 240, 410) for index, step in enumerate(data["loop"])})
    exit_x = 40 + len(data["loop"]) * 240
    positions.update({step["id"]: (exit_x, 410 if index == 0 else 590) for index, step in enumerate(data["exit"])})
    boxes, parts = [], []
    def assert_fits(value, width, char_width=9):
        if len(value) * char_width > width:
            raise ValueError(f"chart label overflows node: {value!r}")
    def label(value, x, y, width, height=28, klass="edge-label", owner=None, target=None):
        value = plain(value)
        lines = value.split(", ") if klass == "command" else [value]
        if target:
            for line in lines: assert_fits(line, width, 7.2 if klass == "command" else 9)
        else:
            width = max(width, max(len(html.unescape(line)) for line in lines) * 7.2)
        boxes.append((x-width/2, y-height/2, width, height, value))
        attrs = f'data-box="{x-width/2},{y-height/2},{width},{height}"'
        if target:
            parts.append(f'<g role="button" tabindex="0" class="selectable" data-id="{plain(target)}" data-owner="{plain(owner or "")}" aria-label="{value}"><rect class="hit" x="{x-width/2}" y="{y-height/2}" width="{width}" height="{height}"/>')
        if len(lines) > 1:
            content = "".join(f'<tspan x="{x}" dy="{14 if i else 0}">{line}</tspan>' for i, line in enumerate(lines))
            parts.append(f'<text class="{klass}" text-anchor="middle" {attrs} x="{x}" y="{y-2}">{content}</text>')
        else:
            parts.append(f'<text class="{klass}" text-anchor="middle" {attrs} x="{x}" y="{y+5}">{value}</text>')
        if target: parts.append("</g>")
    parts.append('<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#57616d"/></marker></defs>')
    def edge(path, source="", target=""): parts.append(f'<path class="edge" data-from="{source}" data-to="{target}" d="{path}" marker-end="url(#arrow)"/>')
    for left, right in zip(data["entry"], data["entry"][1:]):
        x, y = positions[left["id"]]; nx, ny = positions[right["id"]]; edge(f"M{x+220} {y+40} H{nx}")
    first = data["loop"][0]
    x, y = positions[data["entry"][-1]["id"]]; nx, ny = positions[first["id"]]; edge(f"M{x+110} {y+80} V{ny-60} H{nx+110} V{ny}")
    for left, right in zip(data["loop"], data["loop"][1:]):
        x, y = positions[left["id"]]; nx, ny = positions[right["id"]]; edge(f"M{x+220} {y+40} H{nx}")
    gate = next(row for row in data["loop"] if row["id"] == "gate")
    gx, gy = positions[gate["id"]]
    edge(f"M{gx+220} {gy+40} C{gx+270} 500 {gx+260} 580 {gx+235} 580 H150 V490", "gate", "plan_fix")
    report = next(row for row in data["exit"] if row["id"] == "report_rollover")
    rx, ry = positions[report["id"]]
    edge(f"M{gx+220} {gy+40} V320 H{rx+110} V{ry}", "gate", "report_rollover")
    parts.append(f'<!-- {re.sub(r"[`*]", "", data["gate"]["loop_label"])} -->')
    parts.append(f'<!-- {re.sub(r"[`*]", "", data["gate"]["exit_label"])} · {re.sub(r"[`*]", "", data["gate"]["merge_label"])} -->')
    label(data["gate"]["loop_label"].format(threshold=data["gate"]["default_threshold"]), gx-310, 605, 360)
    label(data["gate"]["exit_label"].format(threshold=data["gate"]["default_threshold"]), rx-35, 300, 180)
    merge_from = data["transitions"]["merged"][0]
    exit_steps = {row["id"]: row for row in data["exit"]}
    merge_source, merge_target = exit_steps[merge_from], exit_steps["merged"]
    x, y = positions[merge_source["id"]]; nx, ny = positions[merge_target["id"]]
    edge(f"M{x+220} {y+40} H1550 V{ny+40} H{nx+220}", merge_from, "merged")
    for left, right in zip(data["exit"], data["exit"][1:]):
        if right["id"] == "merged": continue
        x, y = positions[left["id"]]; nx, ny = positions[right["id"]]; edge(f"M{x+110} {y+80} V{ny}")
    label(data["gate"]["merge_label"].format(threshold=data["gate"]["merge_previous_pr_threshold"]), gx-250, 700, 250)
    nodes = data["entry"] + data["loop"] + data["exit"]
    for step in nodes:
        x, y = positions[step["id"]]
        boxes.append((x, y, 220, 80, step["label"]))
        title = plain(step["label"])
        assert_fits(title, 220)
        parts.append(f'<g class="node selectable" role="button" tabindex="0" aria-label="{title}" id="{view}-{step["id"]}" data-id="{step["id"]}"><rect x="{x}" y="{y}" width="220" height="80" rx="12"/><text class="node-label" text-anchor="middle" x="{x+110}" y="{y+30}">{title}</text><text class="badge verdict" text-anchor="middle" x="{x+110}" y="{y+56}">{plain(verdicts.get(step["id"], "keep"))}</text></g>')
        if step["id"] == "merged":
            label("report-rollover.md", x+110, y+98, 220, klass="detail", owner=step["id"], target="report-rollover.md")
        for index, activity in enumerate(step.get("activities", [])):
            label(activity, x+110, y+98+index*32, 220, klass="detail", owner=step["id"], target=step["id"]+f"-activity-{index}")
        for ref in refs:
            if plain(ref.get("step", "")) == title:
                activity_offset = 32 * len(step.get("activities", []))
                if ref["path"]:
                    label(ref["path"], x+110, y+98+activity_offset, 220, klass="detail", owner=step["id"], target=ref["path"])
                if ref.get("run"):
                    label(ref["run"], x+110, y+140+activity_offset, 220, 36, klass="command", owner=step["id"], target=step["id"]+"-run")
    for i, a in enumerate(boxes):
        for b in boxes[i+1:]:
            if a[0] < b[0]+b[2] and b[0] < a[0]+a[2] and a[1] < b[1]+b[3] and b[1] < a[1]+a[3]:
                raise ValueError(f"chart labels overlap: {a[4]!r} and {b[4]!r}")
    return '<svg class="chart" viewBox="0 0 1840 790" role="img" aria-label="Batched Change List workflow">' + ''.join(parts) + '</svg>'


def render(skill_content=None, suggested_verdicts=None, definition_path=None):
    skill = (SKILL.read_bytes() if skill_content is None else skill_content).decode()
    data = definition(definition_path)
    verdicts = data.get("suggested_verdicts", {}) if suggested_verdicts is None else suggested_verdicts
    allowed = {"delete", "simplify", "optimize", "automate", "keep"}
    if not isinstance(verdicts, dict) or any(value not in allowed for value in verdicts.values()):
        raise ValueError("suggested verdicts must map node IDs to supported verdicts")
    refs = router(skill) + side_references(skill)
    definition_hash = hashlib.sha256((definition_path or ROOT / "scripts/workflow_definition.json").read_bytes()).hexdigest()
    fingerprint = source_fingerprint(skill.encode())
    svg = chart_svg(data, refs, verdicts, "current")
    after_svg = chart_svg(data, refs, verdicts, "after")
    refs_html = "".join(f'<button class="side-ref selectable" data-id="{plain(r["path"])}">{plain(r["path"])} · {plain(r["trigger"])}</button>' for r in refs if r["path"] in ("tracker.md", "viewer.md", "principles.md"))
    return f'''<!doctype html><html lang="en" data-source-hash="{fingerprint}" data-definition-hash="{definition_hash}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Batched Change List workflow statechart</title>
<style>:root{{font:15px/1.45 system-ui,sans-serif;color:#20242b;background:#f7f8fa}}body{{margin:2rem auto;padding:0 1rem;max-width:1200px}}h1{{margin-bottom:.25rem}}.tabs{{display:flex;gap:.5rem;margin:1rem 0}}button{{font:inherit;padding:.45rem .8rem;border:1px solid #9aa3ad;border-radius:.5rem;background:white;cursor:pointer}}button[aria-selected=true]{{background:#26364b;color:white}}.chart{{width:100%;min-width:1100px}}.stage{{display:inline-block;border:2px solid #17804b;background:#e3f5ea;border-radius:.7rem;padding:.65rem}}.selectable{{cursor:pointer}}.hit{{fill:transparent;stroke:transparent}}.selectable.selected rect{{stroke:#e3a320;stroke-width:4}}.selectable:focus-visible{{outline:3px solid #e3a320}}.side-ref{{display:block;margin:.5rem 0}}.side-ref.selected{{outline:3px solid #e3a320}}.edge{{fill:none;stroke:#57616d;stroke-width:2;marker-end:url(#arrow)}}.node rect{{fill:white;stroke:#66717d;stroke-width:2}}.node-label,.badge,.edge-label,.detail,.command{{text-anchor:middle;font-family:system-ui,sans-serif}}.node-label{{font-weight:650;font-size:17px}}.badge{{font-size:12px;fill:#485362;text-transform:uppercase}}.edge-label{{font-weight:600;font-size:14px;fill:#303843}}.detail{{font-size:12px;fill:#7542a8}}.command{{font:12px ui-monospace,monospace;fill:#59636e}}.side{{border:1px solid #c7cdd3;border-radius:1rem;padding:1rem;margin-top:1.5rem;background:#fff}}small{{color:#59636e}}.view[hidden]{{display:none}}@media(max-width:700px){{body{{overflow-x:auto}}}}</style><body>
<h1>Batched Change List</h1><p>Green loads with SKILL.md; purple references load on their trigger; grey labels are commands and checks.</p><div class="tabs" role="tablist"><button role="tab" aria-selected="true" data-view="current">Current</button><button role="tab" aria-selected="false" data-view="after">After</button></div>
<div class="stage always" id="skill"><b>SKILL.md</b><br><small>Always loaded rules and router</small></div><main id="flow"><section class="view current">{svg}</section><section class="view after" hidden>{after_svg}</section></main><section class="side"><strong>Side references</strong><div id="side">{refs_html}</div></section><small>Definition SHA-256: {definition_hash} · Source SHA-256: {fingerprint}</small>
<script>function select(el){{document.querySelectorAll('.selected').forEach(x=>x.classList.remove('selected'));el.classList.add('selected');if(el.dataset.id)document.querySelectorAll('[data-owner]').forEach(x=>{{if(x.dataset.owner===el.dataset.id)x.classList.add('selected')}})}}document.querySelectorAll('.selectable').forEach(el=>{{el.addEventListener('click',()=>select(el));if(el.tagName.toLowerCase()!=='button')el.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' '){{e.preventDefault();select(el)}}}})}});document.querySelectorAll('[role=tab]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('[role=tab]').forEach(x=>x.setAttribute('aria-selected',x===b));document.querySelectorAll('.view').forEach(x=>x.hidden=!x.classList.contains(b.dataset.view))}});</script></body></html>'''


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
