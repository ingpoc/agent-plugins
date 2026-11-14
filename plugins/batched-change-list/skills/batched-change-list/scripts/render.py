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
WORKFLOW_STAGES = {"collect": "Collect", "present": "Present", "plan_fix": "Plan & fix", "install": "Install", "verify": "Verify", "introspect": "Introspect", "gate": "Fixes", "report_rollover": "Report & roll over", "merged": "Merged / Done"}
PLAN_STATUSES = {"planned", "running", "review", "installing", "verifying", "verified", "blocked", "failed", "parked"}
PLAN_STATUS_ALIASES = {"pending": "planned", "queued": "planned", "deferred": "parked", "install": "installing", "fixed": "installing", "partial": "verifying"}
PLAN_PHASES = {"fix", "review", "install", "verify", "introspect"}
PLAN_PHASE_ALIASES = {"pending": "fix", "done": "verify"}
LANE_PHASES = {"fix": "Plan & fix", "review": "Plan & fix", "install": "Install", "verify": "Verify", "introspect": "Introspect"}
STALE_LANE_STATUSES = {"running", "review", "installing", "verifying"}
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
    status = plan_status(group)
    started = parse_instant(codex.get("started_at"))
    ended = parse_instant(codex.get("ended_at"))
    seconds = group.get("duration_s", codex.get("duration_s", verify.get("duration_s")))
    if status in PLAN_STATUSES - {"planned", "verified", "parked", "blocked", "failed"} and started and now and not ended:
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
    if not isinstance(status, str) or status not in PLAN_STATUSES | set(PLAN_STATUS_ALIASES) | {"done"}:
        raise ValueError(f"plan group {group.get('id')} has invalid status")
    if status in PLAN_STATUSES:
        return status
    if status == "done":
        if verify.get("status") in ("done", "complete", "verified"):
            return "verified"
        if verify.get("status") in ("running", "partial"):
            return "verifying"
        if verify.get("status") == "blocked":
            return "blocked"
        if verify.get("status") == "failed":
            return "failed"
        return "installing"
    return PLAN_STATUS_ALIASES[status]


def plan_phase(group):
    status = plan_status(group)
    explicit = group.get("phase")
    if status in ("blocked", "failed") and explicit in PLAN_PHASES:
        return explicit
    if status in ("planned", "running"):
        return "fix"
    if status == "review":
        return "review"
    if status == "installing":
        return "install"
    if status in ("verifying", "verified"):
        return "verify"
    if status in ("blocked", "failed"):
        verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
        install = group.get("install", {}) if isinstance(group.get("install"), dict) else {}
        review = group.get("review", {}) if isinstance(group.get("review"), dict) else {}
        if verify.get("status") in ("running", "partial", "failed", "blocked"):
            return "verify"
        if install.get("status") in ("running", "queued", "done"):
            return "install"
        if review.get("status") in ("running", "request-changes"):
            return "review"
    return "fix"


def short_words(value, limit=10):
    return " ".join(str(value or "").split()[:limit])


def plan_groups_for_round(plan, round_number):
    if not isinstance(plan, dict) or plan.get("round", plan.get("cycle", 1)) != round_number:
        return []
    groups = plan.get("groups", [])
    return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []


def lane_issue_status(group, round_number):
    status = plan_status(group)
    group_id = str(group.get("id", "?"))
    if status == "blocked":
        copy = f"Blocked · {group.get('blocked_reason') or 'blocked'}"
    elif status == "parked":
        copy = f"Parked → Round {group.get('parked_to') or round_number + 1}"
    else:
        copy = {"planned": f"Planned · {group_id}", "running": f"Fixing · {group_id}",
                "review": f"In review · {group_id}", "installing": f"In install · {group_id}",
                "verifying": f"Verifying · {group_id}", "verified": f"Verified · {group_id}",
                "failed": f"Fix failed · {group_id}"}.get(status, f"{status.title()} · {group_id}")
    kind = {"blocked": "blk", "parked": "prk", "failed": "bad", "verified": "ok"}.get(status, "run")
    return {"copy": copy, "kind": kind, "status": status}


