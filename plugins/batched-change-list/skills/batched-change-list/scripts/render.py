#!/usr/bin/env python3
"""Validate findings JSON and render a standalone change-list page."""
import argparse
import base64
import html
import json
import mimetypes
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo
import workflow_definition as WF
import live_state

STATUSES = {"verified_fixed", "failing", "untested", "in_progress", "awaiting_user_test",
            "awaiting_verification", "blocked", "investigation", "closed"}
SOURCES = {"User reported", "User requested", "Agent reported", "Telemetry lead", "Source hypothesis"}
CHECK_STATES = {"pending", "checking", "fixed", "not_fixed", "blocked", "inconclusive"}
WORKFLOW_STAGES = {**WF.STAGE_LABELS, "gate": WF.DEFINITION["gate"]["badge_label"]}
# Lane states, phases and groups come only from the lane table in workflow_definition.json.
LANE = WF.LANE
LANE_GROUPS = LANE["groups"]
PLAN_STATUSES = frozenset(LANE["statuses"])
PLAN_STATUS_ALIASES = LANE["status_aliases"]
PLAN_PHASES = frozenset(LANE["phases"])
PLAN_PHASE_ALIASES = LANE["phase_aliases"]
LANE_PHASES = LANE["phase_labels"]
STALE_LANE_STATUSES = LANE_GROUPS["stale"]
LANE_JOB_PHASES = frozenset(LANE["job_phases"])
LANE_JOB_LABELS = LANE["job_phase_labels"]
DEFAULT_ROLLOVER_THRESHOLD = WF.DEFAULT_ROLLOVER_THRESHOLD
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


def reason_fields(text):
    values = {key: "" for key in ("Checked", "Expected", "Observed")}
    for match in re.finditer(r"(?:^|[;\n])\s*(Checked|Expected|Observed)\s*:\s*([^;\n]*)", str(text), re.I):
        values[match.group(1).title()] = match.group(2).strip()
    return values


RAW_EVIDENCE = re.compile(
    r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|html|css|sh|log|swift|md|yaml|yml)\b|"
    r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\b[a-z][\w-]*\.[a-z_][\w]*\b|\b[a-f0-9]{7,}\b|"
    r"(?<!\w)--[\w-]+|\b(?:Named tests|Log)\s*:", re.I)


def plain_evidence(record):
    record = dict(record)
    details = [record.get("details", "")]
    for field in ("checked", "expected", "observed", "verdict"):
        value = record.get(field, "")
        if isinstance(value, str) and ("/" in value or RAW_EVIDENCE.search(value)):
            details.append(f"{field.title()}: {value}")
            record[field] = "Recorded in technical terms; see Details"
    record["details"] = "\n".join(value for value in details if value)
    return record


def has_raw_identifier(value):
    return "/" in value or bool(RAW_EVIDENCE.search(value))


def not_checked_record(record):
    record = dict(record)
    details = [record.get("details", "")]
    details.extend(f"{label}: {record.get(field, '')}" for field, label in
                   (("checked", "Checked"), ("expected", "Expected"), ("observed", "Observed"),
                    ("verdict", "Verdict")) if record.get(field))
    record.update(state="not_checked", checked="Not checked", expected="Not checked",
                  observed="Not checked", verdict="Not checked",
                  details="\n".join(value for value in details if value))
    return record


def version_evidence(item, runs):
    outcome_states = {"fixed": "fixed", "verified_fixed": "fixed", "fail": "not_fixed",
                      "failed": "not_fixed", "failing": "not_fixed", "not_fixed": "not_fixed",
                      "blocked": "blocked", "inconclusive": "inconclusive"}
    candidates = []
    for r in item.get("verification_evidence", []):
        verdict = r.get("verdict", r.get("outcome", ""))
        row = {**r, "verdict": verdict,
                           "outcome": r.get("outcome", verdict),
                           "checked": r.get("checked", r.get("check", "Verification")),
                           "expected": r.get("expected", "Not recorded"),
                           "observed": r.get("observed", r.get("reference", verdict)),
                           "state": outcome_states.get(verdict.strip().lower(), "")}
        if row["state"] == "blocked" and not row.get("blocker"):
            row = not_checked_record(row)
        candidates.append(plain_evidence(row))
    for run in runs:
        check = run.get("checks", {}).get(item["id"], {})
        for result in [*check.get("previous_checks", []), check]:
            if result.get("state") in ("fixed", "not_fixed", "blocked", "inconclusive") and result.get("evidence"):
                fields = reason_fields(result["evidence"])
                attached = result
                state_label = {"fixed": "Fixed", "not_fixed": "Failing", "blocked": "Blocked",
                               "inconclusive": "Inconclusive"}[result["state"]]
                observed = attached.get("observed") or fields["Observed"] or "Observed behavior not recorded"
                row = {"build": run.get("build", "unknown"), "timestamp": result.get("finished_at", "unknown"),
                                   "check_id": result.get("check_id"),
                                   "verdict": f"{state_label} because {observed}",
                                   "checked": attached.get("checked") or fields["Checked"] or "Verification behavior not recorded",
                                   "outcome": result["evidence"],
                                   "expected": attached.get("expected") or fields["Expected"] or "Expected behavior not recorded",
                                   "observed": observed,
                                   "reference": attached.get("reference") or result["evidence"], "check": "Installed verification",
                                   "blocker": attached.get("blocker", ""),
                                   "details": attached.get("details", ""),
                                   "session": run.get("id", ""), "state": result["state"]}
                if result["state"] == "blocked" and not result.get("blocker"):
                    row = not_checked_record(row)
                candidates.append(plain_evidence(row))
    history = []
    seen = set()
    for index, record in enumerate(candidates):
        identity = record.get("check_id") or ("legacy", index)
        if identity in seen:
            existing = next(entry for _, entry in history if entry.get("check_id") == identity)
            existing.update({key: value for key, value in record.items() if value not in (None, "")})
            continue
        seen.add(identity)
        match = re.search(r"\b\d+\.\d+\.\d+\b", record["build"])
        version = match.group() if match else "Version unknown"
        try:
            when = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            timestamp = when.timestamp() if when.tzinfo else float("-inf")
        except (ValueError, OverflowError):
            timestamp = float("-inf")
        rank = (timestamp, index)
        history.append((rank, {"version": version, **record}))
    by_version = {}
    for _, record in sorted(history, key=lambda entry: entry[0], reverse=True):
        by_version.setdefault(record["version"], []).append(record)
    return [{**records[0], "history": records[1:]} for records in by_version.values()]


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


