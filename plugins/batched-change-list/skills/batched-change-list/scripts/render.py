#!/usr/bin/env python3
"""Validate findings JSON and render a standalone change-list page."""
import argparse
import base64
import html
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

STATUSES = {"verified_fixed", "failing", "untested", "in_progress", "awaiting_user_test",
            "awaiting_verification", "blocked", "investigation"}
SOURCES = {"User reported", "User requested", "Agent reported", "Telemetry lead", "Source hypothesis"}
CHECK_STATES = {"pending", "checking", "fixed", "not_fixed", "blocked", "inconclusive"}
WORKFLOW_STAGES = {"collect": "Collect", "present": "Present", "plan_fix": "Plan & fix", "install": "Install", "verify": "Verify", "introspect": "Introspect", "gate": "Fixes", "report_rollover": "Report & roll over"}
DEFAULT_ROLLOVER_THRESHOLD = 15
TEMPLATE = Path(__file__).resolve().parent.parent / "templates/change-list.html"


def looks_like_log_text(text):
    if len(text) > 240:
        return True
    return bool(re.search(
        r"(?:\b\w+=[^\s]+|\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b|"
        r"\b\d{2}:\d{2}:\d{2}\b|[`{}]|(?:^|\s)(?:[\w.-]+/)+[\w.-]+|"
        r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|html|css|sh|log|swift|md|yaml|yml)\b|"
        r"\b[a-z]+_[a-z0-9_]+\b|\b(?:npm run|python3?\s|curl\s|git\s|pytest\b|make\s))",
        text, re.I))


def first_plain_sentence(text):
    for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z\"“])", text):
        prefix = text[:match.start()].rstrip()
        if prefix.casefold().endswith(("e.g.", "i.e.")):
            continue
        return prefix, text[match.end():].strip()
    return text.strip(), ""


def latest_version_evidence(item, runs):
    candidates = [{"build": r["build"], "timestamp": r["timestamp"], "outcome": r["outcome"],
                   "check": r["check"], "state": ""} for r in item.get("verification_evidence", [])]
    for run in runs:
        check = run.get("checks", {}).get(item["id"], {})
        if check.get("state") in ("fixed", "not_fixed", "blocked", "inconclusive") and check.get("evidence"):
            candidates.append({"build": run.get("build", "unknown"), "timestamp": check.get("finished_at", "unknown"),
                               "outcome": check["evidence"], "check": "Installed verification", "state": check["state"]})
    latest = {}
    for index, record in enumerate(candidates):
        match = re.search(r"\b\d+\.\d+\.\d+\b", record["build"])
        version = match.group() if match else "Version unknown"
        try:
            when = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            timestamp = when.timestamp() if when.tzinfo else float("-inf")
        except (ValueError, OverflowError):
            timestamp = float("-inf")
        rank = (timestamp, index)
        if version not in latest or rank > latest[version][0]:
            latest[version] = (rank, {"version": version, **record})
    return [record for _, record in sorted(latest.values(), key=lambda entry: entry[0], reverse=True)]


def rollover_cycle(tracking):
    cycle = tracking.get("cycle", 1)
    if type(cycle) is not int or cycle < 1:
        raise ValueError("tracking.cycle must be a positive integer")
    return cycle


def rollover_gate(findings, tracking):
    threshold = tracking.get("rollover_threshold", DEFAULT_ROLLOVER_THRESHOLD)
    if type(threshold) is not int or threshold < 1:
        raise ValueError("tracking.rollover_threshold must be a positive integer")
    rollover = tracking.get("rollover", {})
    if not isinstance(rollover, dict):
        raise ValueError("tracking.rollover must be an object")
    excluded = rollover.get("excluded_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(item_id, str) for item_id in excluded):
        raise ValueError("tracking.rollover.excluded_ids must be a list of finding IDs")
    excluded = set(excluded)
    fixed = sum(item.get("status") == "verified_fixed" and item.get("id") not in excluded for item in findings)
    has_open = any(item.get("status") not in ("verified_fixed", "closed") for item in findings)
    return fixed, threshold, fixed >= threshold or not has_open


def required_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def safe_link(value, field):
    required_text(value, field)
    parsed = urlsplit(value)
    if (any(ord(character) < 32 for character in value) or "\\" in value
            or parsed.scheme not in ("", "https", "http")
            or (not parsed.scheme and value.startswith("//"))):
        raise ValueError(f"{field} must be a relative path or an HTTP(S) URL")
    return value


def load_inventory(path):
    inventory = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(inventory, dict):
        raise ValueError("inventory must be a JSON object")
    return inventory


def _semver_key(value):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$", value)
    if not match:
        return (-1, 0, 0, 0, ((1, value),))
    prerelease = match.group(4)
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split(".")) if prerelease else ()
    return tuple(int(match.group(i)) for i in (1, 2, 3)) + (0 if prerelease else 1, identifiers)