def finding_lane_status(finding, lane, round_number):
    if lane:
        return lane
    status = finding.get("status", "untested")
    copy = {"failing": "Failing · no lane yet", "untested": "Untested",
            "in_progress": "In progress · no lane yet", "verified_fixed": "Verified fixed",
            "awaiting_user_test": "Awaiting user test", "awaiting_verification": "Awaiting verification",
            "blocked": "Blocked", "investigation": "Investigation needed"}.get(status, str(status).replace("_", " ").title())
    kind = {"failing": "bad", "verified_fixed": "ok", "in_progress": "run", "blocked": "blk"}.get(status, "idle")
    return {"copy": copy, "kind": kind, "status": "no_lane"}


def lane_status_by_finding(data, groups, round_number):
    lanes = {}
    for group in groups:
        lane = lane_issue_status(group, round_number)
        for finding_id in group.get("finding_ids", []) if isinstance(group.get("finding_ids"), list) else []:
            lanes.setdefault(finding_id, lane)
    return {item.get("id"): finding_lane_status(item, lanes.get(item.get("id")), round_number)
            for item in data.get("findings", []) if isinstance(item, dict)}


def carried_tag(finding):
    carried_from = finding.get("carried_from")
    return (f'<span class="carried" title="Carried from {html.escape(str(carried_from), quote=True)}">CARRIED</span>'
            if carried_from not in (None, "") else "")


def render_issue_copy(data, lanes, round_number):
    findings = [item for item in data.get("findings", []) if isinstance(item, dict)]
    if not findings:
        return ""
    cards = []
    for item in findings:
        state = finding_lane_status(item, lanes.get(item.get("id")), round_number)
        cards.append(f'<li><code>{html.escape(str(item.get("id", "")))}{carried_tag(item)}</code>'
                     f'<span class="ttl">{html.escape(str(item.get("title", "")))}</span>'
                     f'<span class="s {state["kind"]}">{html.escape(state["copy"])}</span></li>')
    return ('<div class="copy" aria-label="Issue status copy, derived from lane">'
            '<h3>Issue list status · derived from lane</h3><ul>' + "".join(cards) + '</ul></div>')


def row_phase_track(group, state):
    phase = "verify" if state == "verified" else "fix" if state == "planned" else plan_phase(group)
    phase_index = ("fix", "review", "install", "verify", "introspect").index(phase)
    dots = []
    for index in range(5):
        kind = ""
        if state == "verified" and index < 4:
            kind = "d"
        elif state != "planned" and index < phase_index:
            kind = "d"
        elif state in ("blocked", "failed") and index == phase_index:
            kind = "b" if state == "blocked" else "f"
        elif state not in ("planned", "verified") and index == phase_index:
            kind = "c"
        dots.append(f'<i class="{kind}"></i>' if kind else "<i></i>")
    label = f"Phase {phase_index + 1} of 5: {LANE_PHASES[phase]}"
    if state in ("blocked", "failed"):
        label += f", {state}"
    return f'<span class="track" role="img" aria-label="{html.escape(label, quote=True)}">{"".join(dots)}</span>'


