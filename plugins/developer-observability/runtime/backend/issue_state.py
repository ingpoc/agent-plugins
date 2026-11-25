"""Shared issue lifecycle and scope rules for the viewer and agent API."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


STATES = ("untested", "in_progress", "blocked", "parked", "resolved", "regressed")
ORIGINS = ("new", "carried")
WINDOW_REASONS = ("regressed", "new", "reoccurred", "fixed", "worked", "inconclusive")
TERMINAL = {"resolved", "verified", "verified_fixed", "fixed", "closed", "done"}
FAIL_KINDS = {"fail", "review_failed", "owner_rejected"}
PASS_KINDS = {"pass", "resolved"}
INCONCLUSIVE_KINDS = {"inconclusive"}
IST = timezone(timedelta(hours=5, minutes=30))
MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
RELATIVE_RE = re.compile(r"^now(?:-(?P<count>\d+)(?P<unit>m|h|d))?$", re.IGNORECASE)
OBSERVED_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(?P<year>\d{4})"
    r"(?:(?:,)?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
    re.IGNORECASE,
)
PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[^/\s`'\"]+)+/([^/\s`'\":]+)(:\d+)?")
RELATIVE = re.compile(r"^(?:now(?:-\d+[mhd])?)$", re.IGNORECASE)


def instant(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def resolve_instant(value, now=None):
    """Accept ISO-8601 instants or relative forms like now and now-6h."""
    now = now or datetime.now(timezone.utc)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = RELATIVE_RE.fullmatch(text)
    if match:
        if not match.group("count"):
            return now
        count = int(match.group("count"))
        unit = match.group("unit").lower()
        delta = timedelta(minutes=count) if unit == "m" else timedelta(hours=count) if unit == "h" else timedelta(days=count)
        return now - delta
    return instant(text)


def parse_observed(text):
    """Read a first-seen instant from tracker observed prose (IST wall time)."""
    if not isinstance(text, str) or not text.strip():
        return None
    match = OBSERVED_RE.search(text)
    if not match:
        return None
    month = MONTHS[match.group("month")[:3].lower()]
    hour = int(match.group("hour") or 0)
    minute = int(match.group("minute") or 0)
    try:
        return datetime(int(match.group("year")), month, int(match.group("day")), hour, minute, tzinfo=IST)
    except ValueError:
        return None


def iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def strip_paths(text):
    if not isinstance(text, str) or not text:
        return text

    def repl(match):
        name = match.group(1) + (match.group(2) or "")
        start = match.start()
        previous = text[start - 1] if start else ""
        # Keep a slash when the match ate the rest of a relative path.
        if start > 0 and text[start] == "/" and previous not in "/ \t\n`'\"[(<":
            return "/" + name
        return name

    return PATH_RE.sub(repl, text)


def clean_ref(ref):
    if not ref or not isinstance(ref, str):
        return None
    if ref.endswith(":None") or ref.endswith(":null") or ":None" in ref:
        return None
    if ref.startswith("tracker:"):
        return None
    return ref


def _event(kind, at, summary, ref=None):
    if not at:
        return None
    text = strip_paths(str(summary or kind)).strip()
    if text.startswith("- "):
        text = text[2:].strip()
    return {"kind": kind, "at": at, "summary": text, "ref": clean_ref(ref)}


PASS_CHECK_STATES = {"fixed", "passed", "pass", "resolved"}
NO_VERDICT_CHECK_STATES = {"inconclusive", "blocked", "stopped", "superseded", "cancelled", "canceled", "aborted",
                           "interrupted", "skipped"}
_NO_VERDICT_TEXT = re.compile(r"(?i)^\s*run stopped\b|\bsuperseded by\b|\bstill active\b")
CORROBORATION_SECONDS = 300


def check_outcome(check):
    """One verdict per verification check: pass, fail, inconclusive, or None while it is still running.
    A stopped, superseded or still-active run is no verdict at all: neither a fail nor a verify."""
    if not isinstance(check, dict):
        return None
    status = str(check.get("state") or check.get("status") or "").strip().lower()
    if status in {"pending", "checking", "running", "active", ""}:
        return None
    stopped = check.get("stopped")
    if (stopped not in (None, False, "", "false", "False") or status in NO_VERDICT_CHECK_STATES
            or any(_NO_VERDICT_TEXT.search(str(check.get(key) or "")) for key in ("evidence", "observation", "summary"))):
        return "inconclusive"
    return "pass" if status in PASS_CHECK_STATES else "fail"


def _verification_sourced(source):
    text = str(source or "").strip().lower()
    return "verif" in text or text.startswith("check")


def credible_regression(change, events):
    """A recorded move to regressed counts when the owner rejected it, when it came from somewhere other than a
    verification check, or when a real failing check for the issue sits next to it. A tracker that writes
    regressed for a stopped or superseded run (inconclusive checks) does not move the state."""
    if not isinstance(change, dict) or change.get("to") != "regressed":
        return True
    if change.get("source") == "owner_rejected" or not _verification_sourced(change.get("source")):
        return True
    at = instant(change.get("at"))
    if at is None:
        return False
    return any(event.get("kind") == "fail" and str(event.get("ref") or "").startswith("check:") and instant(event.get("at"))
               and abs((instant(event["at"]) - at).total_seconds()) <= CORROBORATION_SECONDS for event in events or ())


def _as_ids(value):
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value if item}
    if isinstance(value, str) and value:
        return {value}
    return set()


def _lane_status(lane):
    if not isinstance(lane, dict):
        return ""
    return str(lane.get("state") or lane.get("status") or "").strip().lower()


def _had_prior_pass(finding, events=(), prior=None):
    if prior and str((prior or {}).get("status") or "").strip().lower() in TERMINAL:
        return True
    verification = str(finding.get("verification") or "").lower()
    if "sign-off" in verification or "earlier" in verification and "withdrawn" in verification:
        return True
    if "rejected" in verification and ("sign-off" in verification or "withdrawn" in verification):
        return True
    evidence = finding.get("verification_evidence")
    if isinstance(evidence, list) and evidence:
        blob = " ".join(str(row) for row in evidence).lower()
        if "ready to ship" in blob or "sign-off" in blob or "concluded the page is ready" in blob:
            return True
    passes = [instant(event.get("at")) for event in events if event.get("kind") in PASS_KINDS]
    failures = [instant(event.get("at")) for event in events if event.get("kind") in {"fail", "review_failed", "owner_rejected"}]
    valid_passes = [stamp for stamp in passes if stamp]
    valid_failures = [stamp for stamp in failures if stamp]
    if valid_passes and (not valid_failures or min(valid_passes) < max(valid_failures)):
        return True
    return False


def lifecycle_state(finding, events=(), lane=None, prior=None):
    """Compute current state from tracker status, recorded transitions and evidence."""
    status = str(finding.get("status_raw") or finding.get("status") or "").strip().lower()
    transitions = finding.get("state_changes") if isinstance(finding.get("state_changes"), list) else []
    transitions = [row for row in transitions if credible_regression(row, events)]
    rejected = any(isinstance(row, dict) and (row.get("to") == "regressed" or row.get("source") == "owner_rejected")
                   for row in transitions)
    verification = str(finding.get("verification") or "").lower()
    if "withdrawn" in verification or ("rejected" in verification and "sign-off" in verification):
        rejected = True
    terminal = status in TERMINAL
    failures = [event for event in events if event.get("kind") in {"fail", "review_failed", "owner_rejected"}]
    resolved_at = instant(finding.get("resolved_at") or finding.get("fixed_at") or finding.get("verified_at") or finding.get("closed_at"))
    if resolved_at is None:
        resolved_rows = [instant(row.get("at")) for row in transitions if isinstance(row, dict) and row.get("to") == "resolved"]
        resolved_at = max((value for value in resolved_rows if value), default=None)
    later_failure = bool(resolved_at and any(instant(event.get("at")) and instant(event.get("at")) > resolved_at
                                             for event in failures))
    prior_pass = _had_prior_pass(finding, events, prior)
    failing_now = status in {"failing", "failed", "blocked", "changes_requested", "incomplete"} or bool(failures)
    if rejected or later_failure or (prior_pass and failing_now and not terminal):
        return "regressed"
    if terminal:
        return "resolved"
    lane_status = _lane_status(lane)
    if status in {"parked", "deferred"} or lane_status == "deferred":
        return "parked"
    # A temporarily parked fix lane must not rewrite an untested finding (DO-12).
    if lane_status == "parked" and status not in {"untested", "pending", ""}:
        return "parked"
    if status in {"blocked", "failed", "failing", "changes_requested", "incomplete"} or failures:
        return "blocked"
    if status in {"running", "in_progress", "active", "review", "fix", "working"}:
        return "in_progress"
    if lane_status in {"running", "review", "installing", "verifying", "in_progress"}:
        return "in_progress"
    return "untested"


def origin(first_seen, pr, identifier=None, unresolved_ids=(), carried_from=None):
    if identifier and (str(identifier) in _as_ids(unresolved_ids) or str(identifier) in _as_ids(carried_from) or carried_from is True):
        return "carried"
    first_pr = first_seen.get("pr") if isinstance(first_seen, dict) else None
    if first_pr is not None and str(first_pr).lstrip("#") != str(pr).lstrip("#"):
        return "carried"
    return "new"


def parse_window(start, end, now=None):
    now = now or datetime.now(timezone.utc)
    resolved_end = resolve_instant(end, now) if end is not None else now
    resolved_start = resolve_instant(start, now) if start is not None else (resolved_end - timedelta(hours=24) if resolved_end else None)
    if resolved_start is None or resolved_end is None or resolved_start > resolved_end:
        raise ValueError("invalid_time_window: from must be before to")
    return resolved_start, resolved_end


def window_kind(start, end):
    start_text = start if isinstance(start, str) else ""
    end_text = end if isinstance(end, str) else ""
    if RELATIVE.fullmatch(start_text or "") or RELATIVE.fullmatch(end_text or ""):
        return "sliding" if (start_text or "").lower() != "now" or (end_text or "").lower() == "now" else "fixed"
    if (start_text or "").lower().startswith("now-") or (end_text or "").lower() == "now":
        return "sliding"
    return "fixed"


def window_reason(issue, start, end):
    """Classify issue activity in a project-wide window; return None when quiet."""
    start, end = instant(start) or start, instant(end) or end
    first = instant((issue.get("first_seen") or {}).get("at"))
    events = [event for event in issue.get("events", []) if instant(event.get("at"))]
    inside = [event for event in events if start <= instant(event["at"]) <= end]
    failures = [event for event in inside if event.get("kind") in FAIL_KINDS]
    passes_before = any(instant(event.get("at")) < start and event.get("kind") in PASS_KINDS
                        for event in events) or any(
        instant(event.get("at")) < instant(failures[0]["at"]) and event.get("kind") in PASS_KINDS
        for event in events if failures and instant(failures[0].get("at")))
    if failures and (passes_before or _had_prior_pass(issue.get("raw") or issue, events)):
        return "regressed"
    if failures:
        return "reoccurred"
    if any(event.get("kind") in PASS_KINDS for event in inside):
        return "fixed"
    if any(event.get("kind") in INCONCLUSIVE_KINDS for event in inside):
        return "inconclusive"
    if first and start <= first <= end:
        return "new"
    if any(event.get("kind") == "job" for event in inside):
        return "worked"
    return None


def _parse_state_filter(state):
    if not state:
        return set(), set()
    accepted, negated = set(), set()
    for part in state.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("!"):
            negated.add(part[1:].strip())
        else:
            accepted.add(part)
    invalid = (accepted | negated) - set(STATES) - set(ORIGINS)
    if invalid or not (accepted | negated):
        raise ValueError("invalid_state: use " + ", ".join((*STATES, *ORIGINS)) + " or !state")
    if "" in negated:
        raise ValueError("invalid_state: use " + ", ".join((*STATES, *ORIGINS)) + " or !state")
    return accepted, negated


def select_scope(issues, scope="now", state=None, start=None, end=None, now=None, scope_pr=None, scope_round=None):
    now = now or datetime.now(timezone.utc)
    if start is not None or end is not None:
        start, end = parse_window(start, end, now)
        selected = []
        for issue in issues:
            reason = window_reason(issue, start, end)
            if reason:
                events = [event for event in issue.get("events", []) if instant(event.get("at")) and start <= instant(event["at"]) <= end]
                selected.append({**issue, "window_reason": reason, "window_events": events})
        selected.sort(key=lambda row: (row["window_reason"] == "regressed", row["window_reason"] == "new",
                                      row.get("last_seen_at") or ""), reverse=True)
    else:
        selected = list(issues)
        if scope == "now":
            pass
        elif scope.startswith("round:") and scope != "round:current":
            try:
                number = int(scope.split(":", 1)[1])
            except ValueError:
                raise ValueError("invalid_scope: use now, round:current, round:<number>, or all") from None
            selected = [issue for issue in selected if _issue_in_round(issue, number, scope_pr)]
        elif scope not in {"now", "round:current", "all"}:
            raise ValueError("invalid_scope: use now, round:current, round:<number>, or all")
        if scope == "round:current":
            selected = [issue for issue in selected if _issue_in_round(issue, issue.get("current_round"), scope_pr or issue.get("pr"))]
        selected.sort(key=lambda row: (row.get("state") == "regressed", row.get("origin") == "new",
                                      row.get("last_seen_at") or ""), reverse=True)
    if state:
        accepted, negated = _parse_state_filter(state)
        def allowed(issue):
            current = issue.get("state")
            source = issue.get("origin")
            if accepted and current not in accepted and source not in accepted:
                return False
            if current in negated or source in negated:
                return False
            return True
        selected = [issue for issue in selected if allowed(issue)]
    return selected


def _issue_in_round(issue, number, scope_pr=None):
    if number is None:
        return False
    try:
        number = int(number)
    except (TypeError, ValueError):
        return False
    if scope_pr is not None and str(issue.get("pr") or "").lstrip("#") not in {str(scope_pr).lstrip("#"), str(scope_pr)}:
        return False
    if issue.get("round") == number:
        return True
    rounds = issue.get("rounds") or []
    if number in rounds or str(number) in {str(item) for item in rounds}:
        return True
    lane = issue.get("lane") or {}
    if lane.get("round") == number:
        return True
    existed = issue.get("existed_in") or []
    return number in existed


def first_seen_for(finding, tracker_pr, round_number, unresolved_ids=(), source_pr=None):
    stored = finding.get("first_seen") if isinstance(finding.get("first_seen"), dict) else {}
    observed_at = parse_observed(finding.get("observed"))
    stored_at = instant(stored.get("at")) if stored else instant(finding.get("first_seen_at"))
    created_at = instant(finding.get("created_at"))
    candidates = []
    if stored_at is not None:
        candidates.append((stored_at, stored.get("source") or "first_seen_at"))
    if created_at is not None:
        candidates.append((created_at, "created_at"))
    if observed_at is not None:
        candidates.append((observed_at, "observed_text"))
    if candidates:
        # Earliest wins; a later rewritten observed note must not replace an earlier structured time.
        at, source = min(candidates, key=lambda row: (row[0], 0 if row[1] != "observed_text" else 1))
    else:
        at, source = None, "legacy_gap"
    origin_pr = stored.get("pr") if stored.get("pr") is not None else finding.get("first_seen_pr", finding.get("origin_pr"))
    carried = finding.get("carried_from")
    if origin_pr is None and carried is not None and not isinstance(carried, list):
        origin_pr = carried if str(carried).lstrip("#").isdigit() else None
    identifier = str(finding.get("id") or "")
    if origin_pr is None and identifier in _as_ids(unresolved_ids):
        origin_pr = source_pr
    if origin_pr is None and identifier not in _as_ids(unresolved_ids):
        origin_pr = tracker_pr if at or created_at else None
    seen_round = stored.get("round") if stored.get("source") not in {None, "created_at", "legacy_gap"} else None
    return {"at": iso(at) if isinstance(at, datetime) else (stored.get("at") or finding.get("first_seen_at") or finding.get("created_at")),
            "pr": origin_pr, "round": seen_round, "source": source}


def _events_for(finding, group, jobs):
    events = []
    first = finding.get("first_seen")
    first_at = first.get("at") if isinstance(first, dict) else None
    if first_at:
        events.append(_event("first_seen", first_at, "First seen"))
    elif finding.get("created_at"):
        events.append(_event("first_seen", finding["created_at"], "First seen"))
    for key in ("failure_history", "review_history", "review_failures", "owner_reports"):
        rows = finding.get(key) if isinstance(finding.get(key), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            at = row.get("at") or row.get("timestamp") or row.get("created_at")
            source = str(row.get("source") or row.get("kind") or key)
            kind = "owner_rejected" if row.get("source") == "owner_rejected" or row.get("verdict") == "rejected" or row.get("result") == "rejected" else "review_failed" if "review" in source else "fail"
            reference = row.get("ref") or (f"job:{row['job_id']}" if row.get("job_id") else None) or (f"check:{row['check_id']}" if row.get("check_id") else None)
            events.append(_event(kind, at, row.get("reason") or row.get("summary") or source, reference))
    for key in ("verification_history", "checks"):
        rows = finding.get(key) if isinstance(finding.get(key), list) else []
        if isinstance(finding.get(key), dict):
            rows = list(finding[key].values())
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("state") or row.get("status") or "").lower()
            kind = check_outcome(row)
            if kind is None:
                continue
            at = row.get("finished_at") or row.get("updated_at") or row.get("at")
            events.append(_event(kind, at, row.get("evidence") or row.get("summary") or status, row.get("ref") or f"check:{finding.get('id')}"))
    status_raw = str(finding.get("status_raw") or finding.get("status") or "").strip().lower()
    if isinstance(group, dict) and status_raw in {"failing", "failed", "blocked", "changes_requested", "incomplete"}:
        for row in group.get("failure_history", []):
            if isinstance(row, dict):
                events.append(_event("fail", row.get("at") or row.get("timestamp"), row.get("reason") or "Lane failure",
                                     f"job:{row['job_id']}" if row.get("job_id") else None))
    for change in finding.get("state_changes", []) if isinstance(finding.get("state_changes"), list) else []:
        if isinstance(change, dict) and change.get("to") in {"resolved", "regressed"}:
            events.append(_event("owner_rejected" if change.get("source") == "owner_rejected" else change.get("to"),
                                 change.get("at"), change.get("source") or change.get("to"),
                                 change.get("ref") or (f"job:{change['job_id']}" if change.get("job_id") else None)))
    for job in jobs:
        at = job.get("finished_at") or job.get("started_at") or job.get("at")
        kind = "job"
        if job.get("event") == "finish" and job.get("exit_code") not in (None, 0):
            kind = "review_failed" if job.get("phase") == "review" else "fail"
        events.append(_event(kind, at, job.get("phase") or "Job", f"job:{job.get('job_id')}" if job.get("job_id") else None))
    return _sort_events(row for row in events if row)


def _sort_events(events):
    """Sort by instant, not raw strings, so +05:30 and Z timestamps stay in order."""
    def key(row):
        stamp = instant(row.get("at")) or datetime.min.replace(tzinfo=timezone.utc)
        return (stamp, 0 if row.get("kind") == "first_seen" else 1, row.get("kind") or "")
    return sorted(events, key=key)



def _looks_like_pr_number(value, known=None):
    text = str(value or "").lstrip("#")
    if not text.isdigit():
        return False
    if known is not None:
        return text in known
    # Dates such as 20261107 are not pull request numbers.
    if len(text) == 8 and text.startswith("20"):
        return False
    return 1 <= len(text) <= 5


def run_source_pr(run, tracker_pr=None, known_prs=None):
    """Attribute a run to its source tracker, or a -NNN suffix that is a known PR."""
    host = str(tracker_pr).lstrip("#") if tracker_pr is not None else None
    known = None
    if known_prs is not None:
        known = {str(p).lstrip("#") for p in known_prs if p is not None and str(p).lstrip("#")}
        if host:
            known.add(host)
    if not isinstance(run, dict):
        return host
    ident = str(run.get("id") or run.get("run_id") or "")
    match = re.search(r"-(\d+)$", ident)
    if match and _looks_like_pr_number(match.group(1), known):
        return match.group(1)
    recorded = run.get("pr")
    if recorded is not None and _looks_like_pr_number(recorded, known):
        return str(recorded).lstrip("#")
    return host


def resolve_run(runs, key, pr=None):
    """Pick a verification run by id, or the latest run for pr when key='last'."""
    usable = [run for run in runs if isinstance(run, dict) and (run.get("id") or run.get("run_id"))]
    if not usable or key in (None, ""):
        return None
    if str(key).lower() == "last":
        if pr not in (None, "", "current"):
            wanted = str(pr).lstrip("#")
            usable = [run for run in usable if str(run.get("pr") or "").lstrip("#") == wanted]
        if not usable:
            return None
        def sort_key(run):
            stamp = instant(run.get("started_at") or run.get("installed_at") or run.get("ended_at"))
            return stamp or datetime.min.replace(tzinfo=timezone.utc)
        return max(usable, key=sort_key)
    wanted = str(key)
    return next((run for run in usable if str(run.get("id") or "") == wanted or str(run.get("run_id") or "") == wanted), None)


def run_window(run):
    """Return (start, end) only when both bounds were recorded; never invent a range."""
    if not isinstance(run, dict):
        return None, None
    start = run.get("started_at") or run.get("installed_at") or run.get("start")
    end = run.get("ended_at") or run.get("finished_at") or run.get("end")
    if not end:
        stopped = run.get("stopped")
        end = stopped.get("at") if isinstance(stopped, dict) else None
    if start and end and instant(start) and instant(end):
        return start, end
    return start if start and instant(start) else None, None


def public_run(run):
    if not isinstance(run, dict):
        return None
    start, end = run_window(run)
    recorded = bool(start and end)
    return {"id": run.get("id") or run.get("run_id"), "run_id": run.get("run_id") or run.get("id"),
            "pr": run.get("pr"), "round": run.get("round"),
            "started_at": start or run.get("started_at") or run.get("installed_at"),
            "ended_at": end or run.get("ended_at"),
            "recorded": recorded, "status": run.get("status")}


def normalize_run(run):
    if not isinstance(run, dict):
        return None
    start = run.get("installed_at") or run.get("started_at") or run.get("start")
    checks = run.get("checks") if isinstance(run.get("checks"), dict) else {}
    finished = [value for check in checks.values() if isinstance(check, dict)
                for value in (check.get("finished_at"), check.get("updated_at")) if value]
    stopped = run.get("stopped")
    stop_at = stopped.get("at") if isinstance(stopped, dict) else None
    end = run.get("ended_at") or run.get("finished_at") or run.get("end") or stop_at or (max(finished) if finished else None)
    label_round = run.get("round")
    identifier = str(run.get("id") or "")
    match = re.search(r"(?:^|[^a-z])r(\d+)\b", identifier, re.IGNORECASE)
    if label_round is None and match:
        label_round = int(match.group(1))
    return {**run, "run_id": run.get("run_id") or run.get("id"), "started_at": start, "ended_at": end,
            "start": start, "end": end, "round": label_round}


def _round_span(history_rows, current_round, current_started, now=None):
    """Map round number → (start, end) using previous end as start when needed."""
    now = now or datetime.now(timezone.utc)
    spans = {}
    previous_end = None
    for row in history_rows:
        if not isinstance(row, dict) or not isinstance(row.get("round"), int):
            continue
        start = instant(row.get("started_at") or row.get("start")) or previous_end
        end = instant(row.get("ended_at") or row.get("end"))
        spans[row["round"]] = (start, end)
        previous_end = end or start
    if isinstance(current_round, int) and current_round not in spans:
        spans[current_round] = (instant(current_started) or previous_end, None)
    elif isinstance(current_round, int) and spans.get(current_round, (None, None))[0] is None:
        start, end = spans[current_round]
        spans[current_round] = (instant(current_started) or start, end)
    return spans


def _existed_in_round(finding, first_seen, number, spans):
    if number not in spans:
        return False
    start, end = spans[number]
    created = instant((first_seen or {}).get("at")) or instant(finding.get("created_at"))
    if created is None:
        return True
    if end is not None and created > end:
        return False
    return True


def _pr_key(value):
    text = str(value or "").lstrip("#")
    try:
        return int(text)
    except ValueError:
        return 0


def _dedupe_latest_pr(issues):
    chosen = {}
    order = []
    for issue in issues:
        identifier = issue.get("id")
        previous = chosen.get(identifier)
        if previous is None:
            chosen[identifier] = issue
            order.append(identifier)
        elif _pr_key(issue.get("pr")) >= _pr_key(previous.get("pr")):
            chosen[identifier] = issue
    return [chosen[identifier] for identifier in order if identifier in chosen]



def apply_cascade(lists, project, pr, round_id, change):
    """Reset PR and round when a higher cascade control changes.

    Changing the project selects that project's newest PR and its latest round.
    Changing the PR selects that PR's latest round. Changing the round keeps the PR.
    """
    projects = list((lists or {}).get("projects") or [])
    prs = list((lists or {}).get("prs") or [])
    if not isinstance(change, dict) or not change:
        raise ValueError("invalid_scope: cascade change is required")
    if "project" in change:
        project = change["project"]
        if project not in {row.get("id") for row in projects} and projects:
            raise ValueError("unknown_project: no tracker for this project")
        candidates = [row for row in prs if row.get("project") == project] or [row for row in prs]
        if not candidates:
            raise ValueError("tracker_unavailable: no tracker for this project")
        chosen = candidates[0]
        return {"project": project, "pr": str(chosen["pr"]).lstrip("#"), "round": chosen.get("latest_round")}
    if "pr" in change:
        wanted = str(change["pr"]).lstrip("#")
        match = next((row for row in prs if str(row.get("pr")).lstrip("#") == wanted), None)
        if match is None:
            raise ValueError("tracker_unavailable: no tracker for this pull request")
        return {"project": match.get("project") or project, "pr": wanted, "round": match.get("latest_round")}
    if "round" in change:
        value = change["round"]
        if value in (None, "", "current"):
            match = next((row for row in prs if str(row.get("pr")).lstrip("#") == str(pr).lstrip("#")), None)
            value = (match or {}).get("latest_round")
        else:
            try:
                value = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid_scope: use now, round:current, round:<number>, or all") from error
        return {"project": project, "pr": str(pr).lstrip("#") if pr is not None else pr, "round": value}
    raise ValueError("invalid_scope: cascade change is required")


def _activity_counts(issues, start, end, now=None):
    now = now or datetime.now(timezone.utc)
    finish = end or now
    begin = start
    if begin is None:
        return {"found": 0, "fixed": 0, "regressed": 0}
    found = fixed = regressed = 0
    for issue in issues:
        reason = window_reason(issue, begin, finish)
        if reason == "new":
            found += 1
        elif reason == "fixed":
            fixed += 1
        elif reason == "regressed":
            regressed += 1
    return {"found": found, "fixed": fixed, "regressed": regressed}



def _check_window_reason(check, issue):
    outcome = check_outcome(check)
    if outcome is None:
        return None
    if outcome == "pass":
        return "fixed"
    if outcome == "inconclusive":
        return "inconclusive"
    events = issue.get("events") or []
    if _had_prior_pass(issue.get("raw") or issue, events):
        return "regressed"
    return "reoccurred"


def _select_run_issues(issues, run, state=None):
    """Include every finished check in the run, including inconclusive results."""
    checks = run.get("checks") if isinstance(run.get("checks"), dict) else {}
    selected = []
    for issue in issues:
        check = checks.get(issue.get("id"))
        if not isinstance(check, dict):
            continue
        reason = _check_window_reason(check, issue)
        if reason is None:
            continue
        start, end = run_window(run)
        start_at, end_at = instant(start), instant(end)
        events = []
        if start_at and end_at:
            events = [event for event in issue.get("events", []) if instant(event.get("at")) and start_at <= instant(event["at"]) <= end_at]
        reason_text = check.get("evidence") or check.get("summary") or check.get("observation") or check.get("state")
        check_at = check.get("finished_at") or check.get("updated_at") or check.get("at")
        selected.append({**issue, "window_reason": reason, "window_events": events, "check_outcome": check.get("state"),
                         "check_reason": reason_text, "check_at": check_at})
    selected.sort(key=lambda row: (row["window_reason"] == "regressed", row["window_reason"] == "new",
                                  row.get("last_seen_at") or ""), reverse=True)
    if state:
        accepted, negated = _parse_state_filter(state)
        def allowed(issue):
            current = issue.get("state")
            source = issue.get("origin")
            if accepted and current not in accepted and source not in accepted:
                return False
            if current in negated or source in negated:
                return False
            return True
        selected = [issue for issue in selected if allowed(issue)]
    return selected


def _in_span(value, start_at, end_at):
    stamp = instant(value)
    if stamp is None:
        return False
    if start_at and stamp < start_at:
        return False
    if end_at and stamp > end_at:
        return False
    return True


def _at_or_before(value, end_at):
    """True when value has a timestamp at or before end_at (or any time, if the window has no end)."""
    return _in_span(value, None, end_at)


def _snapshot_at(raw, events, changes, end_at):
    """Tracker snapshot as of end_at: carried-in facts plus events, never live status from after the round."""
    resolved_at = None
    for key in ("resolved_at", "fixed_at", "verified_at", "closed_at"):
        stamp = instant(raw.get(key))
        if stamp and (end_at is None or stamp <= end_at):
            resolved_at = stamp if resolved_at is None else max(resolved_at, stamp)
    pass_at = max((instant(event.get("at")) for event in events
                   if event.get("kind") in PASS_KINDS and instant(event.get("at"))), default=None)
    last_fail = max((instant(event.get("at")) for event in events
                     if event.get("kind") in FAIL_KINDS and instant(event.get("at"))), default=None)
    latest_ok = max((stamp for stamp in (resolved_at, pass_at) if stamp), default=None)
    if latest_ok and (last_fail is None or last_fail <= latest_ok):
        status = "verified_fixed"
    else:
        status = ""
    snapshot = {**raw, "status": status, "status_raw": status, "verification": "",
                "verification_evidence": [], "state_changes": changes}
    for key in ("resolved_at", "fixed_at", "verified_at", "closed_at"):
        snapshot[key] = raw.get(key) if _at_or_before(raw.get(key), end_at) else None
    if status == "verified_fixed" and not snapshot.get("resolved_at") and latest_ok:
        snapshot["resolved_at"] = latest_ok.isoformat().replace("+00:00", "Z")
    return snapshot


def _bound_issue_to_round(issue, start, end, carried_to=None, *, seed_from_tracker=False):
    """State at a round's end = carried-in state plus every event replayed through ended_at.

    A merged PR's rounds seed from that tracker's final status. Open-PR past rounds
    never use live status from after the window (so a later fail cannot regress an earlier round).
    """
    end_at = instant(end)
    events = [event for event in (issue.get("events") or []) if _at_or_before(event.get("at"), end_at)]
    events = _sort_events(events)
    bounded = {**issue, "events": events}
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else issue
    changes = [row for row in (raw.get("state_changes") or [])
               if isinstance(row, dict) and _at_or_before(row.get("at"), end_at)]
    if seed_from_tracker:
        status = str(raw.get("status") or issue.get("status_raw") or "").strip().lower()
        snapshot = {**raw, "status": status, "state_changes": changes}
        for key in ("resolved_at", "fixed_at", "verified_at", "closed_at"):
            if instant(raw.get(key)) and end_at and instant(raw.get(key)) > end_at:
                snapshot[key] = None
    else:
        snapshot = _snapshot_at(raw, events, changes, end_at)
    state = lifecycle_state(snapshot, events, lane=None, prior=None)
    last_evidence = max((event for event in events if (event.get("ref") or "").startswith(("job:", "check:"))),
                        key=lambda row: instant(row.get("at")) or datetime.min.replace(tzinfo=timezone.utc),
                        default=None)
    failures = [event for event in events if event.get("kind") in FAIL_KINDS]
    last_seen_at = max((event["at"] for event in failures if event.get("at")), default=None)
    origin_label = issue.get("origin")
    if carried_to and state != "resolved":
        origin_label = "carried"
        bounded["carried_to"] = str(carried_to).lstrip("#")
        bounded["origin_label"] = f"carried → #{str(carried_to).lstrip('#')}"
    bounded.update(state=state, last_evidence=last_evidence, last_seen_at=last_seen_at,
                   needs_you=state in {"blocked", "regressed"} or bool(issue.get("wait") and (
                       not instant((issue.get("wait") or {}).get("since")) or _at_or_before(issue["wait"]["since"], end_at))),
                   origin=origin_label or issue.get("origin"),
                   attempts=len(failures) or 0)
    return bounded



def _fill_round_starts(rows, opened_by_pr=None):
    """Backfill missing started_at from the previous PR's last end, then this PR's opening."""
    opened_by_pr = opened_by_pr or {}
    ordered = sorted(
        (row for row in rows if isinstance(row, dict) and row.get("round") is not None),
        key=lambda row: (_pr_key(row.get("pr")), row.get("round") or 0),
    )
    previous_end = None
    filled = []
    index_by_key = {}
    for row in ordered:
        pr = str(row.get("pr"))
        started = row.get("started_at") or previous_end or opened_by_pr.get(pr)
        updated = {**row, "started_at": started}
        filled.append(updated)
        index_by_key[(pr, row.get("round"))] = updated
        previous_end = row.get("ended_at") or started or previous_end
    # Preserve original order, with filled starts.
    result = []
    for row in rows:
        if not isinstance(row, dict):
            result.append(row)
            continue
        key = (str(row.get("pr")), row.get("round"))
        result.append(index_by_key.get(key, row))
    return result


def build_issues(project, pr="current", scope="now", from_time=None, to_time=None, state=None, state_dir=None, round_id=None, run=None,
                 issue_id=None, include=None):
    """Read trackers and job events and return the shared UI/MCP issue contract."""
    try:
        from .round_view import (_fold_jobs, _history_for_cycle, _iter_trackers, _plan_groups,
                                 _round_from_group_id, _tracker_sort_key)
    except ImportError:
        from round_view import (_fold_jobs, _history_for_cycle, _iter_trackers, _plan_groups,
                                _round_from_group_id, _tracker_sort_key)

    timed = from_time is not None or to_time is not None
    all_trackers = []
    for item in _iter_trackers(project, state_dir):
        tracker_path, document, tracker_pr = item
        tracking = document.get("tracking") if isinstance(document.get("tracking"), dict) else {}
        repo = tracking.get("repo")
        if not repo and isinstance(tracking.get("worktree"), dict):
            repo = tracking["worktree"].get("repo") or tracking["worktree"].get("name")
        exact = (tracker_path.parent.name == project or tracker_path.parent.name.endswith(f"--{project}")
                 or tracking.get("project_id") == project
                 or (isinstance(repo, str) and Path(repo.rstrip("/")).name == project))
        if exact:
            all_trackers.append(item)
    if not all_trackers:
        raise ValueError("tracker_unavailable: no tracker for this project")
    latest_path, latest, latest_pr = max(all_trackers, key=_tracker_sort_key)
    requested_pr = None if pr in (None, "", "current") else str(pr).lstrip("#")
    scope_round = None
    if isinstance(scope, str) and scope.startswith("round:") and scope != "round:current":
        try:
            scope_round = int(scope.split(":", 1)[1])
        except ValueError:
            raise ValueError("invalid_scope: use now, round:current, round:<number>, or all") from None
    if round_id not in (None, ""):
        if str(round_id).lower() == "current":
            if scope in (None, "", "now"):
                scope = "round:current"
        else:
            try:
                scope_round = int(str(round_id).lstrip("R").lstrip("r"))
            except ValueError:
                raise ValueError("invalid_scope: use now, round:current, round:<number>, or all") from None
            scope = f"round:{scope_round}"

    def _meta(document):
        tracking = document.get("tracking") if isinstance(document.get("tracking"), dict) else {}
        plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else document.get("plan", {})
        return tracking, plan, tracking.get("round") or plan.get("round"), tracking.get("cycle") or plan.get("cycle")

    def _history_rows(document, tracker_pr):
        tracking, plan, round_number, cycle = _meta(document)
        history = tracking.get("round_history") if isinstance(tracking.get("round_history"), list) else []
        compact = _history_for_cycle(history, round_number, cycle)
        merged = tracking.get("merged") if isinstance(tracking.get("merged"), dict) else merged_by_pr.get(str(tracker_pr))
        is_open = (not merged) and str(tracker_pr) == str(latest_pr)
        merge_at = (merged or {}).get("merged_at") or (merged or {}).get("at") if merged else None
        rows = []
        previous_end = None
        for row in compact:
            started = row.get("started_at") or previous_end
            ended = row.get("ended_at")
            live = is_open and row.get("round") == round_number and not ended
            if not ended and row.get("round") == round_number and not is_open:
                ended = merge_at
            rows.append({**row, "pr": tracker_pr, "started_at": started, "ended_at": ended, "live": live})
            previous_end = ended or started
        if round_number is not None and all(row.get("round") != round_number for row in rows):
            ended = None if is_open else merge_at
            rows.append({"pr": tracker_pr, "round": round_number, "started_at": tracking.get("round_started_at") or previous_end,
                         "ended_at": ended, "live": is_open, "cycle": cycle})
        return rows

    latest_by_pr = {}
    merged_by_pr = {}
    cycle_by_pr = {}
    for item in all_trackers:
        tracking, plan, round_number, cycle = _meta(item[1])
        latest_by_pr[str(item[2])] = round_number
        cycle_by_pr[str(item[2])] = cycle
        merged = tracking.get("merged") if isinstance(tracking.get("merged"), dict) else None
        merged_by_pr[str(item[2])] = merged
    focus_pr = requested_pr or str(latest_pr)
    if (requested_pr and not timed and scope == "now" and scope_round is None
            and round_id in (None, "") and requested_pr != str(latest_pr) and latest_by_pr.get(requested_pr) is not None):
        scope_round = latest_by_pr[requested_pr]
        scope = f"round:{scope_round}"
    if scope_round is not None and requested_pr is None:
        owning = [item for item in all_trackers
                  if any(row.get("round") == scope_round for row in _history_rows(item[1], item[2]))]
        if owning:
            focus_pr = str(max(owning, key=_tracker_sort_key)[2])

    current = latest
    current_pr = latest_pr
    if requested_pr is not None:
        focused = [item for item in all_trackers if str(item[2]) == requested_pr]
        if not focused:
            raise ValueError("tracker_unavailable: no tracker for this pull request")
        _, current, current_pr = max(focused, key=_tracker_sort_key)
    elif scope_round is not None:
        focused = [item for item in all_trackers if str(item[2]) == str(focus_pr)]
        if focused:
            _, current, current_pr = max(focused, key=_tracker_sort_key)

    current_tracking, current_plan, current_round, current_cycle = _meta(current)
    if scope == "round:current":
        scope_round = current_round
        focus_pr = str(current_pr)

    source_trackers = all_trackers

    jobs = _fold_jobs(state_dir)
    prior_findings = {}
    built = []
    all_rounds = []
    all_runs = []
    seen_round_keys = set()
    known_prs = {str(item[2]).lstrip("#") for item in source_trackers}
    opened_by_pr = {}
    for item in source_trackers:
        tracking = item[1].get("tracking") if isinstance(item[1].get("tracking"), dict) else {}
        pull = tracking.get("pull_request") if isinstance(tracking.get("pull_request"), dict) else {}
        opened = pull.get("opened_at") or tracking.get("opened_at")
        if opened:
            opened_by_pr[str(item[2])] = opened
    # Fill starts before issue bounds so #121 R1 inherits #120's last end.
    prelim_rounds = []
    for _, document, tracker_pr in sorted(source_trackers, key=_tracker_sort_key):
        prelim_rounds.extend(_history_rows(document, tracker_pr))
    filled_rounds = _fill_round_starts(prelim_rounds, opened_by_pr)
    filled_by_key = {(str(row.get("pr")), row.get("round")): row for row in filled_rounds
                     if isinstance(row, dict)}
    for _, document, tracker_pr in sorted(source_trackers, key=_tracker_sort_key):
        tracking, plan, round_number, cycle = _meta(document)
        rollover = tracking.get("rollover") if isinstance(tracking.get("rollover"), dict) else {}
        unresolved_ids = _as_ids(rollover.get("unresolved_ids"))
        carried_from = rollover.get("carried_from") or rollover.get("source_pr")
        source_pr = rollover.get("source_pr")
        if isinstance(source_pr, dict):
            source_pr = source_pr.get("number") or source_pr.get("id")
        groups = _plan_groups(document)
        by_finding = {identifier: group for group in groups
                      for identifier in (group.get("finding_ids") if isinstance(group.get("finding_ids"), list) else [])}
        history_rows = [filled_by_key.get((str(row.get("pr")), row.get("round")), row)
                        for row in _history_rows(document, tracker_pr)]
        spans = _round_span(history_rows, round_number, tracking.get("round_started_at"))
        for row in history_rows:
            key = (str(row.get("pr")), row.get("round"))
            if key in seen_round_keys:
                continue
            seen_round_keys.add(key)
            all_rounds.append(row)
        runs = tracking.get("verification_history") if isinstance(tracking.get("verification_history"), list) else []
        if isinstance(tracking.get("verification_run"), dict):
            runs = [*runs, tracking["verification_run"]]
        for history_run in runs:
            normalized = normalize_run(history_run)
            if normalized:
                attributed = {**normalized, "pr": run_source_pr(normalized, tracker_pr, known_prs)}
                existing = next((row for row in all_runs if str(row.get("id") or row.get("run_id")) == str(attributed.get("id") or attributed.get("run_id"))), None)
                if existing:
                    # Prefer the source-tracker attribution already recorded; merge checks.
                    if isinstance(existing.get("checks"), dict) or isinstance(attributed.get("checks"), dict):
                        existing["checks"] = {**(attributed.get("checks") or {}), **(existing.get("checks") or {})}
                else:
                    all_runs.append(attributed)
        findings = document.get("findings") if isinstance(document.get("findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict) or not finding.get("id"):
                continue
            identifier = str(finding["id"])
            group = by_finding.get(identifier)
            issue_jobs = [job for job in jobs if identifier in (job.get("finding_ids") or [])]
            first = first_seen_for(finding, tracker_pr, round_number, unresolved_ids, source_pr)
            finding_for_events = {**finding, "first_seen": first}
            events = _events_for(finding_for_events, group, issue_jobs)
            verification_runs = tracking.get("verification_history") if isinstance(tracking.get("verification_history"), list) else []
            current_run = tracking.get("verification_run")
            if isinstance(current_run, dict):
                verification_runs = [*verification_runs, current_run]
            for history_run in verification_runs:
                checks = history_run.get("checks") if isinstance(history_run, dict) and isinstance(history_run.get("checks"), dict) else {}
                check = checks.get(identifier)
                if not isinstance(check, dict):
                    continue
                check_state = str(check.get("state") or "").lower()
                kind = check_outcome(check)
                if kind is None:
                    continue
                at = check.get("finished_at") or check.get("updated_at")
                if at:
                    events.append(_event(kind, at, check.get("evidence") or check.get("summary") or check_state,
                                         f"check:{history_run.get('id', 'current')}:{identifier}"))
            events = _sort_events(event for event in events if event)
            events = [event for event in events if not (event.get("kind") == "regressed" and _verification_sourced(event.get("summary"))
                      and not credible_regression({"to": "regressed", "source": event.get("summary"), "at": event.get("at")}, events))]
            lane_round = _round_from_group_id(group.get("id")) if group else None
            if lane_round is None and group:
                try:
                    lane_round = int(group.get("round")) if group.get("round") is not None else None
                except (TypeError, ValueError):
                    lane_round = None
            job_rounds = set()
            for job in issue_jobs:
                job_round = job.get("round")
                if job_round is None:
                    job_round = _round_from_group_id(job.get("group_id"))
                if isinstance(job_round, int):
                    job_rounds.add(job_round)
            existed = [number for number in spans if _existed_in_round(finding, first, number, spans)]
            home_round = lane_round or (first.get("round") if isinstance(first.get("round"), int) else None)
            if home_round is None and str(tracker_pr) == str(current_pr):
                home_round = round_number
            lane = None
            if group:
                lane = {"id": group.get("id"), "state": group.get("status"), "phase": group.get("phase"), "round": lane_round or round_number}
            computed = lifecycle_state(finding, events, lane=lane, prior=prior_findings.get(identifier))
            failures = [event for event in events if event["kind"] in {"fail", "review_failed", "owner_rejected"}]
            last_seen_at = max((event["at"] for event in failures if event.get("at")), default=None)
            evidence = [event for event in events if (event.get("ref") or "").startswith(("job:", "check:"))]
            latest_evidence = max(evidence, key=lambda row: row["at"], default=None)
            if latest_evidence:
                latest_evidence = {**latest_evidence, "summary": strip_paths(latest_evidence.get("summary") or "")}
            built.append({"id": identifier, "title": finding.get("title") or finding.get("summary") or identifier,
                          "area": finding.get("area") or finding.get("category"), "area_source": finding.get("area_source") or "derived",
                          "origin": origin(first, tracker_pr, identifier, unresolved_ids, finding.get("carried_from") or carried_from),
                          "origin_pr": first.get("pr"), "first_seen": first,
                          "state": computed, "status_raw": finding.get("status"), "last_seen_at": last_seen_at,
                          "last_evidence": latest_evidence, "attempts": len(failures) or finding.get("attempts", 0) or 0,
                          "round": home_round, "rounds": sorted(set(job_rounds) | ({lane_round} if lane_round else set())),
                          "existed_in": existed, "current_round": current_round if str(tracker_pr) == str(current_pr) else round_number,
                          "lane": lane, "wait": owner_wait(group),
                          "needs_you": computed in {"blocked", "regressed"} or owner_wait(group) is not None, "events": events,
                          "summary": strip_paths(finding.get("summary") or finding.get("description") or "No summary recorded."),
                          "impact": finding.get("impact"), "done_when": finding.get("done_when"),
                          "jobs": issue_jobs, "raw": finding, "pr": tracker_pr,
                          "_lane_plan": ({"lane": group.get("id"),
                                          "at": group.get("plan_recorded_at") or group.get("recorded_at") or plan.get("recorded_at"),
                                          "what": group.get("reason") or group.get("name"),
                                          "round": lane_round,
                                          "round_start": iso((spans.get(lane_round) or (None, None))[0]) if lane_round in spans else None}
                                         if group else None)})
            prior_findings[identifier] = finding

    catalog = built
    earliest_seen = {}
    for issue in catalog:
        identifier = issue.get("id")
        seen = issue.get("first_seen") if isinstance(issue.get("first_seen"), dict) else None
        if not identifier or not seen or not instant(seen.get("at")):
            continue
        previous = earliest_seen.get(identifier)
        if previous is None or instant(seen["at"]) < instant(previous["at"]) or (
            instant(seen["at"]) == instant(previous["at"]) and seen.get("source") != "observed_text" and previous.get("source") == "observed_text"
        ):
            earliest_seen[identifier] = seen
    for issue in catalog:
        identifier = issue.get("id")
        if identifier in earliest_seen:
            issue["first_seen"] = earliest_seen[identifier]
            if issue["first_seen"].get("pr") is not None:
                issue["origin_pr"] = issue["first_seen"]["pr"]
            issue["events"] = _sort_events(
                [{**event, "at": issue["first_seen"]["at"]} if event.get("kind") == "first_seen" else event
                 for event in issue.get("events") or []]
            )
            issue["origin"] = origin(issue["first_seen"], issue.get("pr"), identifier,
                                     unresolved_ids=(), carried_from=issue.get("origin") == "carried")
    now = datetime.now(timezone.utc)
    decorated_rounds = []
    issues_by_pr = {}
    for issue in catalog:
        issues_by_pr.setdefault(str(issue.get("pr")), []).append(issue)
    for row in all_rounds:
        start = instant(row.get("started_at"))
        end = instant(row.get("ended_at")) or now
        counts_for_round = _activity_counts(issues_by_pr.get(str(row.get("pr")), []), start, end, now)
        decorated_rounds.append({**row, **counts_for_round})
    all_rounds = decorated_rounds
    prs = []
    for item in sorted(all_trackers, key=_tracker_sort_key, reverse=True):
        _, document, tracker_pr = item
        tracking, plan, round_number, cycle = _meta(document)
        merged = merged_by_pr.get(str(tracker_pr))
        pr_issues = issues_by_pr.get(str(tracker_pr), [])
        open_count = sum(issue.get("state") != "resolved" for issue in pr_issues)
        pull = tracking.get("pull_request") if isinstance(tracking.get("pull_request"), dict) else {}
        prs.append({"project": project, "pr": str(tracker_pr), "label": pull.get("label") or f"#{tracker_pr}",
                    "cycle": cycle, "open": open_count, "status": "merged" if merged else "open",
                    "merged": bool(merged), "merged_at": (merged or {}).get("merged_at") if merged else None,
                    "latest_round": round_number, "live": not merged and str(tracker_pr) == str(latest_pr)})
    lists = {"projects": [{"id": project, "label": "Agent Plugins" if project == "agent-plugins" else project}],
             "prs": prs}
    if timed or scope == "all":
        built = _dedupe_latest_pr(catalog)
    else:
        built = [issue for issue in catalog if str(issue.get("pr")) == str(focus_pr)]
        if not built and not catalog:
            built = []
    run_note = None
    last_other_run = None
    run_unrecorded = False
    selected_run = None
    if run not in (None, ""):
        run_pr = requested_pr or (None if str(run).lower() != "last" else str(current_pr))
        if str(run).lower() == "last":
            selected_run = resolve_run(all_runs, "last", pr=run_pr)
            if selected_run is None:
                last_other_run = resolve_run(all_runs, "last")
                run_note = f"no #{str(run_pr).lstrip('#')} run yet"
            # A specific missing last run is not an invalid id.
        else:
            selected_run = resolve_run(all_runs, run)
            if selected_run is None:
                raise ValueError("invalid_run: no matching test run")
        if selected_run is not None:
            run_from, run_to = run_window(selected_run)
            if run_from and run_to:
                from_time, to_time = run_from, run_to
                timed = True
                built = _dedupe_latest_pr(catalog)
            else:
                run_unrecorded = True
    now = datetime.now(timezone.utc)
    if run_unrecorded or (run not in (None, "") and selected_run is None):
        selected = []
    elif selected_run is not None and not run_unrecorded:
        selected = _select_run_issues(built, selected_run, state=state)
    else:
        selected = select_scope(built, scope=scope, state=state, start=from_time, end=to_time, now=now,
                            scope_pr=focus_pr if (scope_round is not None or (not timed and scope != "all")) else None,
                            scope_round=scope_round)
    if scope_round is not None and not timed and not run_unrecorded:
        # Restrict to issues that existed in that round on the focused PR when membership is otherwise empty of dates.
        present = [issue for issue in selected if _issue_in_round(issue, scope_round, focus_pr)]
        if not present:
            present = [issue for issue in built if str(issue.get("pr") or "").lstrip("#") == str(focus_pr).lstrip("#")
                       and (not issue.get("existed_in") or scope_round in (issue.get("existed_in") or [])
                            or _issue_in_round(issue, scope_round, focus_pr)
                            or _existed_without_dates(issue, scope_round))]
            if state:
                present = select_scope(present, scope="all", state=state)
        selected = present

    trail_end = None
    if selected_run is not None and not run_unrecorded:
        # A run view shows the trail as it stood when the run ended (SPEC-trail r2).
        trail_end = instant(run_window(selected_run)[1])
    if scope_round is not None and selected and not timed and selected_run is None:
        focus_rounds = [row for row in all_rounds if str(row.get("pr")) == str(focus_pr) and row.get("round") == scope_round]
        round_row = focus_rounds[0] if focus_rounds else None
        if round_row and not round_row.get("live"):
            trail_end = instant(round_row.get("ended_at"))
            later_pr = None if str(focus_pr) == str(latest_pr) else latest_pr
            later_ids = {issue.get("id") for issue in catalog if str(issue.get("pr")) == str(later_pr)} if later_pr else set()
            merged = bool(merged_by_pr.get(str(focus_pr)))
            selected = [_bound_issue_to_round(issue, round_row.get("started_at"), round_row.get("ended_at"),
                                              later_pr if issue.get("id") in later_ids else None,
                                              seed_from_tracker=merged)
                        for issue in selected]

    gate = current_tracking.get("gate") if isinstance(current_tracking.get("gate"), dict) else {}
    verification = current_tracking.get("verification_run") if isinstance(current_tracking.get("verification_run"), dict) else {}
    threshold = next((source[key] for source in (verification, gate, current_tracking)
                      for key in ("merge_previous_pr_threshold", "merge_threshold")
                      if isinstance(source, dict) and isinstance(source.get(key), int)), None)
    counts = {name: sum(issue["state"] == name for issue in selected) for name in STATES}
    by_origin = {name: sum(issue["origin"] == name for issue in selected) for name in ORIGINS}
    by_reason = {name: sum(issue.get("window_reason") == name for issue in selected) for name in WINDOW_REASONS} if timed else None
    window = None
    quiet_excluded = None
    if run_unrecorded or (run not in (None, "") and selected_run is None):
        window = {"from": None, "to": None, "kind": "run", "rounds": [], "test_runs": [],
                  "run": public_run(selected_run) if selected_run else None, "recorded": False}
        if run_note:
            window["note"] = run_note
        if last_other_run:
            window["last_other_run"] = public_run(last_other_run)
        quiet_excluded = 0
    elif timed:
        start, end = parse_window(from_time, to_time, now)
        quiet_excluded = sum(1 for issue in built if window_reason(issue, start, end) is None)
        in_rounds = []
        for row in all_rounds:
            row_start = instant(row.get("started_at"))
            row_end = instant(row.get("ended_at")) or now
            if row_start and row_end and row_start <= end and row_end >= start:
                in_rounds.append({"pr": row.get("pr"), "round": row.get("round"), "started_at": row.get("started_at"),
                                  "ended_at": row.get("ended_at"), "live": row.get("live")})
            elif row_end and start <= row_end <= end and row_start is None:
                in_rounds.append({"pr": row.get("pr"), "round": row.get("round"), "started_at": row.get("started_at"),
                                  "ended_at": row.get("ended_at"), "live": row.get("live")})
        in_runs = []
        for run in all_runs:
            run_start = instant(run.get("started_at"))
            run_end = instant(run.get("ended_at")) or run_start
            if run_start and run_end and run_start <= end and run_end >= start:
                in_runs.append({"id": run.get("id"), "pr": run.get("pr"), "round": run.get("round"),
                                "started_at": run.get("started_at"), "ended_at": run.get("ended_at")})
        kind = "sliding" if any(isinstance(value, str) and RELATIVE.fullmatch(value.strip()) for value in (from_time, to_time)) else "fixed"
        if (isinstance(from_time, str) and from_time.lower() == "now") or False:
            kind = "sliding"
        window = {"from": start.isoformat(), "to": end.isoformat(), "kind": "run" if selected_run is not None else kind,
                  "rounds": in_rounds, "test_runs": in_runs,
                  "requested_from": from_time, "requested_to": to_time}
        if selected_run is not None:
            window["run"] = public_run(selected_run)

    last_fail = None
    for issue in selected:
        if issue.get("last_seen_at") and (issue.get("state") in {"blocked", "regressed"} or issue.get("attempts")):
            stamp = instant(issue["last_seen_at"])
            if stamp and (last_fail is None or stamp > instant(last_fail["at"])):
                last_fail = {"at": issue["last_seen_at"], "id": issue["id"]}

    pull = current_tracking.get("pull_request") if isinstance(current_tracking.get("pull_request"), dict) else None
    display_pr = str(current_pr if not timed else (requested_pr or latest_pr))
    display_round = scope_round if scope_round is not None and not timed else current_round
    count_payload = {"by_state": counts, "by_origin": by_origin}
    if by_reason is not None:
        count_payload["by_reason"] = by_reason
    if selected_run is not None and not run_unrecorded:
        count_payload["tested"] = len(selected)
        count_payload["fixed"] = sum(issue.get("window_reason") == "fixed" for issue in selected)
        count_payload["inconclusive"] = sum(issue.get("window_reason") == "inconclusive" for issue in selected)
    try:
        from .issue_trail import decorate, parse_include, read_trail_events, rollup
    except ImportError:
        from issue_trail import decorate, parse_include, read_trail_events, rollup
    includes = parse_include(include)
    detail = issue_id not in (None, "")
    if detail:
        wanted = str(issue_id)
        picked = [issue for issue in selected if issue.get("id") == wanted]
        if not picked:
            picked = [issue for issue in _dedupe_latest_pr(catalog) if issue.get("id") == wanted]
        if not picked:
            raise ValueError("invalid_issue: no issue with that id in this project")
        selected = [dict(picked[0])]
        includes = sorted(set(includes) | {"trail", "jobs"}, key=("trail", "jobs", "rollup").index)
    selected = [{key: value for key, value in issue.items()} for issue in selected]
    _carry_failures(selected, catalog, trail_end)
    rollup_rows = None
    if includes:
        emitted = read_trail_events(state_dir)
        try:
            from .round_view import _finding_for_group_id, _group_id_from_repo
        except ImportError:
            from round_view import _finding_for_group_id, _group_id_from_repo
        full_by_key = {(issue.get("id"), str(issue.get("pr"))): issue.get("events") or [] for issue in catalog}
        lane_members = {}
        for _, document, _pr in all_trackers:
            tracking_doc = document.get("tracking") if isinstance(document.get("tracking"), dict) else {}
            carried = tracking_doc.get("carried_gates") if isinstance(tracking_doc.get("carried_gates"), list) else []
            for group in [*_plan_groups(document), *carried]:
                if isinstance(group, dict) and group.get("id") and isinstance(group.get("finding_ids"), list):
                    lane_members.setdefault(str(group["id"]), set()).update(str(item) for item in group["finding_ids"] if item)
        known_ids = {issue.get("id"): issue for issue in catalog if issue.get("id")}
        credit_by_job = {}
        for job in jobs:
            lane_id = job.get("group_id") or _group_id_from_repo(job.get("repo"))
            if not lane_id:
                continue
            if str(lane_id) in lane_members:
                credit_by_job[job.get("job_id")] = lane_members[str(lane_id)]
            else:
                # No membership record for the lane: match its R<n>-<ID> name (R6-DO2 -> DO-2), as the round view does.
                match = _finding_for_group_id(str(lane_id), known_ids)
                if match:
                    credit_by_job[job.get("job_id")] = {match.get("id")}

        def lane_credit(job, issue):
            return issue.get("id") in credit_by_job.get(job.get("job_id"), ())

        def full_events_for(issue):
            return full_by_key.get((issue.get("id"), str(issue.get("pr"))))

        def inherited_for(issue):
            return issue.get("_inherited") or []

        def tracker_for(issue):
            return f"{str(issue.get('pr')).lstrip('#')}.json"

        def merged_for(issue):
            return (merged_by_pr.get(str(issue.get("pr"))) or {}).get("merged_at")

        def lane_plan_for(issue, linked):
            hint = issue.get("_lane_plan") or {}
            lane = hint.get("lane")
            if not lane:
                # No current lane: the first job on the lane the issue was in when it ran (derived).
                lane_jobs = [(instant(job.get("started_at") or job.get("at")), job) for job, link in linked
                             if link == "derived" and instant(job.get("started_at") or job.get("at"))]
                if not lane_jobs:
                    return None
                at, job = min(lane_jobs, key=lambda row: row[0])
                lane_id = job.get("group_id") or _group_id_from_repo(job.get("repo"))
                return {"lane": lane_id, "at": job.get("started_at") or job.get("at"), "source": f"first job on lane {lane_id}",
                        "what": None}
            if hint.get("at"):
                return {"lane": lane, "at": hint["at"], "source": f"{lane} plan recorded_at", "what": hint.get("what")}
            starts = [job.get("started_at") or job.get("at") for job, _ in linked
                      if job.get("group_id") == lane and instant(job.get("started_at") or job.get("at"))]
            if starts:
                return {"lane": lane, "at": min(starts, key=instant), "source": f"first job on lane {lane}", "what": hint.get("what")}
            if hint.get("round_start"):
                return {"lane": lane, "at": hint["round_start"], "source": f"R{hint.get('round')} plan start", "what": hint.get("what")}
            return None

        ctx = {"jobs": jobs, "emitted": emitted, "tracker_for": tracker_for, "lane_plan_for": lane_plan_for,
               "merged_for": merged_for, "lane_credit": lane_credit, "full_events_for": full_events_for,
               "inherited_for": inherited_for}
        decorate(selected, ctx, includes, end_at=trail_end, now=now)
        if "rollup" in includes:
            ordered = sorted({str(issue.get("pr")) for issue in catalog}, key=_pr_key)
            focus = str(focus_pr if not timed else (requested_pr or latest_pr))
            wanted_prs = [value for value in ordered if _pr_key(value) <= _pr_key(focus)][-2:][::-1]
            issue_sets = {value: [issue for issue in catalog if str(issue.get("pr")) == value] for value in wanted_prs}
            rollup_rows = rollup(issue_sets, all_rounds, ctx, now=now)
    for issue in selected:
        issue.pop("_lane_plan", None)
        issue.pop("_inherited", None)
        if not detail:
            # Heavy fields are served only by /api/issues/<id> (the drawer's Raw data box fetches them).
            issue.pop("raw", None)
            issue.pop("jobs", None)
    # Per-check run detail stays in the trackers; the issue rows already carry each check's outcome.
    all_runs = [_light_run(row) for row in all_runs]
    if detail:
        # One issue only: the drawer's Raw data box and agents asking for one id need nothing else.
        return {"schema_version": 1, "project": project, "pr": display_pr, "issue_id": str(issue_id),
                "include": includes, "issues": selected}
    return {"schema_version": 1, "project": project, "pr": display_pr, "cycle": current_tracking.get("cycle"),
            "round": display_round, "round_started_at": current_tracking.get("round_started_at"),
            "pull_request": ({**pull, "opened_at": pull.get("opened_at")} if pull else None),
            "lists": lists,
            "rounds": sorted(all_rounds, key=lambda row: (_pr_key(row.get("pr")), row.get("round") or 0)),
            "runs": all_runs,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "scope": scope if not timed else "window",
            "counts": count_payload,
            "window": window, "quiet_excluded": quiet_excluded,
            "run": public_run(selected_run) if selected_run is not None else None,
            "run_note": run_note,
            "last_other_run": public_run(last_other_run) if last_other_run is not None else None,
            "last_fail": last_fail,
            "gate": {"verified": verification.get("fixed") if isinstance(verification.get("fixed"), int) else sum(issue["state"] == "resolved" for issue in selected if not timed),
                     "threshold": threshold,
                     "threshold_source": "stored" if threshold is not None else None},
            "include": includes,
            **({"rollup": rollup_rows} if rollup_rows is not None else {}),
            "issues": selected}


def _light_run(run):
    if not isinstance(run, dict):
        return run
    return {key: value for key, value in run.items() if key not in {"checks", "install_evidence"}}


def _carry_failures(selected, catalog, end_at=None):
    """Carry failure history from earlier PRs' trackers for the same issue id. Output only:
    state and counts were already computed, so they do not move."""
    kinds = {"fail", "review_failed", "owner_rejected"}
    earlier = {}
    for issue in catalog:
        earlier.setdefault(issue.get("id"), []).append(issue)
    for issue in selected:
        own = issue.get("events") or []
        keys = {(event.get("kind"), event.get("at")) for event in own}
        carried = []
        for prior in earlier.get(issue.get("id"), []):
            if _pr_key(str(prior.get("pr"))) >= _pr_key(str(issue.get("pr"))):
                continue
            for event in prior.get("events") or []:
                stamp = instant(event.get("at"))
                if event.get("kind") not in kinds or not stamp or (end_at is not None and stamp > end_at):
                    continue
                if (event.get("kind"), event.get("at")) in keys:
                    continue
                keys.add((event.get("kind"), event.get("at")))
                carried.append({**event, "pr": prior.get("pr"), "carried": True})
        if not carried:
            continue
        issue["_inherited"] = carried
        issue["events"] = _sort_events([*own, *carried])
        failures = [event for event in issue["events"] if event.get("kind") in kinds and event.get("at")]
        issue["attempts"] = max(int(issue.get("attempts") or 0), len(failures))
        latest = max(failures, key=lambda event: instant(event["at"]))
        if not issue.get("last_seen_at") or instant(latest["at"]) > instant(issue["last_seen_at"]):
            issue["last_seen_at"] = latest["at"]


# The park convention writes the wait as the reason itself ("Needs Guru later round"); prose that merely
# mentions the owner somewhere in a bulk deferral is not a wait on them.
_OWNER_WAIT = re.compile(r"(?i)^\s*(?:needs|waiting on|waiting for|blocked on)\s+(?:guru|the owner|owner)\b")


def owner_wait(group):
    """A recorded wait on the owner: the lane is parked, blocked or deferred and its reason says it needs the
    owner, with no unblock after it was parked. Lane names and other prose are not a wait."""
    if not isinstance(group, dict):
        return None
    if str(group.get("status") or "").lower() not in {"parked", "blocked", "deferred"}:
        return None
    field = next((key for key in ("blocked_reason", "reason", "parked_reason")
                  if isinstance(group.get(key), str) and _OWNER_WAIT.search(group[key])), None)
    if field is None:
        return None
    since = group.get("parked_at") or group.get("updated_at") or group.get("stage_entered_at")
    unblocks = [instant(row.get("at")) for row in group.get("unblock_history") or [] if isinstance(row, dict)]
    unblocks = [stamp for stamp in unblocks if stamp]
    if instant(since):
        if any(stamp > instant(since) for stamp in unblocks):
            return None
    elif unblocks:
        return None
    return {"on": "owner", "since": since if instant(since) else None, "lane": group.get("id"),
            "reason": strip_paths(group[field])[:80], "source": f"tracking.plan.groups[{group.get('id')}].{field}"}


def _existed_without_dates(issue, number):
    return not issue.get("existed_in") and issue.get("round") == number