def discover_installed_plugins(roots=None, now=None):
    home = Path.home()
    if roots is None:
        roots = {
            "agents_plugins": home / ".agents/plugins",
            "codex_cache": home / ".codex/plugins/cache",
            "agents_skills": home / ".agents/skills",
            "cursor_cache": home / ".cursor/plugins/cache",
            "cursor_marketplaces": home / ".cursor/plugins/marketplaces",
        }
    current = now or datetime.now(timezone.utc)
    observed_at = current.isoformat()
    found = {}
    for key, root in roots.items():
        root = Path(root).expanduser()
        if not root.exists():
            continue
        manifests = ()
        if key in ("agents_plugins", "agents_skills"):
            manifests = root.glob("*/plugin.json")
        elif key == "codex_cache":
            manifests = root.glob("*/*/*/plugin.json")
        elif key == "cursor_cache":
            manifests = root.glob("**/plugin.json")
        elif key == "cursor_marketplaces":
            manifests = root.glob("**/plugins/*/plugin.json")
        for manifest in manifests:
            try:
                if manifest.parent.name in (".cursor-plugin", ".claude-plugin"):
                    continue
                package = json.loads(manifest.read_text(encoding="utf-8"))
                name, version = package.get("name"), package.get("version")
                if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
                    continue
                if key == "cursor_cache" and manifest.parent.name != name and manifest.parent.parent.name != name:
                    continue
                if key == "codex_cache" and manifest.parent.parent.name != name:
                    continue
                previous = found.get(name)
                priority = list(roots).index(key)
                semver = _semver_key(version)
                if previous is None or priority < previous[0] or priority == previous[0] and semver > previous[1]:
                    found[name] = (priority, semver, version, key.replace("_", " "))
            except (OSError, json.JSONDecodeError):
                continue
    return {"versions": {name: version for name, (_, _, version, _) in found.items()},
            "sources": {name: source for name, (_, _, _, source) in found.items()},
            "observed_at": observed_at, "source": "installed copies"}