def render_plan(data, tracking, plan, round_number, now):
    active_stage = tracking.get("workflow", {}).get("stage") if isinstance(tracking.get("workflow"), dict) else None
    has_started = active_stage in WORKFLOW_STAGES and list(WORKFLOW_STAGES).index(active_stage) >= list(WORKFLOW_STAGES).index("plan_fix")
    groups = plan_groups_for_round(plan, round_number)
    if not groups:
        empty = f'<div class="plan-empty">No fix plan for Round {round_number} yet.</div>' if has_started else ""
        return empty + render_issue_copy(data, {}, round_number)
    lanes = lane_status_by_finding(data, groups, round_number)
    findings = {item.get("id"): item for item in data.get("findings", []) if isinstance(item, dict)}
    states = {group.get("id"): plan_status(group) for group in groups}
    active_groups = [group for group in groups if states[group.get("id")] not in ("parked", "verified")]
    counts = {state: sum(plan_status(group) == state for group in active_groups)
              for state in ("installing", "verifying", "review", "blocked", "failed")}
    tally = []
    for state, label, kind in (("installing", "installing", ""), ("verifying", "verifying", ""),
                               ("review", "in review", ""), ("blocked", "blocked", "warn"),
                               ("failed", "failed", "bad")):
        if counts[state]:
            value = f'<b>{counts[state]}</b> {label}'
            tally.append(f'<span class="{kind}">{value}</span>')
    rows = []
    labels = {"planned": "Planned", "running": "Fixing", "review": "In review", "installing": "Installing",
              "verifying": "Verifying", "verified": "Verified", "blocked": "Blocked", "failed": "Failed"}
    for group in groups:
        state = states[group.get("id")]
        if state == "parked":
            continue
        codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
        verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
        ids = group.get("finding_ids", []) if isinstance(group.get("finding_ids"), list) else []
        chips = []
        for finding_id in ids[:3]:
            finding = findings.get(finding_id, {})
            chips.append(f'<a class="chip" title="{html.escape(str(finding.get("title", finding_id)), quote=True)}" href="#{quote(str(finding_id), safe="")}">{html.escape(str(finding_id))}{carried_tag(finding)}</a>')
        if len(ids) > 3:
            chips.append(f'<span class="chip more">+{len(ids)-3}</span>')
        if not chips:
            chips = ['<span class="chip no-issues">no issues</span>']
        group_id = html.escape(str(group.get("id", "?")))
        name = html.escape(str(group.get("name") or group.get("label") or f"Group {group.get('id', '?')}"))
        dependencies = group.get("depends_on", [])
        dependency = f'<small>after {html.escape(", ".join(map(str, dependencies)))}</small>' if dependencies else ""
        model = str(codex.get("model", "")).replace("gpt-6-luna", "Luna").replace("gpt-6-astra", "Astra").replace("gpt-6-sol", "Sol")
        if codex.get("effort"):
            model += " " + str(codex["effort"])
        if codex.get("service_tier") == "priority" or codex.get("fast"):
            model += " Fast"
        times = " – ".join(filter(None, (ist_time(codex.get("started_at")), ist_time(codex.get("ended_at")))))
        build = group.get("build")
        note = group.get("blocked_reason") if state == "blocked" else verify.get("failure") if state == "failed" else None
        if not note:
            note = verify.get("summary") or group.get("verify_summary") or group.get("note") or group.get("reason")
        if not note and build:
            note = f'{"Installing" if state == "installing" else "Installed"} build {build}'
        note = short_words(note)
        verify_info = " · ".join(filter(None, (verify.get("method"), verify.get("status"), verify.get("failure"), verify.get("result"), verify.get("summary"))))
        files = group.get("files", group.get("file_notes", []))
        if not isinstance(files, list):
            files = [str(files)]
        details = [("Fix", " · ".join(filter(None, (codex.get("job_id"), codex.get("model"), codex.get("effort"), codex.get("service_tier"), codex.get("worktree"), codex.get("status"), times)))),
                   ("Verify", verify_info), ("Reason", group.get("reason", "")), ("Files", ", ".join(map(str, files)))]
        detail_rows = []
        for finding_id in ids:
            finding = findings.get(finding_id, {})
            status = finding_lane_status(finding, lanes.get(finding_id), round_number)
            detail_rows.append(f'<li><code>{html.escape(str(finding_id))}{carried_tag(finding)}</code>'
                               f'<span class="st {status["kind"]}">{html.escape(status["copy"])}</span>'
                               f'<span>{html.escape(str(finding.get("title", "")))}</span></li>')
        dl = "".join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>' for k, v in details if v)
        spinner = '<span class="spinner" aria-hidden="true"></span>' if state in ("running", "installing", "verifying") else ""
        icon = ('<svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true"><rect x="1" y="1" width="2" height="6" fill="currentColor"/><rect x="5" y="1" width="2" height="6" fill="currentColor"/></svg>'
                if state == "blocked" else "")
        rows.append(f'<details class="grp {state}"><summary><span class="g-name"><code>{group_id}</code><span>{name}{dependency}</span></span>'
                    f'<span class="g-chips">{"".join(chips)}</span>{row_phase_track(group, state)}'
                    f'<span class="g-min">{html.escape(plan_duration(group, now))}</span>'
                    f'<span class="g-model">{html.escape(model or "—")}</span><span class="g-note">{html.escape(note)}</span>'
                    f'<span class="pill {state}">{spinner}{icon}{labels[state]}</span>'
                    '<svg class="chev" viewBox="0 0 14 14" aria-hidden="true"><path d="M5 3l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
                    f'</summary><div class="g-detail"><ul>{"".join(detail_rows)}</ul>{f"<dl>{dl}</dl>" if dl else ""}</div></details>')
    parked_groups = [group for group in groups if states[group.get("id")] == "parked"]
    parked_by_round = {}
    for group in parked_groups:
        target = group.get("parked_to") or round_number + 1
        parked_by_round.setdefault(target, []).append(group)
    parked = "".join(f'<p class="parked-line"><span>Parked for Round {target}</span>'
                      + "".join(f'<span class="pill parked">{html.escape(str(group.get("id", "?")))} · {len(group.get("finding_ids", []))}</span>' for group in target_groups)
                      + '<span>· not running</span></p>' for target, target_groups in sorted(parked_by_round.items()))
    header = (f'<span><strong>Round {round_number}</strong> · {len(active_groups)} lanes · each installs and verifies when ready</span>'
              + (f'<span class="t">{" · ".join(tally)}</span>' if tally else ""))
    return (f'<div class="plan" aria-label="Fix plan, round {round_number}"><div class="plan-head">{header}</div>'
            f'<div class="plan-rows">{"".join(rows)}</div>{parked}'
            '<p class="close-line">Round closes when every lane is <b>verified, blocked or parked</b> → Introspect → gate.</p></div>'
            + render_issue_copy(data, lanes, round_number))


