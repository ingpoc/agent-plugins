"""Read-only per-round view from the change-list tracker and job events."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_ROUND_RE = re.compile(r"^R(\d+)-", re.I)
_WORKTREE_ROUND_RE = re.compile(r"-r(\d+)-", re.I)


_ABS_PATH_RE = re.compile(
    r"(?:file://)?(/?(?:Users|home)/[^\s\]\)'\"<>]+)",
    re.I,
)
_MD_PATH_RE = re.compile(
    r"\[([^\]]+)\]\((?:file://)?(/?(?:Users|home)/[^)]+)\)",
    re.I,
)


def _public_text(value):
    """Drop absolute home paths from UI text; keep the visible label or basename."""
    if not isinstance(value, str) or not value:
        return value
    def md(match):
        return match.group(1) or Path(match.group(2)).name
    text = _MD_PATH_RE.sub(md, value)
    def bare(match):
        return Path(match.group(1).rstrip("/")).name or "file"
    return _ABS_PATH_RE.sub(bare, text)


def _view_status(group):
    """Single status every surface uses. The lane status wins over finding status."""
    status = str((group or {}).get("status") or "").strip().lower()
    if (group or {}).get("id") == "DEFER" and status not in {"parked", "deferred"}:
        return "deferred"
    return status or "unknown"


def _lane_why(group):
    status = _view_status(group)
    if status in {"parked", "deferred"}:
        target = group.get("parked_to")
        if target is not None:
            return f"Parked to round {target}"
        return _public_text(group.get("reason") or "This lane is parked.")
    if status == "blocked":
        return _public_text(group.get("blocked_reason") or group.get("reason") or "This lane is blocked.")
    reason = group.get("reason") or ""
    if re.search(r"(?i)\bparked\b", reason):
        return _public_text(group.get("blocked_reason") or "") or None
    return _public_text(reason) or None



def _state_root(state_dir=None):
    return Path(state_dir or os.environ.get("BCL_STATE_DIR", "~/.agents/state/batched-change-list")).expanduser()


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _pr_number(value):
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        for key in ("number", "id", "label", "url"):
            found = _pr_number(value.get(key))
            if found:
                return found
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    return match.group(1) if match else None


def find_tracker(project, pr=None, state_dir=None):
    """Locate the live tracker JSON for a project (and optional PR). Read only."""
    if not project or not isinstance(project, str):
        raise ValueError("unknown_project: project is required")
    root = _state_root(state_dir)
    wanted = _pr_number(pr)
    matches = []
    if not root.exists():
        raise ValueError("tracker_unavailable: no change-list state directory")
    for path in sorted(root.glob("*/*.json")):
        if path.name in {"archive.json", "parked.json"} or path.name.startswith("."):
            continue
        folder = path.parent.name
        if project not in folder and not folder.endswith(f"--{project}") and project not in path.stem:
            # still allow a later tracking.repo / label match after parse
            tentative = False
        else:
            tentative = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
        label = ""
        if isinstance(tracking.get("worktree"), dict):
            label = str(tracking["worktree"].get("repo") or tracking["worktree"].get("name") or "")
        repo = str(tracking.get("repo") or label or "")
        title = str(data.get("title") or "")
        belongs = (
            tentative
            or project == repo
            or project in repo
            or folder.endswith(f"--{project}")
            or project in folder
            or project in title
        )
        if not belongs:
            continue
        found_pr = _pr_number(tracking.get("pull_request")) or _pr_number(path.stem)
        if wanted and found_pr != wanted:
            continue
        matches.append((path, data, found_pr))
    if wanted:
        exact = [item for item in matches if item[2] == wanted or item[0].stem == wanted]
        if exact:
            matches = exact
    if not matches:
        raise ValueError("tracker_unavailable: no tracker for this project")
    # Prefer the highest PR number when unspecified, then the most recently written.
    def sort_key(item):
        path, data, found = item
        tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
        written = _parse_time(tracking.get("last_written_at")) or datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            pr_num = int(found or 0)
        except ValueError:
            pr_num = 0
        return (pr_num, written, path.stat().st_mtime)

    return max(matches, key=sort_key)


def _fold_jobs(state_dir=None):
    """Fold every job in events.jsonl by job_id. Includes incomplete jobs. Read only."""
    root = _state_root(state_dir)
    jobs = []
    if not root.exists():
        return jobs
    for path in sorted(root.glob("**/events.jsonl")):
        folded = {}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            job_id = item.get("job_id")
            if not job_id:
                continue
            folded[job_id] = {**folded.get(job_id, {}), **item}
        for item in folded.values():
            if item.get("event") == "start" and "finished_at" not in item:
                item = {**item, "event": "incomplete"}
            jobs.append(item)
    return jobs


def _round_from_group_id(group_id):
    match = _ROUND_RE.match(str(group_id or ""))
    return int(match.group(1)) if match else None


def _annotate_job(job, groups):
    """Attach group_id and round when present on the event, else infer from worktree."""
    group_id = job.get("group_id") if isinstance(job.get("group_id"), str) and job.get("group_id") else None
    round_number = job.get("round")
    if isinstance(round_number, str) and round_number.isdigit():
        round_number = int(round_number)
    if not isinstance(round_number, int):
        round_number = None
    repo = str(job.get("repo") or "")
    if not group_id:
        for group in groups:
            worktree = group.get("worktree")
            if worktree and repo and (repo == worktree or repo.endswith(f"/{worktree}") or worktree.endswith(f"/{repo}")):
                group_id = group.get("id")
                if round_number is None:
                    round_number = _round_from_group_id(group_id)
                break
    if round_number is None:
        if group_id:
            round_number = _round_from_group_id(group_id)
        if round_number is None:
            match = _WORKTREE_ROUND_RE.search(repo)
            if match:
                round_number = int(match.group(1))
    started = _parse_time(job.get("started_at") or job.get("at"))
    finished = _parse_time(job.get("finished_at"))
    duration_ms = None
    if started and finished:
        duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    tokens = job.get("tokens") if isinstance(job.get("tokens"), dict) else None
    return {
        "job_id": job.get("job_id"),
        "event": job.get("event"),
        "phase": job.get("phase"),
        "agent": job.get("agent"),
        "repo": job.get("repo"),
        "group_id": group_id,
        "round": round_number,
        "started_at": job.get("started_at") or job.get("at"),
        "finished_at": job.get("finished_at"),
        "exit_code": job.get("exit_code"),
        "tokens": tokens,
        "tokens_source": job.get("tokens_source"),
        "session": job.get("session"),
        "duration_ms": duration_ms,
        "model": job.get("model"),
        "group_id_inferred": bool(group_id and not job.get("group_id")),
    }


def _group_payload(group, findings_by_id):
    finding_ids = [str(x) for x in (group.get("finding_ids") or []) if x]
    findings = []
    for fid in finding_ids:
        item = findings_by_id.get(fid)
        if not item:
            findings.append({"id": fid})
            continue
        findings.append({
            "id": fid,
            "title": item.get("title"),
            "status": item.get("status"),
            "priority": item.get("priority"),
            "summary": _public_text(item.get("summary")),
            "observed": _public_text(item.get("observed")),
            "recommendation": _public_text(item.get("recommendation")),
        })
    verify = group.get("verify") if isinstance(group.get("verify"), dict) else None
    presuite = group.get("presuite") if isinstance(group.get("presuite"), dict) else None
    payload = {
        "id": group.get("id"),
        "name": group.get("name"),
        "label": group.get("label"),
        "status": group.get("status"),
        "phase": group.get("phase"),
        "finding_ids": finding_ids,
        "findings": findings,
        "worktree": group.get("worktree"),
        "reason": group.get("reason"),
        "stage_entered_at": group.get("stage_entered_at"),
        "updated_at": group.get("updated_at"),
        "verify": verify,
        "presuite": presuite,
        "failure_history": [
            ({**item, "reason": _public_text(item.get("reason"))} if isinstance(item, dict) else item)
            for item in (group.get("failure_history") or [])
        ] if isinstance(group.get("failure_history"), list) else [],
        "codex": group.get("codex") if isinstance(group.get("codex"), dict) else {},
        "files": group.get("files") if isinstance(group.get("files"), list) else [],
        "blocked_reason": _public_text(group.get("blocked_reason")),
        "parked_to": group.get("parked_to"),
    }
    payload["reason"] = _public_text(payload.get("reason"))
    payload["view_status"] = _view_status(payload)
    payload["why"] = _lane_why(payload)
    return payload


def _status_counts(findings):
    counts = {}
    for item in findings:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts



def _infer_history_cycles(history):
    """Split stored round_history into cycles. A round number that does not increase starts a new cycle."""
    cycles = []
    current = []
    previous = None
    for item in history:
        if not isinstance(item, dict):
            continue
        round_number = item.get("round")
        if not isinstance(round_number, int):
            continue
        if previous is not None and round_number <= previous:
            if current:
                cycles.append(current)
            current = []
        current.append({
            "round": round_number,
            "fixed": item.get("fixed"),
            "ended_at": item.get("ended_at"),
        })
        previous = round_number
    if current:
        cycles.append(current)
    return cycles


def _history_for_cycle(history, current_round, current_cycle):
    """Chips for one cycle only, one row per round, plus the live round if it has not ended."""
    cycles = _infer_history_cycles(history)
    selected = cycles[-1] if cycles else []
    if isinstance(current_cycle, int) and cycles:
        # Prefer the trailing cycle; label it with the tracker's current cycle when it matches the tail.
        selected = cycles[-1]
    by_round = {}
    for item in selected:
        by_round[item["round"]] = {**item, "cycle": current_cycle, "live": False}
    compact = [by_round[key] for key in sorted(by_round)]
    if isinstance(current_round, int) and current_round not in by_round:
        compact.append({"round": current_round, "fixed": None, "ended_at": None, "cycle": current_cycle, "live": True})
    for item in compact:
        item.setdefault("cycle", current_cycle)
        item.setdefault("live", False)
    return compact


def _round_groups(groups, jobs, selected_round, current_round):
    """Lanes that belong to the selected round. Past rounds are reconstructed from jobs when the live plan has moved on."""
    if selected_round is None or selected_round == current_round:
        return list(groups)
    matching = [group for group in groups if _round_from_group_id(group.get("id")) == selected_round]
    seen = {group.get("id") for group in matching}
    for job in jobs:
        job_round = job.get("round")
        if job_round is None and job.get("group_id"):
            job_round = _round_from_group_id(job.get("group_id"))
        if job_round != selected_round:
            continue
        group_id = job.get("group_id")
        if not group_id or group_id in seen:
            continue
        matching.append({
            "id": group_id,
            "name": group_id,
            "label": group_id,
            "status": "historical",
            "view_status": "historical",
            "phase": job.get("phase"),
            "finding_ids": [],
            "reason": None,
            "why": None,
        })
        seen.add(group_id)
    matching.sort(key=lambda group: str(group.get("id") or ""))
    return matching

def _rank_needs(groups, findings_by_id, parked, jobs, selected_round):
    """Plugin ranks the cards. The UI must not re-sort."""
    cards = []

    def add(priority, kind, title, why, **extra):
        fact = extra.pop("fact", why)
        gap = extra.pop("gap", None)
        guess = extra.pop("guess", None)
        evidence = extra.pop("evidence", None)
        action = extra.pop("action", None)
        cards.append({
            "priority": priority,
            "kind": kind,
            "title": title,
            "why": _public_text(why),
            "fact": _public_text(fact),
            "gap": _public_text(gap),
            "guess": _public_text(guess),
            "evidence": _public_text(evidence),
            "action": action,
            **extra,
        })

    for group in groups:
        status = group.get("view_status") or _view_status(group)
        if status == "blocked":
            reason = group.get("why") or group.get("blocked_reason") or group.get("reason") or "This lane is blocked."
            reason = _public_text(reason)
            add(0, "blocked", f"{group.get('id')} is blocked", reason,
                group_id=group.get("id"), finding_ids=group.get("finding_ids") or [],
                view_status=status,
                fact=reason, gap="This lane cannot move until the block is cleared.",
                evidence=reason, action={"label": "Open lane", "target": group.get("id")})
        elif status in {"parked", "deferred"}:
            reason = group.get("why") or "This lane is parked."
            add(0, status, f"{group.get('id')} is {status}", reason,
                group_id=group.get("id"), finding_ids=group.get("finding_ids") or [],
                view_status=status,
                fact=_public_text(reason), gap="This lane stays parked until it is unparked.",
                evidence=_public_text(reason), action={"label": "Open lane", "target": group.get("id")})
        elif status not in {"verified", "review"}:
            history = group.get("failure_history") or []
            real = [item for item in history if isinstance(item, dict) and not item.get("tool_fault")]
            if real:
                add(1, "failed_attempts", f"{group.get('id')} has failed attempts",
                    _public_text(real[-1].get("reason") or "This lane recorded a failed attempt."),
                    group_id=group.get("id"), finding_ids=group.get("finding_ids") or [],
                    view_status=status,
                    fact=_public_text(real[-1].get("reason") or "This lane recorded a failed attempt."),
                    gap="The last failed attempt still needs a passing rerun.",
                    evidence=_public_text(real[-1].get("reason") or real[-1].get("attempt_id")),
                    action={"label": "Open lane", "target": group.get("id")})
        elif isinstance(group.get("presuite"), dict) and group["presuite"].get("status") == "failed":
            add(1, "failed_attempts", f"{group.get('id')} presuite failed",
                "Presuite exited unsuccessfully.",
                group_id=group.get("id"), finding_ids=group.get("finding_ids") or [],
                fact="Presuite exited unsuccessfully.",
                gap="This lane needs a passing pre-push suite.",
                action={"label": "Open lane", "target": group.get("id")})

    if selected_round is not None:
        round_jobs = [job for job in jobs if job.get("round") == selected_round]
        missing = [job for job in round_jobs if job.get("tokens_source") not in {"stamp", "inline"}
                   or not (isinstance(job.get("tokens"), dict) and isinstance((job.get("tokens") or {}).get("total"), int))]
        if missing and round_jobs:
            add(2, "token_coverage",
                f"{len(missing)} of {len(round_jobs)} jobs in this round have no token coverage",
                "Token totals stay unknown until a stamp or inline source is present.")

    incomplete = [job for job in jobs if job.get("event") == "incomplete" and (selected_round is None or job.get("round") == selected_round)]
    if incomplete:
        add(2, "incomplete_jobs", f"{len(incomplete)} job(s) never recorded a finish",
            "The time bar cannot close these runs.")

    for group in groups:
        status = group.get("view_status") or _view_status(group)
        # Group status is canonical: do not relabel a parked or planned lane as blocked.
        if status in {"blocked", "parked", "deferred"}:
            continue
        for fid in group.get("finding_ids") or []:
            item = findings_by_id.get(fid) or {}
            if item.get("status") == "blocked":
                add(0, "blocked", item.get("title") or fid,
                    _public_text(item.get("observed") or item.get("summary") or "This finding is blocked."),
                    group_id=group.get("id"), finding_ids=[fid], view_status="blocked")

    if isinstance(parked, dict) and parked.get("needs"):
        needs = parked.get("needs")
        if isinstance(needs, list) and needs:
            add(3, "parked", f"{len(needs)} parked need(s) still open",
                "; ".join(str(x) for x in needs[:3]))

    cards.sort(key=lambda card: (card["priority"], card.get("title") or ""))
    seen = set()
    unique = []
    for card in cards:
        key = (card.get("kind"), card.get("group_id"), card.get("title"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(card)
    more = max(0, len(unique) - 3)
    ranked = []
    for index, card in enumerate(unique[:3], start=1):
        ranked.append({**{k: v for k, v in card.items() if k != "priority"}, "rank": index})
    return ranked, more


def build_round(project, pr=None, round=None, state_dir=None):
    """Return plan groups, annotated jobs and the gate for one project/PR/round. Read only."""
    path, data, found_pr = find_tracker(project, pr, state_dir)
    tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
    plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else (data.get("plan") if isinstance(data.get("plan"), dict) else {})
    selected_round = round if isinstance(round, int) else tracking.get("round") or plan.get("round")
    if isinstance(selected_round, str) and selected_round.isdigit():
        selected_round = int(selected_round)
    groups_raw = plan.get("groups") if isinstance(plan.get("groups"), list) else []
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    findings_by_id = {str(item.get("id")): item for item in findings if isinstance(item, dict) and item.get("id")}
    current_round = tracking.get("round") or plan.get("round")
    if isinstance(current_round, str) and str(current_round).isdigit():
        current_round = int(current_round)
    current_cycle = tracking.get("cycle") or plan.get("cycle")
    all_groups = [_group_payload(group, findings_by_id) for group in groups_raw if isinstance(group, dict)]
    all_jobs = [_annotate_job(job, all_groups) for job in _fold_jobs(state_dir)]
    worktrees = {group.get("worktree") for group in all_groups if group.get("worktree")}
    group_ids = {group.get("id") for group in all_groups if group.get("id")}
    jobs = [
        job for job in all_jobs
        if job.get("group_id") in group_ids or (job.get("repo") and job.get("repo") in worktrees)
           or (selected_round is not None and job.get("round") == selected_round)
    ]
    if selected_round is not None:
        visible_jobs = [job for job in jobs
                        if job.get("round") == selected_round
                        or (job.get("round") is None and _round_from_group_id(job.get("group_id")) == selected_round)]
    else:
        visible_jobs = jobs
    groups = _round_groups(all_groups, visible_jobs, selected_round, current_round)

    history = tracking.get("round_history") if isinstance(tracking.get("round_history"), list) else []
    compact_history = _history_for_cycle(history, current_round, current_cycle)
    previous = None
    if selected_round is not None:
        earlier = [item for item in compact_history if isinstance(item.get("round"), int) and item["round"] == selected_round - 1]
        if earlier:
            previous = earlier[-1].get("ended_at")
    viewing_current = selected_round is None or selected_round == current_round

    verification = tracking.get("verification_run") if isinstance(tracking.get("verification_run"), dict) else {}
    checks = verification.get("checks") if isinstance(verification.get("checks"), dict) else {}
    fixed = sum(1 for item in checks.values() if isinstance(item, dict) and item.get("state") == "fixed")
    pending = sum(1 for item in checks.values() if isinstance(item, dict) and item.get("state") == "pending")
    # Do not invent a threshold; surface one only when the tracker stored it.
    threshold = None
    for source in (verification, tracking.get("gate") if isinstance(tracking.get("gate"), dict) else {}, tracking):
        if isinstance(source, dict) and isinstance(source.get("threshold"), int):
            threshold = source["threshold"]
            break
        if isinstance(source, dict) and isinstance(source.get("merge_previous_pr_threshold"), int):
            threshold = source["merge_previous_pr_threshold"]
            break

    parked = tracking.get("parked") if isinstance(tracking.get("parked"), dict) else {}
    parked_out = {
        "count": parked.get("count"),
        "needs": parked.get("needs") if isinstance(parked.get("needs"), list) else [],
    }

    needs, more = _rank_needs(groups, findings_by_id, parked_out, visible_jobs, selected_round)

    round_jobs = [job for job in visible_jobs if selected_round is None or job.get("round") == selected_round]
    eligible = [
        job for job in round_jobs
        if job.get("tokens_source") in {"stamp", "inline"}
        and isinstance(job.get("tokens"), dict)
        and isinstance(job["tokens"].get("total"), int)
    ]
    token_total = sum(job["tokens"]["total"] for job in eligible)
    duration_total = sum(job["duration_ms"] for job in round_jobs if isinstance(job.get("duration_ms"), int))
    by_lane = []
    for group in groups:
        lane_jobs = [job for job in round_jobs if job.get("group_id") == group.get("id")]
        if not lane_jobs:
            continue
        covered = [
            job for job in lane_jobs
            if job.get("tokens_source") in {"stamp", "inline"}
            and isinstance(job.get("tokens"), dict)
            and isinstance(job["tokens"].get("total"), int)
        ]
        by_lane.append({
            "group_id": group.get("id"),
            "jobs": len(lane_jobs),
            "covered": len(covered),
            "tokens": sum(job["tokens"]["total"] for job in covered),
            "duration_ms": sum(job["duration_ms"] for job in lane_jobs if isinstance(job.get("duration_ms"), int)),
        })

    pull = tracking.get("pull_request") if isinstance(tracking.get("pull_request"), dict) else {"label": f"PR #{found_pr}" if found_pr else None}
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project,
        "tracker": path.name,
        "pr": {"label": pull.get("label"), "url": pull.get("url"), "number": found_pr},
        "cycle": current_cycle,
        "round": selected_round,
        "current_round": current_round,
        "viewing_current": viewing_current,
        "updated_at": tracking.get("last_written_at"),
        "round_started_at": previous,
        "round_history": compact_history,
        "gate": {
            "run_id": verification.get("id") if viewing_current else None,
            "status": verification.get("status") if viewing_current else None,
            "cycle": verification.get("cycle") if viewing_current else current_cycle,
            "revision": verification.get("revision") if viewing_current else None,
            "fixed": fixed if viewing_current else next((item.get("fixed") for item in compact_history if item.get("round") == selected_round), 0),
            "pending": pending if viewing_current else 0,
            "total": len(checks) if viewing_current else 0,
            "threshold": threshold if viewing_current else None,
        },
        "status_counts": (
            _status_counts(findings) if viewing_current
            else {**_status_counts([
                finding for group in groups for finding in (group.get("findings") or [])
                if isinstance(finding, dict)
            ]), **({"verified_fixed": next(
                (item.get("fixed") for item in compact_history
                 if item.get("round") == selected_round and isinstance(item.get("fixed"), int)),
                0,
            )} )}
        ),
        "parked": parked_out,
        "needs_you": needs,
        "needs_you_more": more,
        "groups": groups,
        "jobs": visible_jobs,
        "time_tokens": {
            "jobs": len(round_jobs),
            "covered": len(eligible),
            "coverage": f"{len(eligible)}/{len(round_jobs)}" if round_jobs else "0/0",
            "tokens": token_total,
            "incomplete": sum(job.get("event") == "incomplete" for job in round_jobs),
            "duration_ms": duration_total,
            "by_lane": by_lane,
        },
    }