def introspect_record_for_round(tracking, cycle=None, round_number=None):
    cycle = rollover_cycle(tracking) if cycle is None else cycle
    round_number = rollover_round(tracking) if round_number is None else round_number
    history = tracking.get("introspect_history", [])
    if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
        raise ValueError("tracking.introspect_history must be an array of records")
    for record in history:
        if any(value is not None and (type(value) is not int or value < 1)
               for value in (record.get("cycle"), record.get("round"))):
            raise ValueError("tracking.introspect_history cycle and round must be positive when known")
    match = next((record for record in reversed(history)
                  if record.get("attribution") != "unattributed"
                  and record.get("cycle") == cycle and record.get("round") == round_number), None)
    current = tracking.get("introspect")
    match = match or (current if isinstance(current, dict) and current.get("attribution") != "unattributed"
                      and current.get("cycle") == cycle
                      and current.get("round") == round_number else None)
    if match is not None and (
            not isinstance(match.get("result"), str) or not match["result"].strip()
            or not isinstance(match.get("at"), str) or not match["at"].strip()
            or not isinstance(match.get("steps"), list)
            or any(not isinstance(step, str) or not step.strip() for step in match["steps"])
            or not isinstance(match.get("none"), list)
            or any(not isinstance(step, str) or not step.strip() for step in match["none"])
            or not isinstance(match.get("sections"), dict)
            or list(match["sections"]) != ["delete", "simplify", "optimize", "automate"]):
        raise ValueError("current-round Introspect record is incomplete")
    return match


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


def quota_wait_time(group, now):
    wait = parse_instant(group.get("quota_wait_until"))
    return wait if wait and now and wait > now else None


def plan_duration(group, now):
    codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
    verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
    status = plan_status(group)
    started = parse_instant(codex.get("started_at"))
    ended = parse_instant(codex.get("ended_at"))
    seconds = group.get("duration_s", codex.get("duration_s", verify.get("duration_s")))
    if status in STALE_LANE_STATUSES and started and now and not ended:
        seconds = max(0, (now - started).total_seconds())
    elif seconds is None and started and ended:
        seconds = max(0, (ended - started).total_seconds())
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "—"
    return f"{max(1, round(seconds / 60))} min"


def stage_duration(group, now):
    entered = parse_instant(group.get("stage_entered_at"))
    if not entered or not now:
        return "—"
    return f"{max(0, int((now - entered).total_seconds()) // 60)} min"


def plan_status(group):
    codex = group.get("codex", {}) if isinstance(group.get("codex"), dict) else {}
    verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
    status = group.get("status", codex.get("status", "pending"))
    if not isinstance(status, str) or status not in PLAN_STATUSES | set(PLAN_STATUS_ALIASES) | LANE["raw_statuses"]:
        raise ValueError(f"plan group {group.get('id')} has invalid status")
    if status == "installing" and not (isinstance(group.get("install"), dict) and group["install"].get("started_at")):
        codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
        return "review" if codex.get("review_status") == "passed" else "planned"
    if status in PLAN_STATUSES:
        return status
    if status == "done":
        if verify.get("status") in LANE["record_states"]["verify_done"]:
            return "verified"
        if verify.get("status") in LANE["record_states"]["verify_running"]:
            return "verifying"
        if verify.get("status") == "blocked":
            return "blocked"
        if verify.get("status") == "failed":
            return "failed"
        return "installing" if isinstance(group.get("install"), dict) and group["install"].get("started_at") else "planned"
    return PLAN_STATUS_ALIASES[status]


def plan_phase(group):
    status = plan_status(group)
    explicit = group.get("phase")
    codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
    install = group.get("install") if isinstance(group.get("install"), dict) else {}
    job = codex.get("job") if isinstance(codex.get("job"), dict) else {}
    if job.get("phase") == "plan_review" and "exit_code" not in job:
        return "plan_review"
    if status == "planned" and not install.get("started_at") and explicit in LANE_GROUPS["post_review_phases"]:
        return "review" if codex.get("review_status") == "passed" else "fix"
    if status in LANE_GROUPS["attention"] and explicit in PLAN_PHASES:
        return explicit
    if status in LANE_GROUPS["fix"]:
        return explicit if explicit in PLAN_PHASES else "fix"
    if LANE["status_phase"].get(status):
        return LANE["status_phase"][status]
    if status in LANE_GROUPS["attention"]:
        verify = group.get("verify", {}) if isinstance(group.get("verify"), dict) else {}
        install = group.get("install", {}) if isinstance(group.get("install"), dict) else {}
        review = group.get("review", {}) if isinstance(group.get("review"), dict) else {}
        if verify.get("status") in LANE["record_states"]["verify_attention"]:
            return "verify"
        if install.get("status") in LANE["record_states"]["install_active"]:
            return "install"
        if review.get("status") in LANE["record_states"]["review_active"]:
            return "review"
    return "fix"


def current_workflow_stage(recorded_stage, groups):
    phase_order = LANE["phase_rank"]
    active = [group for group in groups if plan_status(group) not in LANE_GROUPS["closed"]]
    if not active:
        return recorded_stage
    phase = min((plan_phase(group) for group in active), key=phase_order.__getitem__)
    if phase == "plan_review":
        return "review"
    return next(stage["id"] for stage in WF.LOOP if stage.get("lane_phase") == phase)


def lane_job(group):
    """The lane's job receipt, preferring codex.job and falling back to legacy group.job."""
    codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
    job = codex.get("job") if isinstance(codex.get("job"), dict) else group.get("job")
    return job if isinstance(job, dict) else None


def lane_job_running(group):
    job = lane_job(group)
    pid = job.get("pid") if job else None
    return bool(job) and "exit_code" not in job and type(pid) is int and pid > 0 and live_state._pid_is_running(pid)


def lane_jobs(group):
    """Finished receipts plus the current job, in start order, without duplicates."""
    codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
    history = [job for job in (codex.get("jobs") or []) if isinstance(job, dict)]
    current = lane_job(group)
    if current and all(job.get("id") != current.get("id") for job in history):
        history = history + [current]
    def start_key(job):
        return job.get("started_at") or job.get("finished_at") or ""
    return sorted(history, key=start_key)