def render_workflow(active, groups, fixed, threshold, gate_met, in_progress, round_number):
    order = ["collect", "present", "plan_fix", "install", "verify", "introspect", "gate", "report_rollover", "merged"]
    active_index = order.index(active) if active in order else -1

    def simple_step(key, label):
        index = order.index(key)
        state = "done" if active == "merged" or (active_index >= 0 and index < active_index) else "current" if active == key else "upcoming"
        current = ' aria-current="step"' if state == "current" else ""
        exit_mark = (f'<span class="wf-exit" aria-hidden="true"><span class="wf-exit-label">{threshold} verified</span>'
                     '<svg width="7" height="8" viewBox="0 0 7 8"><path d="M0 0L7 4L0 8z"/></svg></span>' if key == "report_rollover" else "")
        return (f'<li data-stage="{key}" class="wf-step {state}"{current}><span class="wf-dot"><i></i></span>'
                f'<span class="wf-label">{html.escape(label)}</span>{exit_mark}</li>')

    def chip(count, label="", kind=""):
        if not count:
            return ""
        dots = "".join("<i></i>" for _ in range(min(5, count)))
        more = f'<span class="lc-more">{count}</span>' if count > 5 else ""
        aria = f'{count} lane{"s" if count != 1 else ""}{" " + label if label else ""}'
        text = f" {label}" if count > 5 and label else ""
        if count <= 5:
            text = f"{count} {label}" if label else str(count)
        return f'<span class="lc {kind}" aria-label="{html.escape(aria, quote=True)}"><span class="d">{dots}</span>{more}{text}</span>'

    phase_steps = {"fix": 0, "review": 0, "install": 1, "verify": 2, "introspect": 3}
    lanes = [(group, plan_status(group), phase_steps[plan_phase(group)])
             for group in groups if plan_status(group) not in ("parked", "verified")]
    positions = [position for _, _, position in lanes]
    occupied = len(set(positions))
    steps = []
    for index, (key, label) in enumerate((("plan_fix", "Plan & fix"), ("install", "Install"), ("verify", "Verify"), ("introspect", "Introspect"))):
        count = positions.count(index)
        state = "busy" if count else "done" if not any(position <= index for position in positions) else "upcoming"
        step_lanes = [(group, status) for group, status, position in lanes if position == index]
        chips = []
        for status in ("planned", "running", "review", "installing", "verifying"):
            amount = sum(lane_status == status for _, lane_status in step_lanes)
            text = "review" if status == "review" else "planned" if status == "planned" else ""
            chips.append(chip(amount, text, "rev" if status == "review" else ""))
        blocked = sum(status == "blocked" for _, status in step_lanes)
        failed = sum(status == "failed" for _, status in step_lanes)
        if blocked:
            chips.append(f'<span class="lc blk" aria-label="{blocked} lane{"s" if blocked != 1 else ""} blocked"><svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true"><rect x="1" y="1" width="2" height="6" fill="currentColor"/><rect x="5" y="1" width="2" height="6" fill="currentColor"/></svg>{blocked}</span>')
        if failed:
            chips.append(chip(failed, "failed", "bad"))
        steps.append(f'<li class="wf-step {state}"><span class="wf-dot"><i></i></span><span class="wf-label">{html.escape(label)}</span>'
                     f'<span class="lanes">{"".join(chips)}</span></li>')

    gate_state = "done" if gate_met else "upcoming"
    accessible = f"{fixed} of {threshold} fixes verified, {in_progress} in progress, gate {'met' if gate_met else 'not met'}"
    amount = min(100, round(100 * fixed / threshold))
    if gate_met:
        icon = (f'<svg width="22" height="22" viewBox="0 0 22 22" role="img" aria-label="{html.escape(accessible, quote=True)}">'
                '<circle cx="11" cy="11" r="8.5" fill="var(--green)" stroke="var(--green)" stroke-width="3"/>'
                '<path d="M7.2 11.2l2.6 2.6 5-5.4" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    else:
        arc = (f'<circle cx="11" cy="11" r="8.5" fill="none" stroke="#9bbca8" stroke-width="3" stroke-linecap="round" pathLength="100" stroke-dasharray="{amount} 100" transform="rotate(-90 11 11)"/>' if amount else "")
        icon = (f'<svg width="22" height="22" viewBox="0 0 22 22" role="img" aria-label="{html.escape(accessible, quote=True)}">'
                '<circle cx="11" cy="11" r="8.5" fill="#fff" stroke="var(--sage-line)" stroke-width="3"/>' + arc + '</svg>')
    gate = (f'<li data-stage="gate" class="wf-step {gate_state} wf-gate"><span class="wf-dot">{icon}</span>'
            f'<span class="wf-label">Fixes <b>{fixed}/{threshold}</b> verified<small>{in_progress} in progress</small></span>'
            '<span class="lanes"></span></li>')
    pips = "".join("<i></i>" for _ in range(min(round_number, 5)))
    spread = f'<span class="spread">· {len(lanes)} lanes across {occupied} steps</span>' if occupied > 1 else ""
    pre = simple_step("collect", "Collect") + simple_step("present", "Present")
    post = simple_step("report_rollover", "Report & roll over")
    if active == "merged":
        post += simple_step("merged", WORKFLOW_STAGES["merged"])
    return (f'<ol class="wf" aria-label="Workflow steps"><li class="wf-pre"><ol>{pre}</ol></li>'
            f'<li class="wf-band {"gate-met" if gate_met else "gate-pending"}" role="group" aria-label="Repeats each round. Round {round_number}. {len(lanes)} lanes across {occupied} steps.">'
            f'<div class="wf-band-head"><span><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M10 6a4 4 0 1 1-1.2-2.85" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M9.6 1v2.6H7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>Repeats each round{spread}</span>'
            f'<span class="wf-round">Round {round_number} <span class="wf-pips">{pips}</span></span></div>'
            f'<ol class="wf-band-steps">{"".join(steps)}{gate}</ol>'
            f'<div class="wf-loop" aria-hidden="true"><svg class="wf-loop-head" width="10" height="8" viewBox="0 0 10 8"><path d="M5 0L10 8H0z"/></svg>'
            f'<span class="wf-loop-label">under {threshold} verified · back to Plan &amp; fix</span>'
            f'<span class="wf-loop-mobile">under {threshold} verified · repeat</span></div></li>'
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
    if not isinstance(tracking_data, dict):
        raise ValueError("tracking must be an object")
    workflow = tracking_data.get("workflow")
    if workflow is not None and (not isinstance(workflow, dict) or workflow.get("stage") not in (*WORKFLOW_STAGES, "install_verify", None) or not isinstance(workflow.get("updated_at"), str) or not workflow["updated_at"].strip()):
        raise ValueError("tracking.workflow requires a valid stage and updated_at")
    active = workflow.get("stage") if workflow else None
    stale_after = workflow.get("stale_after_min", 10) if workflow else 10
    if type(stale_after) is not int or stale_after < 1:
        raise ValueError("tracking.workflow.stale_after_min must be a positive integer")
    plan = tracking_data.get("plan")
    round_number = rollover_round(tracking_data)
    groups = plan_groups_for_round(plan, round_number)
    active_lanes = any(plan_status(group) in STALE_LANE_STATUSES for group in groups)
    ist = ZoneInfo("Asia/Kolkata")
    merged = tracking_data.get("merged") if active == "merged" else None
    merge_commit = ""
    merge_timestamp = ""
    if active == "merged":
        if not isinstance(merged, dict):
            raise ValueError("merged workflow requires tracking.merged")
        merge_commit = required_text(merged.get("commit_sha"), "tracking.merged.commit_sha")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", merge_commit, re.I):
            raise ValueError("tracking.merged.commit_sha must be a full Git commit SHA")
        merge_timestamp = required_text(merged.get("merged_at"), "tracking.merged.merged_at")
        if parse_instant(merge_timestamp) is None:
            raise ValueError("tracking.merged.merged_at must be an ISO 8601 timestamp")
    reported = parse_instant(merge_timestamp if merged else workflow.get("updated_at")) if workflow else None
    if workflow and reported is None:
        raise ValueError("tracking.workflow.updated_at must be an ISO 8601 timestamp")
    now_record = workflow.get("now", "") or "" if isinstance(workflow, dict) else ""
    if now_record is not None and not isinstance(now_record, str):
        raise ValueError("tracking.workflow.now must be a string")
    match = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", now_record)
    reference = now or (parse_instant(match.group()) if match else None)
    if reference is not None and reference.tzinfo is None:
        raise ValueError("now must include a timezone")
    relative = ""
    if reported and reference:
        seconds = max(0, int((reference - reported).total_seconds()))
        minutes = seconds // 60
        relative = "just now" if seconds < 60 else f"{minutes} min ago" if minutes < 60 else f"{minutes // 60} hr ago" if minutes < 1440 else f"{minutes // 1440} days ago"
    time_format = "%-I:%M:%S %p IST" if merged else "%-I:%M %p IST"
    time_html = (f'<time datetime="{html.escape(reported.isoformat(), quote=True)}" '
                 f'data-page-updated="{html.escape(reported.isoformat(), quote=True)}" '
                 f'data-stale-after-min="{stale_after}" data-active-lanes="{str(active_lanes).lower()}">'
                 f'{html.escape(reported.astimezone(ist).strftime(time_format))}</time>') if reported else ''
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
        carried_from = item.get("carried_from")
        if carried_from is not None and not (type(carried_from) is int and carried_from > 0 or isinstance(carried_from, str) and carried_from.strip()):
            raise ValueError(f"{item['id']}.carried_from must be a positive round number or nonempty cycle")
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
    render_now = reference or reported
    lanes = lane_status_by_finding(data, groups, round_number)
    in_progress = sum(item.get("status") == "in_progress" for item in findings)
    stale = bool(reported and reference and active_lanes and (reference - reported).total_seconds() > stale_after * 60)
    stale_tag = f'<span class="tag-stale" title="No update for over {stale_after} minutes while lanes are active"{"" if stale else " hidden"}>Stale</span>'
    relative_html = f'<span data-relative-time>{html.escape(relative)}</span>'
    merge_html = f'<span class="merge-meta" title="Merge commit"><span>Merge</span> <code>{html.escape(merge_commit)}</code></span>' if merge_commit else ""
    fresh_html = (f'<span class="fresh">Updated {time_html} · {relative_html}{merge_html}{stale_tag}</span>'
                  if time_html else '<span class="fresh">No update recorded</span>')
    timeline_steps = render_workflow(active, groups, fixed_count, threshold, gate_met, in_progress, round_number)
    plan_view = render_plan(data, tracking_data, plan, round_number, render_now)
    viewer_html = (f'<p class="viewer-footer">reading {html.escape(str(viewer["path"]))}, viewer {html.escape(str(viewer["version"]))}, started {html.escape(str(viewer["started"]))}</p>' if viewer else "")
    timeline = (f'<section class="workflow" aria-label="Workflow progress"><div class="workflow-heading"><strong>Workflow</strong>{fresh_html}</div>'
                f'{timeline_steps}{plan_view}{viewer_html}</section>')
    for item in output:
        item["_lane_status"] = lanes.get(item["id"], finding_lane_status(item, None, round_number))
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
