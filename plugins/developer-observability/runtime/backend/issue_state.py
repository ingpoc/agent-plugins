"""Shared issue lifecycle and scope rules for the viewer and agent API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


STATES = ("untested", "in_progress", "blocked", "parked", "resolved", "regressed")
ORIGINS = ("new", "carried")


def instant(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def lifecycle_state(finding, events=()):
    """Compute current state from tracker status, recorded transitions and evidence."""
    status = str(finding.get("status_raw") or finding.get("status") or "").strip().lower()
    transitions = finding.get("state_changes") if isinstance(finding.get("state_changes"), list) else []
    rejected = any(isinstance(row, dict) and (row.get("to") == "regressed" or row.get("source") == "owner_rejected")
                   for row in transitions)
    terminal = status in {"resolved", "verified", "verified_fixed", "fixed", "closed", "done"}
    failures = [event for event in events if event.get("kind") in {"fail", "review_failed", "owner_rejected"}]
    resolved_at = instant(finding.get("resolved_at") or finding.get("fixed_at") or finding.get("verified_at") or finding.get("closed_at"))
    if resolved_at is None:
        resolved_rows = [instant(row.get("at")) for row in transitions if isinstance(row, dict) and row.get("to") == "resolved"]
        resolved_at = max((value for value in resolved_rows if value), default=None)
    if rejected or (terminal and resolved_at and any(instant(event.get("at")) and instant(event.get("at")) > resolved_at
                                                      for event in failures)):
        return "regressed"
    if terminal:
        return "resolved"
    if status in {"parked", "deferred"}:
        return "parked"
    if status in {"blocked", "failed", "changes_requested", "incomplete"} or failures:
        return "blocked"
    if status in {"running", "in_progress", "active", "review", "fix", "working"}:
        return "in_progress"
    if events or finding.get("attempts"):
        return "in_progress"
    return "untested"


def origin(first_seen, pr):
    first_pr = first_seen.get("pr") if isinstance(first_seen, dict) else None
    return "new" if first_pr is not None and str(first_pr).lstrip("#") == str(pr).lstrip("#") else "carried"


def parse_window(start, end, now=None):
    now = now or datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(hours=24)
    start = instant(start) if isinstance(start, str) else start
    end = instant(end) if isinstance(end, str) else end
    if start is None or end is None or start > end:
        raise ValueError("invalid_time_window: from must be before to")
    return start, end


def window_reason(issue, start, end):
    """Classify issue activity in a project-wide window; return None when quiet."""
    first = instant((issue.get("first_seen") or {}).get("at"))
    events = [event for event in issue.get("events", []) if instant(event.get("at"))]
    inside = [event for event in events if start <= instant(event["at"]) <= end]
    if first and start <= first <= end:
        return "new"
    failures = [event for event in inside if event.get("kind") in {"fail", "review_failed", "owner_rejected", "inconclusive"}]
    passes_before = any(instant(event.get("at")) < start and event.get("kind") in {"pass", "resolved"}
                        for event in events)
    if failures and passes_before:
        return "regressed"
    if failures:
        return "reoccurred"
    if any(event.get("kind") in {"pass", "resolved"} for event in inside):
        return "fixed"
    if any(event.get("kind") == "job" for event in inside):
        return "worked"
    return None


def select_scope(issues, scope="now", state=None, start=None, end=None, now=None):
    now = now or datetime.now(timezone.utc)
    if start is not None or end is not None:
        start, end = parse_window(start, end, now)
        selected = []
        for issue in issues:
            reason = window_reason(issue, start, end)
            if reason:
                selected.append({**issue, "window_reason": reason})
        selected.sort(key=lambda row: (row["window_reason"] == "regressed", row["window_reason"] == "new",
                                      row.get("last_seen_at") or ""), reverse=True)
    else:
        selected = list(issues)
        if scope == "now":
            selected = [issue for issue in selected if issue.get("state") != "resolved"]
        elif scope.startswith("round:") and scope != "round:current":
            try:
                number = int(scope.split(":", 1)[1])
            except ValueError:
                raise ValueError("invalid_scope: use now, round:current, round:<number>, or all") from None
            selected = [issue for issue in selected if issue.get("round") == number]
        elif scope not in {"now", "round:current", "all"}:
            raise ValueError("invalid_scope: use now, round:current, round:<number>, or all")
        if scope == "round:current":
            selected = [issue for issue in selected if issue.get("round") == issue.get("current_round")]
        selected.sort(key=lambda row: (row.get("state") == "regressed", row.get("origin") == "new",
                                      row.get("last_seen_at") or ""), reverse=True)
    if state:
        accepted = {part.strip() for part in state.split(",") if part.strip()}
        invalid = accepted - set(STATES) - set(ORIGINS)
        if invalid:
            raise ValueError("invalid_state: use " + ", ".join((*STATES, *ORIGINS)))
        selected = [issue for issue in selected if issue.get("state") in accepted or issue.get("origin") in accepted]
    return selected


def _event(kind, at, summary, ref=None):
    if not at:
        return None
    return {"kind": kind, "at": at, "summary": str(summary or kind), "ref": ref}


def _events_for(finding, group, jobs):
    events = []
    first = finding.get("first_seen")
    if isinstance(first, dict):
        events.append(_event("first_seen", first.get("at"), "First seen", f"tracker:{finding.get('id')}"))
    elif finding.get("created_at"):
        events.append(_event("first_seen", finding["created_at"], "First seen", f"tracker:{finding.get('id')}"))
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
            at = row.get("finished_at") or row.get("updated_at") or row.get("at")
            kind = "pass" if status in {"fixed", "passed", "pass", "resolved"} else "inconclusive" if status in {"inconclusive", "pending"} else "fail"
            events.append(_event(kind, at, row.get("summary") or status, row.get("ref") or f"check:{finding.get('id')}"))
    if isinstance(group, dict):
        for row in group.get("failure_history", []):
            if isinstance(row, dict):
                events.append(_event("fail", row.get("at") or row.get("timestamp"), row.get("reason") or "Lane failure", f"job:{row.get('job_id')}"))
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
        events.append(_event(kind, at, job.get("phase") or "Job", f"job:{job.get('job_id')}"))
    return sorted((row for row in events if row), key=lambda row: row["at"])


def build_issues(project, pr="current", scope="now", from_time=None, to_time=None, state=None, state_dir=None):
    """Read trackers and job events and return the shared UI/MCP issue contract."""
    from round_view import _fold_jobs, _iter_trackers, _plan_groups, _tracker_sort_key

    trackers = []
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
            trackers.append(item)
    if not trackers:
        raise ValueError("tracker_unavailable: no tracker for this project")
    selected_pr = None if pr in (None, "", "current") else pr
    if selected_pr is not None:
        trackers = [item for item in trackers if str(item[2]) == str(selected_pr).lstrip("#")]
        if not trackers:
            raise ValueError("tracker_unavailable: no tracker for this pull request")
    _, current, current_pr = max(trackers, key=_tracker_sort_key)
    current_tracking = current.get("tracking") if isinstance(current.get("tracking"), dict) else {}
    current_plan = current_tracking.get("plan") if isinstance(current_tracking.get("plan"), dict) else current.get("plan", {})
    current_round = current_tracking.get("round") or current_plan.get("round")
    jobs = _fold_jobs(state_dir)
    built = []
    for _, document, tracker_pr in trackers:
        if from_time is None and to_time is None and scope != "all" and str(tracker_pr) != str(current_pr):
            continue
        if selected_pr is not None and str(tracker_pr) != str(current_pr):
            continue
        tracking = document.get("tracking") if isinstance(document.get("tracking"), dict) else {}
        plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else document.get("plan", {})
        round_number = tracking.get("round") or plan.get("round")
        groups = _plan_groups(document)
        by_finding = {identifier: group for group in groups
                      for identifier in (group.get("finding_ids") if isinstance(group.get("finding_ids"), list) else [])}
        findings = document.get("findings") if isinstance(document.get("findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict) or not finding.get("id"):
                continue
            identifier = str(finding["id"])
            group = by_finding.get(identifier)
            issue_jobs = [job for job in jobs if identifier in (job.get("finding_ids") or [])]
            events = _events_for(finding, group, issue_jobs)
            verification_runs = tracking.get("verification_history") if isinstance(tracking.get("verification_history"), list) else []
            current_run = tracking.get("verification_run")
            if isinstance(current_run, dict):
                verification_runs = [*verification_runs, current_run]
            for run in verification_runs:
                checks = run.get("checks") if isinstance(run, dict) and isinstance(run.get("checks"), dict) else {}
                check = checks.get(identifier)
                if not isinstance(check, dict):
                    continue
                check_state = str(check.get("state") or "").lower()
                kind = "pass" if check_state in {"fixed", "passed", "pass", "resolved"} else "inconclusive" if check_state in {"inconclusive", "pending", "blocked"} else "fail"
                at = check.get("finished_at") or check.get("updated_at")
                if at:
                    events.append(_event(kind, at, check.get("summary") or check_state,
                                         f"check:{run.get('id', 'current')}:{identifier}"))
            events.sort(key=lambda row: row["at"])
            first = finding.get("first_seen")
            if not isinstance(first, dict):
                origin_pr = finding.get("first_seen_pr", finding.get("origin_pr"))
                carried_from = finding.get("carried_from")
                if origin_pr is None and carried_from is not None:
                    origin_pr = carried_from if str(carried_from).isdigit() else None
                first = {"at": finding.get("first_seen_at") or finding.get("created_at"),
                         "pr": origin_pr if origin_pr is not None else tracker_pr if finding.get("created_at") else None,
                         "round": finding.get("first_seen_round") or round_number if finding.get("created_at") else None,
                         "source": "first_seen_at" if finding.get("first_seen_at") else "created_at" if finding.get("created_at") else "legacy_gap"}
            computed = lifecycle_state(finding, events)
            failures = [event for event in events if event["kind"] in {"fail", "review_failed", "owner_rejected"}]
            last_seen = finding.get("last_seen")
            last_seen_at = last_seen.get("at") if isinstance(last_seen, dict) else last_seen if isinstance(last_seen, str) else None
            if failures:
                seen_candidates = [value for value in [last_seen_at, *(event["at"] for event in failures if event.get("at"))] if value]
                last_seen_at = max(seen_candidates, default=None)
            evidence = [event for event in events if (event.get("ref") or "").startswith(("job:", "check:"))]
            latest = max(evidence, key=lambda row: row["at"], default=None)
            built.append({"id": identifier, "title": finding.get("title") or finding.get("summary") or identifier,
                          "area": finding.get("area") or finding.get("category"), "area_source": finding.get("area_source") or "derived",
                          "origin": origin(first, tracker_pr), "origin_pr": first.get("pr"), "first_seen": first,
                          "state": computed, "status_raw": finding.get("status"), "last_seen_at": last_seen_at,
                          "last_evidence": latest, "attempts": len(failures) or finding.get("attempts", 0),
                          "round": round_number, "current_round": current_round if str(tracker_pr) == str(current_pr) else round_number,
                          "lane": ({"id": group.get("id"), "state": group.get("status"), "round": round_number} if group else None),
                          "needs_you": computed in {"blocked", "regressed"}, "events": events,
                          "summary": finding.get("summary") or finding.get("description") or "No summary recorded.",
                          "impact": finding.get("impact"), "done_when": finding.get("done_when"),
                          "jobs": issue_jobs, "raw": finding, "pr": tracker_pr})
    selected = select_scope(built, scope=scope, state=state, start=from_time, end=to_time)
    gate = current_tracking.get("gate") if isinstance(current_tracking.get("gate"), dict) else {}
    verification = current_tracking.get("verification_run") if isinstance(current_tracking.get("verification_run"), dict) else {}
    threshold = next((source[key] for source in (verification, gate, current_tracking)
                      for key in ("merge_previous_pr_threshold", "merge_threshold")
                      if isinstance(source, dict) and isinstance(source.get(key), int)), None)
    counts = {name: sum(issue["state"] == name for issue in built) for name in STATES}
    by_origin = {name: sum(issue["origin"] == name for issue in built) for name in ORIGINS}
    history = current_tracking.get("round_history") if isinstance(current_tracking.get("round_history"), list) else []
    round_rows = []
    previous_end = None
    for row in history:
        if not isinstance(row, dict):
            continue
        round_rows.append({**row, "started_at": row.get("started_at") or previous_end})
        previous_end = row.get("ended_at")
    if current_round is not None:
        round_rows.append({"round": current_round, "started_at": current_tracking.get("round_started_at"), "ended_at": None})
    runs = current_tracking.get("verification_history") if isinstance(current_tracking.get("verification_history"), list) else []
    if isinstance(current_tracking.get("verification_run"), dict):
        runs = [*runs, current_tracking["verification_run"]]
    return {"schema_version": 1, "project": project, "pr": current_pr, "cycle": current_tracking.get("cycle"),
            "round": current_round, "round_started_at": current_tracking.get("round_started_at"),
            "pull_request": ({**current_tracking["pull_request"], "opened_at": current_tracking["pull_request"].get("opened_at")}
                             if isinstance(current_tracking.get("pull_request"), dict) else None),
            "rounds": round_rows, "runs": runs,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "scope": scope, "counts": {"by_state": counts, "by_origin": by_origin},
            "gate": {"verified": verification.get("fixed"), "threshold": threshold,
                     "threshold_source": "stored" if threshold is not None else None}, "issues": selected}