def job_duration(job, now=None):
    start = parse_instant(job.get("started_at")) if job else None
    end = parse_instant(job.get("finished_at")) if job else None
    if start and not end:
        end = parse_instant(now) if isinstance(now, str) or now is None else now
        if end is None:
            end = datetime.now(timezone.utc)
    if not start or not end or end < start:
        return ""
    seconds = int((end - start).total_seconds())
    if seconds < 60:
        return "less than one min"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def render_job_timeline(group, now=None):
    rows = []
    for job in lane_jobs(group):
        phase = str(job.get("phase") or "job").replace("_", " ")
        model = job.get("model") or "—"
        if "exit_code" not in job:
            state = job.get("latest_step", {}).get("step") or "running"
        elif job.get("exit_code") == 0:
            state = job.get("finished_phase_status") or "passed"
        else:
            state = job.get("first_error_line") or job.get("finished_phase_status") or f"exit {job.get('exit_code')}"
        start = ist_time(job.get("started_at")) or job.get("started_at") or ""
        duration = job_duration(job, now)
        output = job.get("output_path") or ""
        output_html = (f'<a class="job-output" href="{html.escape(Path(output).name, quote=True)}">'
                       f'{html.escape(Path(output).name)}</a>' if output else "")
        parts = [phase, model, state, start, duration]
        cells = " · ".join(html.escape(str(part)) for part in parts if part)
        if output_html:
            cells = f"{cells} · {output_html}" if cells else output_html
        rows.append(f"<li>{cells}</li>")
    if not rows:
        return ""
    return f'<ol class="job-timeline" aria-label="Lane job timeline">{"".join(rows)}</ol>'


TRAIL_LABELS = {"failed_first": "Failed first", "root_cause": "Root cause", "fix_verified": "Fix verified"}


def lane_trail(group):
    """BCL-179: failed-first, root-cause and fix-verified steps in order, with IST times."""
    trail = [row for row in (group.get("trail") or []) if isinstance(row, dict) and row.get("step") in TRAIL_LABELS]
    parts = [" · ".join(filter(None, (TRAIL_LABELS[row["step"]], ist_time(row.get("at")), str(row.get("result") or ""))))
             for row in trail]
    if plan_phase(group) in ("review", "install", "verify") or plan_status(group) == "verified":
        seen = {row["step"] for row in trail}
        parts += [f"{label}: not recorded" for step, label in TRAIL_LABELS.items()
                  if step not in seen and step != "fix_verified"]
    return "; ".join(parts)


def lane_model(group, codex):
    """Who works on the lane now: the model recorded for install or verify (else a scripted
    check), the job model for the lane's current stage, then the reviewer while the lane is
    in review, then the model that wrote the fix."""
    stage = LANE["status_phase"].get(plan_status(group)) if plan_status(group) in LANE_GROUPS["stage_command"] else None
    if stage:
        recorded = group.get("stage_model") if isinstance(group.get("stage_model"), dict) else {}
        model = recorded.get("model") if recorded.get("stage") == stage else None
        return model.strip() if isinstance(model, str) and model.strip() else "No model (scripted check)"
    job = codex.get("job") if isinstance(codex.get("job"), dict) else {}
    in_review = group.get("phase") in LANE["review_phases"] or plan_status(group) == "review"
    job_stage = "fix" if job.get("phase") == "fix" else "review" if job.get("phase") in LANE_JOB_PHASES else None
    if isinstance(job.get("model"), str) and job["model"].strip() and job_stage == ("review" if in_review else "fix"):
        return job["model"].strip()
    if in_review and isinstance(codex.get("reviewer_model"), str) and codex["reviewer_model"].strip():
        return codex["reviewer_model"].strip()
    model = str(codex.get("model", "")).replace("gpt-6-luna", "Luna").replace("gpt-6-astra", "Astra").replace("gpt-6-sol", "Sol")
    if codex.get("effort"):
        model += " " + str(codex["effort"])
    if codex.get("service_tier") == "priority" or codex.get("fast"):
        model += " Fast"
    return model


def short_words(value, limit=10):
    return " ".join(str(value or "").split()[:limit])


def plan_groups_for_round(plan, round_number):
    if not isinstance(plan, dict) or plan.get("round", plan.get("cycle", 1)) != round_number:
        return []
    groups = plan.get("groups", [])
    return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []


def checks_pending_reason(group):
    reason = str((group or {}).get("blocked_reason") or "").strip().lower()
    return any(token in reason for token in (
        "individual issue checks pending",
        "remaining issue checks pending",
        "issue checks pending",
    ))


def lane_display(group, round_number, paused=None):
    """Derive one lane's display state from its job receipt and the round pause.

    The same result feeds the finding chip, the lane row, and the round tally, so a
    retained review receipt can never disagree with a settled lane outcome.
    """
    status = plan_status(group)
    group_id = str(group.get("id", "?"))
    codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
    job = lane_job(group)

    def display(copy, kind, state, label=None, live=False):
        return {"copy": copy, "kind": kind, "status": state,
                "label": label or copy, "live": live}

    # 1. Settled lanes keep their outcome; a retained review receipt cannot change it.
    if status == "verified":
        return display(f"Verified · {group_id}", "ok", "verified", "Verified")
    if status == "blocked":
        if checks_pending_reason(group):
            return display(f"Checks pending · {group.get('blocked_reason')}", "run",
                           "verifying", "Checks pending")
        return display(f"Blocked · {group.get('blocked_reason') or ''}".rstrip(), "blk",
                       "blocked", "Blocked")
    if status == "failed":
        return display(f"Fix failed · {group_id}", "bad", "failed", "Failed")
    if status == "parked":
        target = group.get("parked_to")
        copy = f"Parked → Round {target}" if target is not None else "Parked"
        return display(copy, "prk",
                       "parked", "Parked")

    # 2. A paused round stops every lane that is still in flight.
    if paused and status in STALE_LANE_STATUSES:
        copy = f"Paused · {paused.get('reason') or 'paused'}"
        return display(f"{copy} · {group_id}", "warn", "paused", copy)

    # 3. A started receipt without an exit code is live while its PID lives, else interrupted.
    if job and "exit_code" not in job and job.get("phase") in LANE_JOB_PHASES:
        phase = job["phase"]
        label = LANE_JOB_LABELS[phase]
        pid = job.get("pid")
        if type(pid) is int and pid > 0 and live_state._pid_is_running(pid):
            return display(f"{label} · {group_id}", "run",
                           "running" if phase == "fix" else "review", label, live=True)
        resume = "plan review" if phase == "plan_review" else phase
        copy = f"Interrupted · resume at {resume}"
        return display(f"{copy} · {group_id}", "warn", "interrupted", copy)

    # 4. A job-driven lane (fix or review) without an unfinished job is stopped, not working.
    #    Install and verify run as stage commands without a job receipt, so they keep stored copy.
    if status in LANE_GROUPS["job_driven"]:
        if codex.get("review_status") == "passed":
            return display(f"Review passed · {group_id}", "ok", "review", "Review passed")
        if (status == "review" and plan_phase(group) == "review"
                and codex.get("fix_status") == "passed" and not codex.get("review_status")):
            return display(f"Fix passed · review next · {group_id}", "ok", "review",
                           "Fix passed · review next")
        return display(f"Paused · needs resume · {group_id}", "warn", "paused",
                       "Paused · needs resume")

    # 5. Other stored outcomes.
    if codex.get("review_status") == "changes_requested" or codex.get("plan_review_status") == "changes_requested":
        return display(f"Changes requested · {group_id}", "warn", "running", "Changes requested")
    if (job and job.get("finished_phase_status") == "incomplete"
            or codex.get("review_status") == "incomplete" or codex.get("plan_review_status") == "incomplete"):
        return display(f"Review incomplete · {group_id}", "warn", "running", "Review incomplete")
    if codex.get("review_status") == "passed" or codex.get("plan_review_status") == "passed":
        return display(f"Review passed · {group_id}", "ok", "review", "Review passed")
    if job and job.get("phase") == "plan_review" and job.get("finished_phase_status") == "passed":
        return display(f"Plan review passed · {group_id}", "ok", "review", "Plan review passed")
    if checks_pending_reason(group) and status == "verifying":
        return display(f"Checks pending · {group.get('blocked_reason')}", "run", "verifying", "Verifying")
    return display(f"{LANE['status_copy'][status]} · {group_id}", LANE["status_kind"][status], status,
                   LANE["status_labels"][status])