def render_tracking(tracking, inventory_override=None):
    if not isinstance(tracking, dict):
        raise ValueError("tracking must be an object")
    parts = []
    pull_request = tracking.get("pull_request")
    if pull_request is not None:
        if not isinstance(pull_request, dict):
            raise ValueError("tracking.pull_request must be an object")
        label = html.escape(required_text(pull_request.get("label"), "tracking.pull_request.label"))
        url = html.escape(safe_link(pull_request.get("url"), "tracking.pull_request.url"), quote=True)
        parts.append(f'<a class="tracking-item" href="{url}"><span class="tracking-label">Pull request</span><strong>{label}</strong></a>')
    worktree = tracking.get("worktree")
    if worktree is not None:
        label = html.escape(required_text(worktree, "tracking.worktree"))
        parts.append(f'<div class="tracking-item"><span class="tracking-label">Worktree</span><strong>{label}</strong></div>')
    run = tracking.get("verification_run")
    build_card = ""
    if run is not None:
        if not isinstance(run, dict) or run.get("status") not in ("active", "complete"):
            raise ValueError("tracking.verification_run must have an active or complete status")
        if not isinstance(run.get("checks"), dict):
            raise ValueError("tracking.verification_run.checks must be an object")
        for field in ("id", "build", "revision", "installed_at", "install_evidence"):
            required_text(run.get(field), f"tracking.verification_run.{field}")
        build = html.escape(run["build"])
        revision = html.escape(run["revision"])
        installed = html.escape(run["installed_at"])
        build_card = f'<div class="tracking-item"><span class="tracking-label">Verification build</span><strong>{build} · {revision}</strong><span>Run installed {installed} · {run["status"]}</span></div>'
    recorded_inventory = inventory_override is None and tracking.get("installed_plugins") is not None
    inventory = inventory_override if inventory_override is not None else tracking.get("installed_plugins")
    if inventory_override is not None and not isinstance(inventory_override, dict):
        raise ValueError("inventory must be an object")
    stored_inventory = tracking.get("installed_plugins")
    if stored_inventory is not None and (not isinstance(stored_inventory, dict) or not isinstance(stored_inventory.get("versions"), dict)):
        raise ValueError("tracking.installed_plugins requires versions")
    if inventory_override is not None and inventory_override.get("sources") and stored_inventory:
        stored = stored_inventory["versions"]
        versions = dict(inventory_override["versions"])
        recorded_versions = {name: version for name, version in stored.items() if name not in versions}
        versions.update(recorded_versions)
        inventory = {**inventory_override, "versions": versions, "recorded_versions": recorded_versions}
    if inventory_override is not None and "sources" in inventory_override and not inventory_override.get("versions"):
        inventory = stored_inventory
        recorded_inventory = stored_inventory is not None

    if inventory is not None:
        if not isinstance(inventory, dict) or not isinstance(inventory.get("versions"), dict):
            raise ValueError("tracking.installed_plugins requires versions")
        observed = html.escape(required_text(inventory.get("observed_at"), "installed_plugins.observed_at"))
        source = html.escape(required_text(inventory.get("source"), "installed_plugins.source"))
        versions = []
        recorded_versions = inventory.get("recorded_versions", {})
        sources = inventory.get("sources", {})
        for name, version in inventory["versions"].items():
            label = ' <small>As recorded</small>' if name in recorded_versions else f' <small>{html.escape(sources[name])}</small>' if name in sources else ''
            versions.append(f'<span>{html.escape(required_text(name, "plugin name"))} <strong>{html.escape(required_text(version, "plugin version"))}</strong>{label}</span>')
        if not versions:
            versions.append("<span>No plugin versions recorded</span>")
        detail_rows = []
        if run is not None:
            detail_rows.append(f'<p>Verification run: {build} · {revision}<br>Installed {installed} · {run["status"]}</p>')
        detail_rows.append(f'<p>Inventory checked {observed} · {source}</p>')
        inventory_note = "As recorded" if recorded_inventory else "Live, read from installed copies at render time" if sources else "Inventory supplied at render time"
        build_card = (f'<div class="tracking-item"><span class="tracking-label">Installed plugins</span>'
                     f'<div class="plugin-versions">{"".join(versions)}</div><span class="inventory-note">{inventory_note}</span>'
                     f'<details class="build-details"><summary>Build details</summary>{"".join(detail_rows)}</details></div>')
    summary = '<div class="tracking" aria-label="Pull request and worktree">' + "".join(parts) + "</div>" if parts else ""
    return summary, build_card


