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
            "summary": item.get("summary"),
            "observed": item.get("observed"),
            "recommendation": item.get("recommendation"),
        })
    verify = group.get("verify") if isinstance(group.get("verify"), dict) else None
    presuite = group.get("presuite") if isinstance(group.get("presuite"), dict) else None
    return {
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
        "failure_history": group.get("failure_history") if isinstance(group.get("failure_history"), list) else [],
        "codex": group.get("codex") if isinstance(group.get("codex"), dict) else {},
        "files": group.get("files") if isinstance(group.get("files"), list) else [],
        "blocked_reason": group.get("blocked_reason"),
        "parked_to": group.get("parked_to"),
    }


def _status_counts(findings):
    counts = {}
    for item in findings:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _rank_needs(groups, findings_by_id, parked, jobs, selected_round):
    """Plugin ranks the cards. The UI must not re-sort."""
    cards = []

    def add(priority, kind, title, why, **extra):
        cards.append({"priority": priority, "kind": kind, "title": title, "why": why, **extra})

    for group in groups:
        if group.get("status") == "blocked":
            reason = group.get("blocked_reason") or group.get("reason") or "This lane is blocked."
            add(0, "blocked", f"{group.get('id')} is blocked", reason,
                group_id=group.get("id"), finding_ids=group.get("finding_ids") or [])
        elif group.get("status") not in {"deferred", "verified", "review"}:
            history = group.get("failure_history") or []
            real = [item for item in history if isinstance(item, dict) and not item.get("tool_fault")]
            if real:
                add(1, "failed_attempts", f"{group.get('id')} has failed attempts",
                    real[-1].get("reason") or "This lane recorded a failed attempt.",
                    group_id=group.get("id"), finding_ids=group.get("finding_ids") or [])
        elif isinstance(group.get("presuite"), dict) and group["presuite"].get("status") == "failed":
            add(1, "failed_attempts", f"{group.get('id')} presuite failed",
                "Presuite exited unsuccessfully.",
                group_id=group.get("id"), finding_ids=group.get("finding_ids") or [])

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
        if group.get("status") == "blocked":
            continue
        for fid in group.get("finding_ids") or []:
            item = findings_by_id.get(fid) or {}
            if item.get("status") == "blocked":
                add(0, "blocked", item.get("title") or fid,
                    item.get("observed") or item.get("summary") or "This finding is blocked.",
                    group_id=group.get("id"), finding_ids=[fid])

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
    groups = [_group_payload(group, findings_by_id) for group in groups_raw if isinstance(group, dict)]
    all_jobs = [_annotate_job(job, groups) for job in _fold_jobs(state_dir)]
    worktrees = {group.get("worktree") for group in groups if group.get("worktree")}
    group_ids = {group.get("id") for group in groups if group.get("id")}
    jobs = [
        job for job in all_jobs
        if job.get("group_id") in group_ids or (job.get("repo") and job.get("repo") in worktrees)
    ]
    if selected_round is not None:
        visible_jobs = [job for job in jobs if job.get("round") == selected_round or job.get("round") is None and job.get("group_id") in {
            group.get("id") for group in groups if _round_from_group_id(group.get("id")) == selected_round
        }]
    else:
        visible_jobs = jobs

    history = tracking.get("round_history") if isinstance(tracking.get("round_history"), list) else []
    compact_history = []
    for item in history:
        if not isinstance(item, dict):
            continue
        compact_history.append({
            "round": item.get("round"),
            "fixed": item.get("fixed"),
            "ended_at": item.get("ended_at"),
        })
    previous = None
    if selected_round is not None:
        earlier = [item for item in compact_history if isinstance(item.get("round"), int) and item["round"] == selected_round - 1]
        if earlier:
            previous = earlier[-1].get("ended_at")

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
        "cycle": tracking.get("cycle") or plan.get("cycle"),
        "round": selected_round,
        "updated_at": tracking.get("last_written_at"),
        "round_started_at": previous,
        "round_history": compact_history,
        "gate": {
            "run_id": verification.get("id"),
            "status": verification.get("status"),
            "cycle": verification.get("cycle"),
            "revision": verification.get("revision"),
            "fixed": fixed,
            "pending": pending,
            "total": len(checks),
            "threshold": threshold,
        },
        "status_counts": _status_counts(findings),
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