def lane_issue_status(group, round_number, paused=None):
    display = lane_display(group, round_number, paused)
    return {key: display[key] for key in ("copy", "kind", "status")}


def finding_lane_status(finding, lane, round_number):
    outcome = {"verified_fixed": "Verified fixed", "failing": "Failing", "untested": "Untested",
               "in_progress": "In progress", "awaiting_user_test": "Awaiting user test",
               "awaiting_verification": "Awaiting verification", "blocked": "Blocked",
               "investigation": "Investigation needed"}.get(finding.get("status", "untested"),
                                                                str(finding.get("status", "untested")).replace("_", " ").title())
    if lane:
        return {**lane, "outcome": outcome}
    status = finding.get("status", "untested")
    copy = {"failing": "Failing · no lane yet", "untested": "Untested",
            "in_progress": "In progress · no lane yet", "verified_fixed": "Verified fixed",
            "awaiting_user_test": "Awaiting user test", "awaiting_verification": "Awaiting verification",
            "blocked": "Blocked", "investigation": "Investigation needed", "closed": "Closed"}.get(status, str(status).replace("_", " ").title())
    kind = {"failing": "bad", "verified_fixed": "ok", "in_progress": "run", "blocked": "blk"}.get(status, "idle")
    return {"copy": copy, "kind": kind, "status": "no_lane", "outcome": outcome}


def lane_status_by_finding(data, groups, round_number, displays=None):
    if displays is None:
        workflow = data.get("tracking", {}).get("workflow", {})
        paused = workflow.get("paused") if isinstance(workflow, dict) else None
        displays = {group.get("id"): lane_display(group, round_number, paused) for group in groups}
    lanes = {}
    for group in groups:
        display = displays[group.get("id")]
        lane = {key: display[key] for key in ("copy", "kind", "status")}
        for finding_id in group.get("finding_ids", []) if isinstance(group.get("finding_ids"), list) else []:
            lanes.setdefault(finding_id, lane)
    return {item.get("id"): finding_lane_status(item, lanes.get(item.get("id")), round_number)
            for item in data.get("findings", []) if isinstance(item, dict)}


def carried_tag(finding):
    carried_from = finding.get("carried_from")
    return (f'<span class="carried" title="Carried from {html.escape(str(carried_from), quote=True)}">CARRIED</span>'
            if carried_from not in (None, "") else "")


def row_phase_track(group, state):
    lane_phases = [stage["lane_phase"] for stage in WF.LOOP
                   if stage.get("lane_phase") and stage["lane_phase"] != "introspect"]
    phase = "verify" if state == "verified" else "fix" if state == "planned" else plan_phase(group)
    if phase == "plan_review":
        phase = "review"
    phase_index = lane_phases.index(phase)
    dots = []
    for index in range(len(lane_phases)):
        kind = ""
        if state == "verified" or (state != "planned" and index < phase_index):
            kind = "d"
        elif state in LANE_GROUPS["attention"] and index == phase_index:
            kind = "b" if state == "blocked" else "f"
        elif state not in LANE_GROUPS["at_rest"] and index == phase_index:
            kind = "c"
        dots.append(f'<i class="{kind}"></i>' if kind else "<i></i>")
    label = (f"Phase {phase_index + 1} of {len(lane_phases)}: {LANE_PHASES[phase]}" if phase_index < len(lane_phases)
             else "Lane complete; Introspect is a round step")
    if state in LANE_GROUPS["attention"]:
        label += f", {state}"
    return f'<span class="track" role="img" aria-label="{html.escape(label, quote=True)}">{"".join(dots)}</span>'