def render(data, base, inventory_override=None, now=None):
    if not isinstance(data, dict):
        raise ValueError("document must be an object")
    title = required_text(data.get("title"), "title")
    tracking_data = data.get("tracking", {})
    workflow = tracking_data.get("workflow")
    if workflow is not None and (not isinstance(workflow, dict) or workflow.get("stage") not in (*WORKFLOW_STAGES, "install_verify", None) or not isinstance(workflow.get("updated_at"), str) or not workflow["updated_at"].strip()):
        raise ValueError("tracking.workflow requires a valid stage and updated_at")
    active = workflow.get("stage") if workflow else None
    cycle = rollover_cycle(tracking_data)
    ist = ZoneInfo("Asia/Kolkata")
    reported = datetime.fromisoformat(workflow["updated_at"].replace("Z", "+00:00")).astimezone(ist) if workflow else None
    time_html = (f'<time datetime="{html.escape(reported.isoformat(), quote=True)}" data-relative>'
                 f'{html.escape(reported.strftime("%b %-d, %Y, %-I:%M %p IST"))}</time>') if reported else ''
    now_html = ""
    if workflow:
        stored_now = required_text(workflow["now"], "tracking.workflow.now") if workflow.get("now") is not None else ""
        iso_pattern = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)"
        timestamps = re.findall(iso_pattern, stored_now)
        status = re.sub(iso_pattern, "", stored_now).strip(" \t—-·")
        current = now or (datetime.fromisoformat(timestamps[0].replace("Z", "+00:00")) if timestamps else reported)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        current_ist = current.astimezone(ist)
        now_html = (f'<p class="workflow-now"><strong>Now</strong> '
                    f'<time datetime="{html.escape(current.isoformat(), quote=True)}">'
                    f'{html.escape(current_ist.strftime("%-I:%M %p IST"))}</time>'
                    f'{" · " + html.escape(status) if status else ""}</p>')
    tracking, build_card = render_tracking(tracking_data, inventory_override)
    run = tracking_data.get("verification_run")
    progress = '<p id="verification-progress" role="status"></p>' if isinstance(run, dict) and run.get("status") == "active" else ""
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise ValueError("findings must be an array")
    seen = set()
    output = []
    for index, original in enumerate(findings):
        if not isinstance(original, dict):
            raise ValueError(f"findings[{index}] must be an object")
        item = dict(original)
        for field in ("id", "title", "source", "status", "observed", "recommendation"):
            required_text(item.get(field), f"findings[{index}].{field}")
        for field in ("summary", "impact", "done_when"):
            if field in item and not isinstance(item[field], str):
                raise ValueError(f"findings[{index}].{field} must be a string")
        if item["id"] in seen:
            raise ValueError(f"duplicate id: {item['id']}")
        seen.add(item["id"])
        if item["status"] not in STATUSES or item["source"] not in SOURCES:
            raise ValueError(f"{item['id']}: unsupported status or source")
        check = run["checks"].get(item["id"]) if run is not None else None
        if run is not None:
            if not isinstance(check, dict) or check.get("state") not in CHECK_STATES:
                raise ValueError(f"{item['id']}: verification check must belong to the current run")
            item["check"] = {**check, "run_id":run["id"]}
            state = check["state"]
            if state == "checking":
                required_text(check.get("started_at"), f"{item['id']}.check.started_at")
            if state in ("fixed", "not_fixed", "blocked", "inconclusive"):
                required_text(check.get("evidence"), f"{item['id']}.check.evidence")
                required_text(check.get("finished_at"), f"{item['id']}.check.finished_at")
            if run["status"] == "active" and state == "fixed" and item["status"] != "verified_fixed":
                raise ValueError(f"{item['id']}: fixed check requires verified_fixed status")
            if run["status"] == "active" and state == "not_fixed" and item["status"] == "verified_fixed":
                raise ValueError(f"{item['id']}: a failed check cannot retain verified_fixed status")
            if run["status"] == "complete" and state in ("pending", "checking"):
                raise ValueError(f"{item['id']}: completed run has unchecked issues")
        if not isinstance(item.get("verification", ""), str):
            raise ValueError(f"{item['id']}.verification must be a string")
        if item["status"] == "verified_fixed":
            required_text(item.get("verification"), f"{item['id']}.verification")
            refs = item.get("verification_refs")
            if not isinstance(refs, list) or not refs:
                raise ValueError(f"{item['id']}: verified_fixed requires verification_refs")
            for ref in refs:
                required_text(ref, f"{item['id']}.verification_refs")
        details = item.get("details", {})
        if not isinstance(details, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in details.items()):
            raise ValueError(f"{item['id']}.details must contain string keys and values")
        item["details"] = details
        if item.get("summary") is not None:
            item["evidence_text"] = item["observed"]
        else:
            candidate, remainder = first_plain_sentence(item["observed"])
            if candidate and not looks_like_log_text(candidate):
                item["summary"] = candidate
                item["evidence_text"] = remainder
            else:
                item["summary"] = ""
                item["evidence_text"] = item["observed"]
        item["impact"] = item.get("impact", "")
        if "done_when" not in item:
            verification = item.get("verification", "")
            if verification:
                item["done_when"] = (verification if item["status"] == "verified_fixed"
                    else re.sub(r"^Pending:\s*", "", verification))
            if not item.get("done_when"):
                item["done_when"] = next((value for key, value in details.items() if key.casefold() == "expected"), "")
            if looks_like_log_text(item.get("done_when", "")):
                item["done_when"] = ""  # log-like text stays in Evidence and the version timeline
        if run is not None and check.get("evidence"):
            if check.get("corrections"):
                details = {**details, "Check corrections": json.dumps(check["corrections"], ensure_ascii=False)}
            item["details"] = details
        records = item.get("verification_evidence", [])
        fields = ("check", "outcome", "build", "session", "timestamp", "reference")
        if not isinstance(records, list) or any(not isinstance(r, dict) or set(r) != set(fields) or any(not isinstance(r[k], str) or not r[k].strip() for k in fields) for r in records):
            raise ValueError(f"{item['id']}.verification_evidence has invalid records")
        item["version_timeline"] = latest_version_evidence(item, [*tracking_data.get("verification_history", []), *([run] if run else [])])
        item.pop("verification_evidence", None)
        if item["status"] == "verified_fixed":
            item["details"] = {**details, "Verification references": "\n".join(item["verification_refs"])}
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(f"{item['id']}.evidence must be an array")
        item["evidence"] = []
        for shot in evidence:
            if not isinstance(shot, dict):
                raise ValueError(f"{item['id']}: evidence must be an object")
            shot = dict(shot)
            for field in ("src", "alt", "caption"):
                required_text(shot.get(field), f"{item['id']}.evidence.{field}")
            source = Path(shot["src"])
            if source.is_absolute() or urlsplit(shot["src"]).scheme or shot["src"].startswith("//"):
                raise ValueError(f"{item['id']}: evidence src must be a relative local image")
            mime = mimetypes.guess_type(source.name)[0]
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError(f"{item['id']}: unsupported evidence image type")
            shot["src"] = f"data:{mime};base64," + base64.b64encode((base / source).read_bytes()).decode("ascii")
            if shot.get("full"):
                safe_link(shot["full"], f"{item['id']}.evidence.full")
            item["evidence"].append(shot)
        output.append(item)
    if run is not None and set(run["checks"]) != seen:
        raise ValueError("verification run checks must match the tracked issue IDs")
    template = TEMPLATE.read_text(encoding="utf-8")
    fixed_count, threshold, gate_met = rollover_gate(findings, tracking_data)
    rollover = tracking_data.get("rollover", {})
    excluded = rollover.get("excluded_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded) or not set(excluded) <= seen:
        raise ValueError("rollover excluded_ids must identify tracked findings")
    steps = []
    for index, (key, label) in enumerate(WORKFLOW_STAGES.items(), 1):
        name = f"Fixes {fixed_count} of {threshold}" if key == "gate" else label
        classes = "workflow-step" + (" active" if key == active else "")
        classes += (" gate-met" if gate_met else " gate-pending") if key == "gate" else ""
        current = ' aria-current="step"' if key == active else ""
        round_label = f'<small class="workflow-round">Round {cycle}</small>' if key == "gate" else ""
        steps.append(f'<li data-stage="{key}" class="{classes}"{current}><span>{index}</span>{html.escape(name)}{round_label}</li>')
    steps = "".join(steps)
    plan = tracking_data.get("plan") if isinstance(tracking_data, dict) else None
    lanes = ""
    if isinstance(plan, dict) and plan.get("cycle", cycle) == cycle and isinstance(plan.get("groups"), list):
        lane_rows = []
        for group in plan["groups"]:
            if not isinstance(group, dict):
                continue
            codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
            verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
            findings = ", ".join(html.escape(str(value)) for value in group.get("finding_ids", []))
            cells = [("Findings", findings or "None", group.get("status")),
                     ("Codex", codex.get("model", "Codex"), codex.get("status")),
                     ("Verify", verify.get("method", "Verification"), verify.get("status"))]
            boxes = "".join(f'<div class="plan-box"><strong>{label} · {html.escape(str(status or "pending"))}</strong><span>{html.escape(str(value))}</span>{f'<small>{html.escape(str(times))}</small>' if (times := " · ".join(str(x) for x in ((codex.get("started_at"), codex.get("ended_at"), codex.get("duration_s")) if label == "Codex" else (verify.get("started_at"), verify.get("ended_at"), verify.get("duration_s"))) if x is not None)) else ""}</div><span class="plan-arrow" aria-hidden="true">→</span>' for label, value, status in cells)
            reason_text = group.get("reason") or ("Depends on " + ", ".join(group["depends_on"]) if group.get("depends_on") else "")
            reason = f'<small class="plan-reason">{html.escape(str(reason_text))}</small>' if reason_text else ""
            lane_rows.append(f'<div class="plan-lane"><strong class="plan-lane-id">{html.escape(str(group.get("id", "?")))}</strong>{boxes}{reason}</div>')
        if lane_rows:
            lanes = '<div class="plan-lanes" aria-label="Recorded execution plan"><div class="plan-groups">' + "".join(lane_rows) + '</div><div class="plan-box plan-merge"><strong>Merge</strong></div></div>'
    loop_label = f"fewer than {threshold}"
    timeline = (f'<section class="workflow" aria-label="Workflow progress"><div class="workflow-heading"><strong>Workflow</strong><span>{"Last reported " + time_html if time_html else "No active step reported"}</span></div>'
                f'<ol>{steps}</ol><svg class="workflow-loop" role="img" aria-label="Fewer than {threshold} verified fixes returns to Plan and fix" viewBox="0 0 100 28" preserveAspectRatio="none"><defs><marker id="workflow-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0 0 L6 3 L0 6 z"/></marker></defs><path class="workflow-loop-path" d="M90 0 C90 26 35 26 35 0" marker-end="url(#workflow-arrow)"/><text class="workflow-loop-label" x="62" y="26">{html.escape(loop_label)}</text></svg>{now_html}{lanes}</section>')
    serialized = json.dumps([item for item in output if item["id"] not in excluded], ensure_ascii=True).replace("<", "\\u003c")
    run_view = {key: run[key] for key in ("id", "build", "revision", "installed_at", "status")} if run else None
    replacements = {"{{TITLE}}": html.escape(title), "{{FINDINGS_JSON}}": serialized,
                    "{{TRACKING}}": tracking, "{{BUILD_CARD}}": build_card, "{{TIMELINE}}": timeline,
                    "{{VERIFICATION_PROGRESS}}": progress,
                    "{{RUN_JSON}}": json.dumps(run_view, ensure_ascii=True).replace("<", "\\u003c")}
    return re.sub(r"\{\{TITLE\}\}|\{\{FINDINGS_JSON\}\}|\{\{TRACKING\}\}|\{\{BUILD_CARD\}\}|\{\{TIMELINE\}\}|\{\{VERIFICATION_PROGRESS\}\}|\{\{RUN_JSON\}\}",
                  lambda match: replacements[match.group()], template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="authoritative findings JSON")
    parser.add_argument("output", type=Path, help="output HTML")
    parser.add_argument("--inventory", type=Path, help="optional installed-plugin inventory JSON")
    parser.add_argument("--now", help="explicit render time as ISO 8601")
    parser.add_argument("--live", action="store_true", help="discover installed plugins and use the current time")
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve() or args.output.resolve() == TEMPLATE.resolve():
            raise ValueError("output must differ from input and template")
        data = json.loads(args.input.read_text(encoding="utf-8"))
        current = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc) if args.live else None
        inventory = load_inventory(args.inventory) if args.inventory else discover_installed_plugins(now=current) if args.live else None
        result = render(data, args.input.resolve().parent, inventory, current)
        args.output.write_text(result, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"render: {error}\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
