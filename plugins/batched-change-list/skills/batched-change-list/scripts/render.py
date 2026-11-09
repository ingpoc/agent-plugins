#!/usr/bin/env python3
"""Validate findings JSON and render a standalone change-list page."""
import argparse
import base64
import html
import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

STATUSES = {"verified_fixed", "failing", "untested", "in_progress", "awaiting_user_test",
            "awaiting_verification", "blocked", "investigation"}
SOURCES = {"User reported", "User requested", "Agent reported", "Telemetry lead", "Source hypothesis"}
CHECK_STATES = {"pending", "checking", "fixed", "not_fixed", "blocked", "inconclusive"}
WORKFLOW_STAGES = {"collect": "Collect", "present": "Present", "plan_fix": "Plan & fix", "install": "Install", "verify": "Verify", "report_rollover": "Report & roll over"}
TEMPLATE = Path(__file__).resolve().parent.parent / "templates/change-list.html"


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


def render_tracking(tracking):
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
    inventory = tracking.get("installed_plugins")
    if inventory is not None:
        if not isinstance(inventory, dict) or not isinstance(inventory.get("versions"), dict) or not inventory["versions"]:
            raise ValueError("tracking.installed_plugins requires versions")
        observed = html.escape(required_text(inventory.get("observed_at"), "installed_plugins.observed_at"))
        source = html.escape(required_text(inventory.get("source"), "installed_plugins.source"))
        versions = []
        for name, version in inventory["versions"].items():
            versions.append(f'<span>{html.escape(required_text(name, "plugin name"))} <strong>{html.escape(required_text(version, "plugin version"))}</strong></span>')
        if run is None:
            raise ValueError("installed_plugins requires a verification run")
        build_card = (f'<div class="tracking-item"><span class="tracking-label">Installed plugins</span>'
                     f'<div class="plugin-versions">{"".join(versions)}</div><span class="inventory-note">Inventory · see build details</span>'
                     f'<details class="build-details"><summary>Build details</summary>'
                     f'<p>Verification run: {build} · {revision}<br>Installed {installed} · {run["status"]}</p>'
                     f'<p>Inventory checked {observed} · {source}</p></details></div>')
    summary = '<div class="tracking" aria-label="Pull request and worktree">' + "".join(parts) + "</div>" if parts else ""
    return summary, build_card


def render(data, base):
    if not isinstance(data, dict):
        raise ValueError("document must be an object")
    title = required_text(data.get("title"), "title")
    tracking_data = data.get("tracking", {})
    workflow = tracking_data.get("workflow")
    if workflow is not None and (not isinstance(workflow, dict) or workflow.get("stage") not in (*WORKFLOW_STAGES, "install_verify", None) or not isinstance(workflow.get("updated_at"), str) or not workflow["updated_at"].strip()):
        raise ValueError("tracking.workflow requires a valid stage and updated_at")
    active = workflow.get("stage") if workflow else None
    steps = ''.join('<li class="workflow-step' + (' active" aria-current="step"' if key == active else '"') + f'><span>{index}</span>{html.escape(label)}</li>' for index, (key, label) in enumerate(WORKFLOW_STAGES.items(), 1))
    timestamp = html.escape(workflow['updated_at']) if workflow else ''
    timeline = f'<section class="workflow" aria-label="Workflow progress"><div class="workflow-heading"><strong>Workflow</strong><span>{"Last reported " + timestamp if timestamp else "No active step reported"}</span></div><ol>{steps}</ol></section>'
    tracking, build_card = render_tracking(tracking_data)
    run = tracking_data.get("verification_run")
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
            if state == "fixed" and item["status"] != "verified_fixed":
                raise ValueError(f"{item['id']}: fixed check requires verified_fixed status")
            if state == "not_fixed" and item["status"] == "verified_fixed":
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
        displayed = {" ".join(item.get("verification", "").split()).casefold()}
        unique = {}
        for key, value in details.items():
            normalized = " ".join(value.split()).casefold()
            if normalized and normalized not in displayed:
                unique[key] = value
                displayed.add(normalized)
        details = unique
        item["details"] = details
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
    excluded = tracking_data.get("rollover", {}).get("excluded_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded) or not set(excluded) <= seen:
        raise ValueError("rollover excluded_ids must identify tracked findings")
    serialized = json.dumps([item for item in output if item["id"] not in excluded], ensure_ascii=True).replace("<", "\\u003c")
    run_view = {key: run[key] for key in ("id", "build", "revision", "installed_at", "status")} if run else None
    replacements = {"{{TITLE}}": html.escape(title), "{{FINDINGS_JSON}}": serialized,
                    "{{TRACKING}}": tracking, "{{BUILD_CARD}}": build_card, "{{TIMELINE}}": timeline, "{{RUN_JSON}}": json.dumps(run_view, ensure_ascii=True).replace("<", "\\u003c")}
    return re.sub(r"\{\{TITLE\}\}|\{\{FINDINGS_JSON\}\}|\{\{TRACKING\}\}|\{\{BUILD_CARD\}\}|\{\{TIMELINE\}\}|\{\{RUN_JSON\}\}",
                  lambda match: replacements[match.group()], template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="authoritative findings JSON")
    parser.add_argument("output", type=Path, help="output HTML")
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve() or args.output.resolve() == TEMPLATE.resolve():
            raise ValueError("output must differ from input and template")
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = render(data, args.input.resolve().parent)
        args.output.write_text(result, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"render: {error}\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