def render_plan(data, tracking, plan, round_number, now, displays=None):
    active_stage = tracking.get("workflow", {}).get("stage") if isinstance(tracking.get("workflow"), dict) else None
    has_started = active_stage in WORKFLOW_STAGES and list(WORKFLOW_STAGES).index(active_stage) >= list(WORKFLOW_STAGES).index("plan_fix")
    groups = plan_groups_for_round(plan, round_number)
    if not groups:
        empty = f'<div class="plan-empty">No fix plan for Round {round_number} yet.</div>' if has_started else ""
        return empty
    if displays is None:
        workflow = tracking.get("workflow") if isinstance(tracking.get("workflow"), dict) else {}
        paused = workflow.get("paused")
        displays = {group.get("id"): lane_display(group, round_number, paused) for group in groups}
    lanes = lane_status_by_finding(data, groups, round_number, displays)
    findings = {item.get("id"): item for item in data.get("findings", []) if isinstance(item, dict)}
    states = {group.get("id"): displays[group.get("id")]["status"] for group in groups}
    active_groups = [group for group in groups if states[group.get("id")] not in LANE_GROUPS["closed"]]
    counts = {state: sum(states[group.get("id")] == state for group in groups) for state, _, _ in LANE["tally"]}
    tally = []
    for state, label, kind in LANE["tally"]:
        if counts[state]:
            value = f'<b>{counts[state]}</b> {label}'
            tally.append(f'<span class="{kind}">{value}</span>')
    rows = []
    for group in groups:
        display = displays[group.get("id")]
        state = display["status"]
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
        model = lane_model(group, codex)
        times = " – ".join(filter(None, (ist_time(codex.get("started_at")), ist_time(codex.get("ended_at")))))
        build = group.get("build")
        waiting_until = quota_wait_time(group, now)
        if waiting_until:
            note = f"Waiting for quota until {ist_time(group.get('quota_wait_until'))}"
        else:
            note = group.get("blocked_reason") if state == "blocked" else verify.get("failure") if state == "failed" else None
            if not note:
                note = verify.get("summary") or group.get("verify_summary") or group.get("note") or group.get("reason")
            if not note and build:
                note = f'{"Installing" if state == "installing" else "Installed"} build {build}'
        job = lane_job(group)
        if display.get("live") and job and isinstance(job.get("latest_step"), dict):
            step = job["latest_step"]
            note = step.get("step") or note
            if step.get("result"):
                note = f"{step.get('step')}: {step.get('result')}"
        note = short_words(note)
        pill_label = "Waiting for quota" if waiting_until else display["label"]
        verify_info = " · ".join(filter(None, (verify.get("method"), verify.get("status"), verify.get("failure"), verify.get("result"), verify.get("summary"))))
        files = group.get("files", group.get("file_notes", []))
        if not isinstance(files, list):
            files = [str(files)]
        details = [("Trail", lane_trail(group)), ("Fix", " · ".join(filter(None, (codex.get("job_id"), codex.get("model"), codex.get("effort"), codex.get("service_tier"), codex.get("worktree"), codex.get("status"), times)))),
                   ("Verify", verify_info), ("Verify command", verify.get("command", "")),
                   ("Reason", group.get("reason", "")), ("Files", ", ".join(map(str, files)))]
        detail_rows = []
        for finding_id in ids:
            finding = findings.get(finding_id, {})
            status = finding_lane_status(finding, lanes.get(finding_id), round_number)
            detail_rows.append(f'<li><code>{html.escape(str(finding_id))}{carried_tag(finding)}</code>'
                               f'<span class="st {status["kind"]}">{html.escape(status["copy"])}</span>'
                               f'<span>{html.escape(str(finding.get("title", "")))}</span></li>')
        dl = "".join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>' for k, v in details if v)
        spinner = '<span class="spinner" aria-hidden="true"></span>' if state in LANE_GROUPS["busy"] else ""
        icon = ('<svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true"><rect x="1" y="1" width="2" height="6" fill="currentColor"/><rect x="5" y="1" width="2" height="6" fill="currentColor"/></svg>'
                if state == "blocked" else "")
        rows.append(f'<details class="grp {state}"><summary><span class="g-name"><code>{group_id}</code><span>{name}{dependency}</span></span>'
                    f'<span class="g-chips">{"".join(chips)}</span>{row_phase_track(group, plan_status(group))}'
                    f'<span class="g-min">{html.escape(stage_duration(group, now))}</span>'
                    f'<span class="g-model">{html.escape(model or "—")}</span><span class="g-note">{html.escape(note)}</span>'
                    f'<span class="pill {state}">{spinner}{icon}{html.escape(pill_label)}</span>'
                    '<svg class="chev" viewBox="0 0 14 14" aria-hidden="true"><path d="M5 3l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
                    f'</summary><div class="g-detail"><ul>{"".join(detail_rows)}</ul>{f"<dl>{dl}</dl>" if dl else ""}'
                    f'{render_job_timeline(group, now)}</div></details>')
    parked_groups = [group for group in groups if states[group.get("id")] == "parked"]
    parked_by_round = {}
    for group in parked_groups:
        target = group.get("parked_to") or round_number + 1
        parked_by_round.setdefault(target, []).append(group)
    parked = "".join(f'<p class="parked-line"><span>Parked for Round {target}</span>'
                      + "".join(f'<span class="pill parked">{html.escape(str(group.get("id", "?")))} · {len(group.get("finding_ids", []))}</span>' for group in target_groups)
                      + '<span>· not running</span></p>' for target, target_groups in sorted(parked_by_round.items()))
    open_count = len(active_groups)
    lane_head = (f'{open_count} open lane{"" if open_count == 1 else "s"} · each installs and verifies when ready'
                 if open_count else f'0 open lanes · {len(groups)} settled')
    header = (f'<span><strong>Round {round_number}</strong> · {lane_head}</span>'
              + (f'<span class="t">{" · ".join(tally)}</span>' if tally else ""))
    return (f'<div class="plan" aria-label="Fix plan, round {round_number}"><div class="plan-head">{header}</div>'
            '<p class="close-line">Lane dots: Fix → Review → Install → Verify. Filled means completed; outlined means not completed (highlighted: current). Introspect is a round step.</p>'
            f'<div class="plan-rows">{"".join(rows)}</div>{parked}'
            '<p class="close-line">Round closes when every lane is <b>verified, blocked or parked</b> → Introspect → gate.</p></div>')


