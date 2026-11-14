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
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

STATUSES = {"verified_fixed", "failing", "untested", "in_progress", "awaiting_user_test",
            "awaiting_verification", "blocked", "investigation"}
SOURCES = {"User reported", "User requested", "Agent reported", "Telemetry lead", "Source hypothesis"}
CHECK_STATES = {"pending", "checking", "fixed", "not_fixed", "blocked", "inconclusive"}
WORKFLOW_STAGES = {"collect": "Collect", "present": "Present", "plan_fix": "Plan & fix", "install": "Install", "verify": "Verify", "introspect": "Introspect", "gate": "Fixes", "report_rollover": "Report & roll over"}
DEFAULT_ROLLOVER_THRESHOLD = 15
AGENT_COLLECTION_PLUGINS = ("agent-computer-use", "batched-change-list", "comet-control",
                            "context-ledger", "developer-observability")
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


def rollover_round(tracking):
    round_number = tracking.get("round")
    if round_number is None:
        plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else {}
        round_number = plan.get("round", 1)
        if round_number is None:
            round_number = 1
    if type(round_number) is not int or round_number < 1:
        raise ValueError("tracking.round must be a positive integer")
    history = tracking.get("round_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError("tracking.round_history must be an array of round records")
    return round_number


def parse_instant(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant


def ist_time(value):
    instant = parse_instant(value)
    return instant.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%-I:%M %p IST") if instant else ""


def plan_duration(group, now):
    codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
    verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
    status = group.get("status", codex.get("status", "pending"))
    started = parse_instant(codex.get("started_at"))
    ended = parse_instant(codex.get("ended_at"))
    seconds = group.get("duration_s", codex.get("duration_s", verify.get("duration_s")))
    if status == "running" and started and now:
        updated = parse_instant(codex.get("updated_at") or group.get("updated_at") or codex.get("started_at"))
        if updated and (now - updated).total_seconds() > 300:
            return "stale"
        seconds = max(0, (now - started).total_seconds())
    elif seconds is None and started and ended:
        seconds = max(0, (ended - started).total_seconds())
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "—"
    return f"{max(1, round(seconds / 60))} min"


def plan_status(group):
    codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
    verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
    status = group.get("status", codex.get("status", "pending"))
    verify_status = verify.get("status", "pending")
    if verify_status == "partial":
        return "partial"
    if verify_status in ("failed", "blocked") or status in ("failed", "blocked") or codex.get("status") in ("failed", "blocked"):
        return "failed"
    if verify_status == "running":
        return "verifying"
    if verify_status in ("done", "complete", "verified"):
        return "verified"
    if status in ("done", "fixed"):
        return "fixed"
    if status == "running" or codex.get("status") == "running":
        return "running"
    return "queued"


def short_words(value, limit=10):
    return " ".join(str(value or "").split()[:limit])


def render_plan(data, tracking, plan, round_number, now):
    active = tracking.get("workflow", {}).get("stage")
    stages = list(WORKFLOW_STAGES)
    has_started = active in stages and stages.index(active) >= stages.index("plan_fix")
    matching = isinstance(plan, dict) and plan.get("round", plan.get("cycle", 1)) == round_number
    if not matching:
        return f'<div class="plan-empty">No fix plan for Round {round_number} yet.</div>' if has_started else ""
    groups = [group for group in plan.get("groups", []) if isinstance(group, dict)]
    counts = {state: sum(plan_status(group) == state for group in groups) for state in ("running", "fixed", "partial", "verifying", "verified", "failed", "queued")}
    tally = " · ".join(f"{counts[key]} {label}" for key, label in (("running", "running"), ("fixed", "fixed"), ("partial", "partial"), ("failed", "failed"), ("verifying", "verifying"), ("verified", "verified"), ("queued", "planned")) if counts[key])
    findings = {item.get("id"): item for item in data.get("findings", [])}
    rows = []
    labels = {"queued":"Planned", "running":"Fixing", "fixed":"Fixed", "partial":"Partial", "verifying":"Verifying", "verified":"Verified", "failed":"Failed"}
    for group in groups:
        codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
        verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
        state = plan_status(group)
        ids = group.get("finding_ids", []) if isinstance(group.get("finding_ids"), list) else []
        chips = [f'<a class="chip" title="{html.escape(str(findings.get(i, {}).get("title", i)), quote=True)}" href="#{quote(str(i), safe="")}">{html.escape(str(i))}</a>' for i in ids[:3]]
        if len(ids) > 3: chips.append(f'<span class="chip">+{len(ids)-3}</span>')
        if not chips: chips = ['<span class="chip no-issues">no issues</span>']
        name = html.escape(str(group.get("name") or group.get("label") or group.get("reason") or f"Group {group.get('id', '?')}"))
        deps = group.get("depends_on", [])
        dependency = f'<small>after {html.escape(", ".join(map(str, deps)))}</small>' if deps else ""
        verify_text = verify.get("summary") or group.get("verify_summary") or short_words(verify.get("failure") or verify.get("result") or verify.get("method"))
        if state == "failed": verify_text = verify.get("failure") or verify.get("result") or verify_text
        verify_text = short_words(verify_text)
        model = str(codex.get("model", "")).replace("gpt-6-luna", "Luna").replace("gpt-6-astra", "Astra").replace("gpt-6-sol", "Sol")
        if codex.get("effort"): model += " " + str(codex["effort"])
        if codex.get("service_tier") == "priority" or codex.get("fast"): model += " Fast"
        duration = plan_duration(group, now)
        times = " – ".join(filter(None, (ist_time(codex.get("started_at")), ist_time(codex.get("ended_at")))))
        file_notes = group.get("files", group.get("file_notes", []))
        if not isinstance(file_notes, list): file_notes = [str(file_notes)]
        verify_info = " · ".join(filter(None, (verify.get("method"), verify.get("status"), verify.get("failure"), verify.get("result"), verify.get("summary"), ist_time(verify.get("started_at")), ist_time(verify.get("ended_at")))))
        details = [("Issues", " · ".join(f"{i} {findings.get(i, {}).get('title','')}".strip() for i in ids) or "No issues"),
                   ("Fix", " · ".join(filter(None, (codex.get("job_id"), codex.get("model"), codex.get("effort"), codex.get("service_tier"), codex.get("worktree"), codex.get("status"), times)))),
                   ("Verify", verify_info), ("Reason", group.get("reason", "")), ("Files", ", ".join(map(str, file_notes)))]
        dl = "".join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>' for k,v in details if v)
        rows.append(f'<details class="grp {state}"><summary><span class="g-id">{html.escape(str(group.get("id", "?")))}</span><span class="g-name">{name}{dependency}</span><span class="g-meta"><span class="g-chips">{"".join(chips)}</span><span class="g-min">{html.escape(duration)}</span><span class="g-model">{html.escape(model or "—")}</span><span class="g-verify">{html.escape(verify_text)}</span></span><span class="pill {state}">{labels[state]}</span><svg class="chev" viewBox="0 0 14 14" aria-hidden="true"><path d="m5 2 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></summary><dl class="g-detail">{dl}</dl></details>')
    noun = "group" if len(groups) == 1 else "groups"
    head = f'<strong>Round {round_number}</strong> · {len(groups)} {noun} in parallel'
    if tally:
        head += f' · {html.escape(tally)}'
    pr = tracking.get("pull_request") if isinstance(tracking.get("pull_request"), dict) else {}
    match = re.search(r"/pull/(\d+)", str(pr.get("url") or ""))
    plan_key = match.group(1) if match else (tracking.get("worktree") if isinstance(tracking.get("worktree"), str) and tracking.get("worktree") else "round")
    return (f'<details class="plan" id="round-plan" data-plan-key="{html.escape(str(plan_key), quote=True)}">'
            f'<summary class="plan-head">{head}</summary>'
            f'<div class="plan-rows">{"".join(rows)}{render_plan_tail(plan, tracking)}</div></details>')


def render_plan_tail(plan, tracking):
    run = tracking.get("verification_run") if isinstance(tracking.get("verification_run"), dict) else {}
    checks = run.get("checks", {}) if isinstance(run.get("checks"), dict) else {}
    checked = sum(isinstance(c, dict) and c.get("state") in ("fixed", "not_fixed", "blocked", "inconclusive") for c in checks.values())
    fallback = (("merge", {"status": "done" if run else "pending"}), ("build", {"status": "done" if run and run.get("status") == "complete" else "running" if run else "pending", "build": run.get("build", "")}), ("verify", {"status": "running" if run and run.get("status") == "active" else "done" if run else "pending", "checked": checked, "total": len(checks)}))
    rows = []
    for key, default in fallback:
        record = plan.get(key) if isinstance(plan.get(key), dict) else default
        status = record.get("status", "pending")
        state = "done" if status in ("done", "complete", "fixed") else "running" if status in ("running", "active") else "failed" if status in ("failed", "blocked") else "pending"
        detail = record.get("ref", "") if key == "merge" else record.get("build", "") if key == "build" else f'{record.get("checked", checked)}/{record.get("total", len(checks))}' if key == "verify" and (checks or "checked" in record or "total" in record) else ""
        label = key.capitalize()
        rows.append(f'<li class="{state}"><i></i>{label}{f" <small>{html.escape(str(detail))}</small>" if detail else ""}</li>')
    return f'<div class="tail"><span class="tail-label">Once per round</span><ol>{"".join(rows)}</ol></div>'


def render_workflow(active, fixed, threshold, gate_met, round_number):
    order = ["collect", "present", "plan_fix", "install", "verify", "introspect", "gate", "report_rollover"]
    active = "unknown" if active == "install_verify" else active
    active_index = order.index(active) if active in order else -1
    def step(key, label, index):
        state = "done" if (key == "gate" and gate_met) or (active_index >= 0 and index < active_index) else "current" if index == active_index else "upcoming"
        current = ' aria-current="step"' if state == "current" else ""
        suffix = '<span class="sr">, done</span>' if state == "done" else ""
        tag = '<span class="wf-now-tag">Now</span>' if state == "current" else ""
        classes = f"wf-step {state}" + (" wf-gate" if key == "gate" else "")
        if key == "gate":
            accessible = f"Fixes {fixed} of {threshold}, gate {'met' if gate_met else 'not met'}"
            if gate_met:
                icon = f'<svg width="22" height="22" viewBox="0 0 22 22" role="img" aria-label="{accessible}"><circle cx="11" cy="11" r="8.5" fill="var(--green)" stroke="var(--green)" stroke-width="3"/><path d="M7.2 11.2l2.6 2.6 5-5.4" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            else:
                amount = min(100, round(100 * fixed / threshold))
                icon = f'<svg width="22" height="22" viewBox="0 0 22 22" role="img" aria-label="{accessible}"><circle cx="11" cy="11" r="8.5" fill="#fff" stroke="var(--amber-track)" stroke-width="3"/><circle cx="11" cy="11" r="8.5" fill="none" stroke="var(--amber)" stroke-width="3" stroke-linecap="round" pathLength="100" stroke-dasharray="{amount} 100" transform="rotate(-90 11 11)"/></svg>'
            label = f'Fixes <b>{fixed}/{threshold}</b><span class="wf-loop-mobile">{"gate met" if gate_met else f"under {threshold} · repeat"}</span>'
            return f'<li data-stage="{key}" class="{classes}"{current}><span class="wf-dot">{icon}</span><span class="wf-label">{label}{suffix}</span></li>'
        return f'<li data-stage="{key}" class="{classes}"{current}><span class="wf-dot"><i></i></span><span class="wf-label">{html.escape(label)}{tag}{suffix}{f"<small>{threshold} reached</small>" if key == "report_rollover" else ""}</span>{"<span class=\"wf-exit\" aria-hidden=\"true\"><span class=\"wf-exit-label\">"+str(threshold)+" reached</span><svg width=\"7\" height=\"8\" viewBox=\"0 0 7 8\"><path d=\"M0 0L7 4L0 8z\"/></svg></span>" if key == "report_rollover" else ""}</li>'
    pre = "".join(step(key, WORKFLOW_STAGES[key], order.index(key)) for key in ("collect", "present"))
    repeated = "".join(step(key, WORKFLOW_STAGES[key], order.index(key)) for key in ("plan_fix", "install", "verify", "introspect", "gate"))
    post = step("report_rollover", WORKFLOW_STAGES["report_rollover"], order.index("report_rollover"))
    pips = min(round_number, 5)
    pip_html = "".join(f'<i class="{"now" if index == pips else "past" if index < pips else ""}"></i>' for index in range(1, pips + 1))
    return (f'<ol class="wf" aria-label="Workflow steps"><li class="wf-pre"><ol>{pre}</ol></li>'
            f'<li class="wf-band {"gate-met" if gate_met else "gate-pending"}" role="group" aria-label="Repeats each round. Round {round_number}.">'
            f'<div class="wf-band-head" aria-hidden="true"><span><svg width="12" height="12" viewBox="0 0 12 12"><path d="M10 6a4 4 0 1 1-1.2-2.85" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M9.6 1v2.6H7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>Repeats each round</span><span class="wf-round">Round {round_number} <span class="wf-pips">{pip_html}</span></span></div>'
            f'<ol class="wf-band-steps">{repeated}</ol><div class="wf-loop" aria-hidden="true"><svg class="wf-loop-head" width="10" height="8" viewBox="0 0 10 8"><path d="M5 0L10 8H0z"/></svg><span class="wf-loop-label">under {threshold} fixes · back to Plan &amp; fix</span><span class="sr">Under {threshold} verified fixes returns to Plan and fix.</span></div></li>'
            f'<li class="wf-post"><ol>{post}</ol></li></ol>')


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
    counted = [item for item in findings if isinstance(item.get("id"), str) and item["id"] not in excluded]
    fixed = sum(item.get("status") == "verified_fixed" for item in counted)
    has_open = any(item.get("status") not in ("verified_fixed", "closed") for item in counted)
    # A count at or above the threshold is always met; never render N of 15 as not met when N >= 15.
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
    context = []
    pull_request = tracking.get("pull_request")
    if pull_request is not None:
        if not isinstance(pull_request, dict):
            raise ValueError("tracking.pull_request must be an object")
        label = html.escape(required_text(pull_request.get("label"), "tracking.pull_request.label"))
        url = html.escape(safe_link(pull_request.get("url"), "tracking.pull_request.url"), quote=True)
        context.append(f'<a class="header-pr" href="{url}">{label}</a>')
    worktree = tracking.get("worktree")
    if worktree is not None:
        context.append(f'<span class="header-worktree">{html.escape(required_text(worktree, "tracking.worktree"))}</span>')
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
        observed = required_text(inventory.get("observed_at"), "installed_plugins.observed_at")
        source = required_text(inventory.get("source"), "installed_plugins.source")
        recorded_versions = inventory.get("recorded_versions", {})
        sources = inventory.get("sources", {})
        chips = []
        for name, version in inventory["versions"].items():
            required_text(name, "plugin name")
            required_text(version, "plugin version")
            if name not in AGENT_COLLECTION_PLUGINS:
                continue
            tip = []
            if name in recorded_versions or recorded_inventory:
                tip.append("As recorded")
            elif name in sources:
                tip.append(str(sources[name]))
            tip.append(f"Inventory checked {observed} · {source}")
            chips.append(f'<span class="plugin-chip" title="{html.escape(" · ".join(tip), quote=True)}">{html.escape(name)} {html.escape(version)}</span>')
        tips = [f"Inventory checked {observed} · {source}"]
        if run is not None:
            tips.insert(0, f"Verification run: {build} · {revision}, installed {installed} · {run['status']}")
        context_html = (' · '.join(context)) if context else ""
        context_line = f'<p class="header-context">{context_html}</p>' if context_html else ""
        chips_html = f'<div class="plugin-chips">{"".join(chips)}</div>' if chips else ""
        title = html.escape(" · ".join(tips), quote=True)
        if context_line or chips_html:
            build_card = f'<div class="header-meta" title="{title}">{context_line}{chips_html}</div>'
        context = []  # consumed into the header
    if not build_card and (context or run is not None):
        tips = []
        if run is not None:
            tips.append(f"Verification run: {build} · {revision}, installed {installed} · {run['status']}")
        context_html = ' · '.join(context)
        context_line = f'<p class="header-context">{context_html}</p>' if context_html else ""
        title = html.escape(" · ".join(tips), quote=True) if tips else ""
        attr = f' title="{title}"' if title else ""
        if context_line:
            build_card = f'<div class="header-meta"{attr}>{context_line}</div>'
    return "", build_card


def render(data, base, inventory_override=None, now=None, viewer=None):
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
    relative = ""
    now_record = workflow.get("now", "") if isinstance(workflow, dict) else ""
    match = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", now_record)
    reference = now or (parse_instant(match.group()) if match else reported)
    if reported and reference:
        minutes = max(0, int((reference - reported).total_seconds() / 60))
        amount, unit = (minutes, "minute") if minutes < 60 else (minutes // 60, "hour") if minutes < 1440 else (minutes // 1440, "day")
        relative = f" ({amount} {unit}{'' if amount == 1 else 's'} ago)"
    time_html = (f'<time datetime="{html.escape(reported.isoformat(), quote=True)}">'
                 f'{html.escape(reported.strftime("%b %-d, %Y, %-I:%M %p IST") + relative)}</time>') if reported else ''
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
    plan = tracking_data.get("plan") if isinstance(tracking_data, dict) else None
    round_number = rollover_round(tracking_data)
    render_now = now or parse_instant(workflow.get("updated_at")) if workflow else now
    timeline_steps = render_workflow(active, fixed_count, threshold, gate_met, round_number)
    plan_view = render_plan(data, tracking_data, plan, round_number, render_now)
    viewer_html = (f'<p class="viewer-footer">reading {html.escape(str(viewer["path"]))}, viewer {html.escape(str(viewer["version"]))}, started {html.escape(str(viewer["started"]))}</p>' if viewer else "")
    timeline = (f'<section class="workflow" aria-label="Workflow progress"><div class="workflow-heading"><strong>Workflow</strong><span>{"Last reported " + time_html if time_html else "No active step reported"}</span></div>'
                f'{timeline_steps}{now_html}{plan_view}{viewer_html}</section>')
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
    parser.add_argument("--viewer-metadata", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--now", help="explicit render time as ISO 8601")
    parser.add_argument("--live", action="store_true", help="discover installed plugins and use the current time")
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve() or args.output.resolve() == TEMPLATE.resolve():
            raise ValueError("output must differ from input and template")
        data = json.loads(args.input.read_text(encoding="utf-8"))
        current = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc) if args.live else None
        inventory = load_inventory(args.inventory) if args.inventory else discover_installed_plugins(now=current) if args.live else None
        viewer = json.loads(args.viewer_metadata.read_text()) if args.viewer_metadata else None
        result = render(data, args.input.resolve().parent, inventory, current, viewer)
        args.output.write_text(result, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"render: {error}\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