def render_workflow(active, groups, fixed, threshold, gate_met, in_progress, round_number, reached=None,
                    cycle=1, introspect_record=None, report_exception=False):
    active = current_workflow_stage(active, groups)
    order = [stage["id"] for stage in WF.STAGES]
    active_index = order.index(active) if active in order else -1
    reached = [step for step in (reached or []) if isinstance(step, str)]

    def simple_step(stage):
        key, label = stage["id"], stage["label"]
        index = order.index(key)
        state = "done" if active == "merged" or (active_index >= 0 and index < active_index) else "current" if active == key else "upcoming"
        if key == "merged" and active == "merged":
            label = stage.get("completed_label", label)
        current = ' aria-current="step"' if state == "current" else ""
        exit_mark = (f'<span class="wf-exit" aria-hidden="true"><span class="wf-exit-label">{threshold}+ verified</span>'
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

    phase_steps = LANE["phase_rank"]
    lanes = [(group, plan_status(group), phase_steps[plan_phase(group)])
             for group in groups if plan_status(group) != "parked"]

    def review_complete(group):
        codex = group.get("codex")
        return (isinstance(codex, dict)
                and (codex.get("one_file") is True and not codex.get("reviewer_session")
                     or codex.get("reviewer_session") and codex.get("review_status") == "passed"))

    positions = [position for _, _, position in lanes]
    occupied = len(set(positions))
    steps = []
    for index, stage in enumerate(stage for stage in WF.LOOP if stage.get("lane_phase")):
        key, label = stage["id"], stage["label"]
        step_lanes = [(group, status) for group, status, position in lanes if position == index]
        past = bool(lanes) and all(position > index or status in LANE_GROUPS["settled"] for _, status, position in lanes)
        if key == "install":
            past = bool(lanes) and all(
                position > index and status not in LANE_GROUPS["attention"] and review_complete(group)
                for group, status, position in lanes)
        if key == "verify":
            past = past and any(position >= 2 or status in LANE_GROUPS["verify"]
                                for _, status, position in lanes)
        if key == "introspect":
            past = (isinstance(introspect_record, dict)
                    and introspect_record.get("cycle") == cycle
                    and introspect_record.get("round") == round_number)
        running = any(status in STALE_LANE_STATUSES for _, status in step_lanes)
        if not lanes:
            past = key in reached and active_index > order.index(key)
        current = active == key
        state = "busy" if running and current else "current" if current else "done" if past else "upcoming"
        chips = []
        for status in LANE["step_chips"]:
            amount = sum(lane_status == status for _, lane_status in step_lanes)
            text = "review" if status == "review" else "planned" if status == "planned" else ""
            chips.append(chip(amount, text, "rev" if status == "review" else ""))
        blocked = sum(status == "blocked" for _, status in step_lanes)
        failed = sum(status == "failed" for _, status in step_lanes)
        if blocked:
            chips.append(f'<span class="lc blk" aria-label="{blocked} lane{"s" if blocked != 1 else ""} blocked"><svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true"><rect x="1" y="1" width="2" height="6" fill="currentColor"/><rect x="5" y="1" width="2" height="6" fill="currentColor"/></svg>{blocked}</span>')
        if failed:
            chips.append(chip(failed, "failed", "bad"))
        activity = next((stage.get("activities", []) for stage in WF.LOOP if stage["id"] == key), [])
        sublabel = '<small> · Pre-push suite</small>' if "Pre-push suite" in activity else ""
        steps.append(f'<li data-stage="{key}" class="wf-step {state}"><span class="wf-dot"><i></i></span><span class="wf-label">{html.escape(label)}{sublabel}</span>'
                     f'<span class="lanes">{"".join(chips)}</span></li>')

    gate_state = "done" if gate_met else "current" if active == "gate" else "upcoming"
    merge_threshold = max(threshold, WF.MERGE_PREVIOUS_PR_THRESHOLD)
    accessible = (f"{fixed} of {merge_threshold} fixes verified (report gate {threshold}), "
                  f"{in_progress} in progress, gate {'met' if gate_met else 'not met'}")
    if report_exception:
        accessible += "; report gate exception applies"
    amount = min(100, round(100 * fixed / merge_threshold))
    if gate_met:
        icon = (f'<svg width="22" height="22" viewBox="0 0 22 22" role="img" aria-label="{html.escape(accessible, quote=True)}">'
                '<circle cx="11" cy="11" r="8.5" fill="var(--green)" stroke="var(--green)" stroke-width="3"/>'
                '<path d="M7.2 11.2l2.6 2.6 5-5.4" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    else:
        arc = (f'<circle cx="11" cy="11" r="8.5" fill="none" stroke="#9bbca8" stroke-width="3" stroke-linecap="round" pathLength="100" stroke-dasharray="{amount} 100" transform="rotate(-90 11 11)"/>' if amount else "")
        icon = (f'<svg width="22" height="22" viewBox="0 0 22 22" role="img" aria-label="{html.escape(accessible, quote=True)}">'
                '<circle cx="11" cy="11" r="8.5" fill="#fff" stroke="var(--sage-line)" stroke-width="3"/>' + arc + '</svg>')
    gate = (f'<li data-stage="gate" class="wf-step {gate_state} wf-gate"><span class="wf-dot">{icon}</span>'
            f'<span class="wf-label">{WF.DEFINITION["gate"]["badge_label"]} <b>{fixed}/{merge_threshold}</b> verified<small>{fixed}/{threshold} report gate</small><small>{in_progress} in progress</small>'
            f'{"<small>Report gate exception: no counted findings remain open.</small>" if report_exception else ""}</span>'
            '<span class="lanes"></span></li>')
    pips = "".join("<i></i>" for _ in range(min(round_number, 5)))
    spread = f'<span class="spread">· {len(lanes)} lanes across {occupied} steps</span>' if occupied > 1 else ""
    entry = [stage for stage in WF.ENTRY if not (
        stage.get("hide_after_rollover_until_reached") and cycle > 1 and stage["id"] not in reached)]
    pre = "".join(simple_step(stage) for stage in entry)
    post = "".join(simple_step(stage) for stage in WF.EXIT)
    gate_note = WF.DEFINITION["gate"]["note"].format(
        threshold=threshold, merge_threshold=WF.MERGE_PREVIOUS_PR_THRESHOLD)
    return (f'<ol class="wf" aria-label="Workflow steps"><li class="wf-pre"><ol>{pre}</ol></li>'
            f'<li class="wf-band {"gate-met" if gate_met else "gate-pending"}" role="group" aria-label="Repeats each round. Round {round_number}. {len(lanes)} lanes across {occupied} steps.">'
            f'<div class="wf-band-head"><span><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M10 6a4 4 0 1 1-1.2-2.85" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M9.6 1v2.6H7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>Repeats each round{spread}</span>'
            f'<span class="wf-round">Round {round_number} <span class="wf-pips">{pips}</span></span></div>'
            f'<ol class="wf-band-steps">{"".join(steps)}{gate}</ol>'
            f'<div class="wf-loop" aria-hidden="true"><svg class="wf-loop-head" width="10" height="8" viewBox="0 0 10 8"><path d="M5 0L10 8H0z"/></svg>'
            f'<span class="wf-loop-label">{html.escape(WF.DEFINITION["gate"]["loop_label"].format(threshold=threshold))}</span>'
            f'<span class="wf-loop-mobile">{html.escape(WF.DEFINITION["gate"]["mobile_loop_label"].format(threshold=threshold))}</span></div></li>'
            f'<li class="wf-post"><ol>{post}</ol></li></ol>'
            f'<p class="gate-note">{html.escape(gate_note)} Fix count excludes completed introspection workflow records and excluded issues.</p>')


def check_keeps_verified(check):
    """A verified issue stays counted unless its latest check reopened it; a check left unfinished by a stopped run does not (BCL-99)."""
    if not isinstance(check, dict):
        return False
    state = check.get("state")
    if state == "inconclusive" and check.get("stopped") is True:
        return True
    # A check still in progress is collecting evidence, not a reopen.
    return state not in {"blocked", "inconclusive", "not_fixed"}


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
    counted = [item for item in findings if isinstance(item.get("id"), str) and item["id"] not in excluded
               and not (re.fullmatch(r"WF-\d+", item["id"]) and item.get("status") == "verified_fixed"
                        and isinstance(item.get("details"), dict) and item["details"].get("fixed_in") == "Introspect")]
    run = tracking.get("verification_run") or {}
    checks = run.get("checks", {}) if isinstance(run, dict) else {}
    def currently_fixed(item):
        check = checks.get(item["id"], {}) if isinstance(checks, dict) else {}
        return item.get("status") == "verified_fixed" and check_keeps_verified(check)
    fixed = sum(currently_fixed(item) for item in counted)
    has_open = any(item.get("status") != "closed" and not currently_fixed(item) for item in counted)
    # A count at or above the threshold is always met; never render N of 15 as not met when N >= 15.
    return fixed, threshold, fixed >= threshold or not has_open


def report_gate_state(findings, tracking):
    fixed, threshold, met = rollover_gate(findings, tracking)
    reached = tracking.get("stages_reached", [])
    if not isinstance(reached, list) or any(not isinstance(stage, str) for stage in reached):
        raise ValueError("tracking.stages_reached must be a list of stage names")
    if "install" in reached and introspect_record_for_round(tracking) is None:
        met = False
    return fixed, threshold, met


def rollover_gate_state(findings, tracking):
    fixed, threshold, phase_met = report_gate_state(findings, tracking)
    return fixed, threshold, fixed >= threshold and phase_met


def merge_gate(findings, tracking):
    fixed, _, _ = rollover_gate(findings, tracking)
    threshold = WF.MERGE_PREVIOUS_PR_THRESHOLD
    return fixed, threshold, fixed >= threshold



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
    details = []
    pull_request = tracking.get("pull_request")
    if pull_request is not None:
        if not isinstance(pull_request, dict):
            raise ValueError("tracking.pull_request must be an object")
        label = html.escape(required_text(pull_request.get("label"), "tracking.pull_request.label"))
        url = html.escape(safe_link(pull_request.get("url"), "tracking.pull_request.url"), quote=True)
        context.append(f'<a class="header-pr" href="{url}">{label}</a>')
    worktree = tracking.get("worktree")
    if worktree is not None:
        worktree = required_text(worktree, "tracking.worktree")
        short_worktree = Path(worktree.rstrip("/\\")).name
        if not short_worktree:
            raise ValueError("tracking.worktree must include a worktree name")
        context.append(f'<span class="header-worktree">{html.escape(short_worktree)}</span>')
        if short_worktree != worktree:
            details.append(f'<details class="worktree-details"><summary>Details</summary><p>{html.escape(worktree)}</p></details>')
    archive = tracking.get("archive")
    if archive is not None:
        if (not isinstance(archive, dict) or type(archive.get("count")) is not int
                or archive["count"] < 0):
            raise ValueError("tracking.archive requires a nonnegative count")
        if archive["count"]:
            context.append(f'<span>{archive["count"]} archived</span>')
    parked = tracking.get("parked")
    if parked is not None:
        if (not isinstance(parked, dict) or type(parked.get("count")) is not int
                or parked["count"] < 0 or not isinstance(parked.get("needs"), list)
                or any(not isinstance(value, str) or not value.strip() for value in parked["needs"])):
            raise ValueError("tracking.parked requires a nonnegative count and needs array")
        if parked["count"]:
            needs = list(dict.fromkeys(short_words(value, 8) + ("…" if len(value.split()) > 8 else "") for value in parked["needs"]))
            if needs:
                need_text = needs[0] if len(needs) == 1 else f'{", ".join(needs[:-1])} or {needs[-1]}'
                context.append(f'<span>{parked["count"]} parked (need {html.escape(need_text)})</span>')
            else:
                context.append(f'<span>{parked["count"]} parked</span>')
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
        shared = run.get("shared_check")
        if shared is not None:
            if not isinstance(shared, dict) or shared.get("state") not in ("checking", "passed", "failed"):
                raise ValueError("tracking.verification_run.shared_check is invalid")
            context.append({"checking": "Shared checks running for this install",
                            "passed": "Shared checks passed once for this install",
                            "failed": "Shared checks failed for this install"}[shared["state"]])
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
        details_html = ''.join(details)
        if context_line or chips_html or details_html:
            build_card = f'<div class="header-meta" title="{title}">{context_line}{details_html}{chips_html}</div>'
        context = []  # consumed into the header
    if not build_card and (context or run is not None or details):
        tips = []
        if run is not None:
            tips.append(f"Verification run: {build} · {revision}, installed {installed} · {run['status']}")
        context_html = ' · '.join(context)
        context_line = f'<p class="header-context">{context_html}</p>' if context_html else ""
        title = html.escape(" · ".join(tips), quote=True) if tips else ""
        attr = f' title="{title}"' if title else ""
        details_html = ''.join(details)
        if context_line or details_html:
            build_card = f'<div class="header-meta"{attr}>{context_line}{details_html}</div>'
    return "", build_card


PLUGIN_PREFIXES = {"BCL": "batched-change-list", "ACU": "agent-computer-use", "CC": "comet-control",
                   "COMET": "comet-control", "CL": "context-ledger", "DO": "developer-observability"}
PLUGIN_PATH = re.compile(r"plugins/([a-z0-9][a-z0-9-]*)/")


def plugin_of(item):
    """Plugin for the issue list: explicit field, then ID prefix, then the first plugins/<name>/ path it names."""
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    for value in (item.get("plugin"), details.get("plugin")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    prefix = str(item.get("id", "")).split("-", 1)[0].upper()
    if prefix in PLUGIN_PREFIXES:
        return PLUGIN_PREFIXES[prefix]
    text = " ".join(str(value) for value in [*item.values(), *details.values()] if isinstance(value, str))
    match = PLUGIN_PATH.search(text)
    return match.group(1) if match else "other"


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
    paused = workflow.get("paused") if isinstance(workflow, dict) else None
    displays = {group.get("id"): lane_display(group, round_number, paused) for group in groups}
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
    reported = (parse_instant(tracking_data.get("last_written_at")) or
                (parse_instant(workflow.get("updated_at")) if workflow else None))
    if workflow and reported is None:
        raise ValueError("tracking.workflow.updated_at must be an ISO 8601 timestamp")
    reference = now or datetime.now(timezone.utc)
    if reference is not None and reference.tzinfo is None:
        raise ValueError("now must include a timezone")
    render_now = reference
    # Stale only for overdue live work: unfinished, PID-live jobs of unsettled lanes in an
    # unpaused round. Each job's freshness is its latest heartbeat or lane progress (else its
    # start); the oldest of those decides, so one fresh job cannot mask an overdue one.
    job_times = []
    if not paused:
        for group in groups:
            if quota_wait_time(group, render_now) or not lane_job_running(group):
                continue
            if displays[group.get("id")]["status"] in LANE_GROUPS["settled"] | {"paused"}:
                continue
            job = lane_job(group) or {}
            progress = [stamp for stamp in (parse_instant(job.get("heartbeat_at")), parse_instant(group.get("updated_at"))) if stamp]
            latest = max(progress) if progress else parse_instant(job.get("started_at"))
            if latest:
                job_times.append(latest)
    stale_active = bool(job_times)
    stale_since = min(job_times) if job_times else None
    relative = ""
    if reported and reference:
        seconds = max(0, int((reference - reported).total_seconds()))
        minutes = seconds // 60
        relative = "just now" if seconds < 60 else f"{minutes} min ago" if minutes < 60 else f"{minutes // 60} hr ago" if minutes < 1440 else f"{minutes // 1440} days ago"
    time_format = "%-I:%M:%S %p IST" if merged else "%-I:%M %p IST"
    time_html = (f'<time datetime="{html.escape(reported.isoformat(), quote=True)}" '
                 f'data-page-updated="{html.escape(reported.isoformat(), quote=True)}" '
                 f'data-stale-after-min="{stale_after}" data-stale-active="{str(stale_active).lower()}" '
                 f'data-stale-since="{html.escape(stale_since.isoformat(), quote=True) if stale_since else ""}">'
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
            if check is not None and (not isinstance(check, dict) or check.get("state") not in CHECK_STATES):
                raise ValueError(f"{item['id']}: verification check must belong to the current run")
            check = check or {"state": "pending"}
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
            if run["status"] == "complete" and item["id"] in run["checks"] and state in ("pending", "checking"):
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
        technical_text = {}
        for field in ("title", "summary", "impact", "done_when"):
            value = item.get(field)
            if isinstance(value, str) and value.strip() and has_raw_identifier(value):
                technical_text[field.replace("_", " ").title()] = value
                item[field] = "Recorded in technical terms; see Details"
        if technical_text:
            wording = "\n".join(f"{key}: {value}" for key, value in technical_text.items())
            details = {**details, "Technical wording": "\n".join(filter(None, (details.get("Technical wording", ""), wording)))}
            item["details"] = details
        if run is not None and check.get("evidence"):
            if check.get("corrections"):
                details = {**details, "Check corrections": json.dumps(check["corrections"], ensure_ascii=False)}
            item["details"] = details
        records = item.get("verification_evidence", [])
        fields = ("check", "outcome", "build", "session", "timestamp", "reference")
        structured = {"checked", "expected", "observed", "verdict", "build", "session", "timestamp", "reference"}
        identified = structured | {"check_id"}
        legacy = {"check", "outcome", "build", "session", "timestamp", "reference"}
        allowed_records = tuple(schema | suffix for schema in (legacy, structured, identified)
                                for suffix in (set(), {"details"}, {"blocker"}, {"details", "blocker"}))
        if not isinstance(records, list) or any(not isinstance(r, dict) or set(r) not in allowed_records or
                any(not isinstance(r[k], str) or (k != "details" and not r[k].strip()) for k in r) for r in records):
            raise ValueError(f"{item['id']}.verification_evidence has invalid records")
        version_runs = ([run] if run else []) if carried_from is not None else [
            *tracking_data.get("verification_history", []), *([run] if run else [])]
        item["version_timeline"] = version_evidence(item, version_runs)
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
    if run is not None and not set(run["checks"]).issubset(seen):
        raise ValueError("verification run checks must match the tracked issue IDs")
    template = TEMPLATE.read_text(encoding="utf-8")
    fixed_count, threshold, gate_met = rollover_gate_state(findings, tracking_data)
    report_exception = fixed_count < threshold and report_gate_state(findings, tracking_data)[2]
    rollover = tracking_data.get("rollover", {})
    excluded = rollover.get("excluded_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded) or not set(excluded) <= seen:
        raise ValueError("rollover excluded_ids must identify tracked findings")
    render_now = render_now or reported
    lanes = lane_status_by_finding(data, groups, round_number, displays)
    in_progress = sum(item.get("status") == "in_progress" for item in findings)
    stale = bool(stale_active and reference and stale_since and
                 reference - stale_since > timedelta(minutes=stale_after))
    stale_tag = f'<span class="tag-stale" title="No update for over {stale_after} minutes"{"" if stale else " hidden"}>Stale</span>'
    relative_html = f'<span data-relative-time>{html.escape(relative)}</span>'
    merge_html = f'<span class="merge-meta" title="Merge commit"><span>Merge</span> <code>{html.escape(merge_commit)}</code></span>' if merge_commit else ""
    fresh_html = (f'<span class="fresh">Updated {time_html} · {relative_html}{merge_html}{stale_tag}</span>'
                  if time_html else '<span class="fresh">No update recorded</span>')
    timeline_steps = render_workflow(active, groups, fixed_count, threshold, gate_met, in_progress, round_number,
                                    tracking_data.get("stages_reached") or [], rollover_cycle(tracking_data),
                                    introspect_record_for_round(tracking_data), report_exception)
    plan_view = render_plan(data, tracking_data, plan, round_number, render_now, displays)
    fix_order = tracking_data.get("fix_order") if isinstance(tracking_data.get("fix_order"), dict) else None
    if fix_order and fix_order.get("ids"):
        plan_view += (f'<p class="fix-order">Fix order: {html.escape(" → ".join(map(str, fix_order["ids"])))}'
                      f' · set by {html.escape(str(fix_order.get("by", "")))} · {html.escape(str(fix_order.get("reason", "")))}</p>')
    viewer_html = (f'<div class="viewer-footer">Live view, viewer {html.escape(str(viewer["version"]))}, started {html.escape(str(viewer["started"]))}'
                   f'<details><summary>Details</summary><p>Report path: {html.escape(str(viewer["path"]))}</p></details></div>'
                   if viewer else '<p class="viewer-footer">Viewer version not recorded</p>')
    latest_html = ""
    latest_step = None
    for group in groups:
        job = lane_job(group)
        step = job.get("latest_step") if job and isinstance(job.get("latest_step"), dict) else None
        if step and (latest_step is None or str(step.get("at") or "") >= str(latest_step.get("at") or "")):
            latest_step = step
    if latest_step and latest_step.get("step"):
        label = latest_step["step"]
        if latest_step.get("result"):
            label = f"{label}: {latest_step['result']}"
        when = ist_time(latest_step.get("at")) or ""
        latest_html = f'<p class="now-line">{html.escape(" · ".join(part for part in (when, label) if part))}</p>'
    timeline = (f'<section class="workflow" aria-label="Workflow progress"><div class="workflow-heading"><strong>Workflow · last recorded</strong>{fresh_html}</div>'
                f'{latest_html}{timeline_steps}{plan_view}{viewer_html}</section>')
    group_of = {identifier: {"id": str(group.get("id")), "name": str(group.get("name") or group.get("label") or group.get("id"))}
                for group in groups for identifier in group.get("finding_ids", []) if isinstance(identifier, str)}
    for item in output:
        item["_lane_status"] = lanes.get(item["id"], finding_lane_status(item, None, round_number))
        if item["id"] in group_of:
            item["_group"] = group_of[item["id"]]
        item["_plugin"] = plugin_of(item)
    order = tracking_data.get("fix_order") if isinstance(tracking_data.get("fix_order"), dict) else {}
    rank = {identifier: index for index, identifier in enumerate(order.get("ids") or []) if isinstance(identifier, str)}
    output.sort(key=lambda item: rank.get(item["id"], len(rank)))
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
