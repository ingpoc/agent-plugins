"""Read-only per-round view from the change-list tracker and job events."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_ROUND_RE = re.compile(r"^R(\d+)-", re.I)
_WORKTREE_ROUND_RE = re.compile(r"-r(\d+)-", re.I)
_WORKTREE_GROUP_RE = re.compile(r"-r(\d+)-([a-z0-9]+)", re.I)
_SHARED_WORKTREE_SUFFIXES = {"ship", "wt", "tmp", "job", "work"}
_CACHE_PATH_RE = re.compile(r"(?:~|/Users/[^/]+|/home/[^/]+)?/\.cache/\S+|~/\.cache/\S+")
_MODEL_HINT_RE = re.compile(r"\b(grok|astra|terra|sol|luna|codex|gpt|claude|gemini)\b", re.I)
_MODEL_BAD_RE = re.compile(r"(?i)(?:\.sh\b|check\.py|--|/)")
_PHASES = ("fix", "review", "install", "verify", "introspect")
_PHASE_ALIASES = {
    "planned": None,
    "plan_review": "fix",
    "fix": "fix",
    "fixing": "fix",
    "review": "review",
    "install": "install",
    "installing": "install",
    "presuite": "install",
    "verify": "verify",
    "verifying": "verify",
    "introspect": "introspect",
}


_ABS_PATH_RE = re.compile(
    r"(?:file://)?(/?(?:Users|home)/[^\s\]\)'\"<>]+)",
    re.I,
)
_MD_PATH_RE = re.compile(
    r"\[([^\]]+)\]\((?:file://)?(/?(?:Users|home)/[^)]+)\)",
    re.I,
)


def _public_text(value):
    """Drop absolute home paths and ~/.cache paths from UI text; keep the basename."""
    if not isinstance(value, str) or not value:
        return value
    def md(match):
        return match.group(1) or Path(match.group(2)).name
    text = _MD_PATH_RE.sub(md, value)
    def bare(match):
        return Path(match.group(1).rstrip("/")).name or "file"
    text = _ABS_PATH_RE.sub(bare, text)
    def cache(match):
        return "file"
    return _CACHE_PATH_RE.sub(cache, text)


def _clip_words(value, limit=10):
    if not value:
        return None
    words = str(value).split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit])


def _ist_clock(value):
    stamp = _parse_time(value)
    if stamp is None:
        return None
    local = stamp.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return local.strftime("%I:%M %p").lstrip("0")


def _group_id_from_repo(repo):
    """Infer R7-ACU20 from a worktree name like bcl120-r7-acu20. Skip shared trees."""
    if not repo:
        return None
    name = Path(str(repo).rstrip("/")).name
    match = _WORKTREE_GROUP_RE.search(name)
    if not match:
        return None
    suffix = match.group(2).lower()
    if suffix in _SHARED_WORKTREE_SUFFIXES:
        return None
    return f"R{int(match.group(1))}-{match.group(2).upper()}"


def _finding_for_group_id(group_id, findings_by_id):
    if not group_id or not findings_by_id:
        return None
    suffix = str(group_id).split("-", 1)[-1]
    dashed = re.sub(r"([A-Za-z]+)(\d+)", r"\1-\2", suffix)
    for key in (suffix, dashed, f"DO-{dashed}", f"BCL-{dashed}", f"ACU-{dashed}"):
        item = findings_by_id.get(key) or findings_by_id.get(str(key).upper()) or findings_by_id.get(str(key).lower())
        if item:
            return item
    suffix_l = dashed.lower()
    for key, item in findings_by_id.items():
        if str(key).lower() == suffix_l or str(key).lower().endswith("-" + suffix_l) or str(key).lower().endswith(suffix_l):
            return item
    return None


def _view_status(group):
    """Single status every surface uses. The lane status wins over finding status."""
    status = str((group or {}).get("status") or "").strip().lower()
    if (group or {}).get("id") == "DEFER" and status not in {"parked", "deferred"}:
        return "deferred"
    return status or "unknown"


def _lane_why(group, jobs=None):
    """A factual why of at most 10 words. The full brief stays on the group."""
    status = _view_status(group)
    if status in {"parked", "deferred"}:
        target = group.get("parked_to")
        if target is not None:
            return f"Parked to round {target}"
        return _clip_words(_public_text(group.get("reason") or "This lane is parked."))
    if status == "blocked":
        return _clip_words(
            _public_text(group.get("blocked_reason") or group.get("reason") or "") or "reason not recorded"
        )
    jobs = [job for job in (jobs or []) if isinstance(job, dict)]
    jobs = sorted(jobs, key=lambda job: job.get("finished_at") or job.get("started_at") or "")
    fails = [job for job in jobs if isinstance(job.get("exit_code"), int) and job.get("exit_code") != 0]
    last = jobs[-1] if jobs else None
    if last and last.get("exit_code") == 0:
        phase = str(last.get("phase") or "job").replace("_", " ")
        phase = phase[:1].upper() + phase[1:]
        if fails:
            when = _ist_clock(fails[-1].get("finished_at") or fails[-1].get("started_at"))
            if when:
                return _clip_words(f"{phase} passed on retry; {len(fails)} fail at {when}")
            return _clip_words(f"{phase} passed on retry")
        if status == "verified":
            return _clip_words(f"{phase} passed; verified")
        return _clip_words(f"{phase} passed")
    if last and isinstance(last.get("exit_code"), int) and last.get("exit_code") != 0:
        phase = str(last.get("phase") or "job").replace("_", " ")
        phase = phase[:1].upper() + phase[1:]
        error = last.get("first_error_line") or last.get("must_fix")
        if isinstance(error, str) and error.strip():
            return _clip_words(error.strip())
        if last.get("output_path") or last.get("output_retained"):
            return _clip_words(f"{phase} failed · output retained")
        return _clip_words(f"{phase} failed · exit {last.get('exit_code')}")
    if status == "verified":
        return "Verified"
    if status == "historical":
        return "Recorded in a finished round"
    reason = group.get("reason") or ""
    if re.search(r"(?i)\bparked\b", reason):
        return _clip_words(_public_text(group.get("blocked_reason") or "") or None)
    return _clip_words(_public_text(reason) or None)



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


_GENERIC_SLUG_TAILS = frozenset({"app", "web", "api", "frontend", "backend", "service"})
_DEFAULT_CONFIG = Path.home() / ".config/developer-observability/config.json"


def _tracker_id(value):
    """Stable tracker id. Keeps non-numeric ids such as bcl-r1; does not pull digits out of them."""
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        for key in ("list_id", "id", "number", "label"):
            found = _tracker_id(value.get(key))
            if found:
                return found
        return _pr_number(value)
    text = str(value).strip().lstrip("#")
    if not text:
        return None
    if text.isdigit():
        return text
    # URLs and "PR 19" still resolve to the number; lettered ids stay intact.
    if re.search(r"[A-Za-z]", text) and "://" not in text and "/pull/" not in text:
        return text
    return _pr_number(text)


def _is_tracker_path(path):
    if path.name in {"archive.json", "parked.json"} or path.name.startswith("."):
        return False
    if path.parent.name == "viewers" or path.name.endswith(".viewer.json"):
        return False
    return True


def _is_tracker_document(data):
    return isinstance(data, dict) and (
        isinstance(data.get("tracking"), dict) or isinstance(data.get("findings"), list)
    )


def _slug_variants(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return set()
    tokens = [token for token in re.split(r"[^a-z0-9]+", raw) if token]
    variants = {raw, "".join(tokens)}
    trimmed = list(tokens)
    while trimmed and trimmed[-1] in _GENERIC_SLUG_TAILS:
        trimmed.pop()
        if trimmed:
            variants.add("-".join(trimmed))
            variants.add("".join(trimmed))
    return variants


def _tracker_aliases(path, data):
    tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
    aliases = {path.parent.name, path.stem}
    if "--" in path.parent.name:
        aliases.add(path.parent.name.split("--", 1)[1])
    repo = tracking.get("repo")
    worktree = tracking.get("worktree")
    if not repo and isinstance(worktree, dict):
        repo = worktree.get("repo") or worktree.get("name")
    if isinstance(repo, str) and repo.strip():
        aliases.add(repo.strip())
        aliases.add(Path(repo.rstrip("/")).name)
    if isinstance(worktree, str) and worktree.strip():
        aliases.add(Path(worktree.rstrip("/")).name)
    if tracking.get("project_id"):
        aliases.add(str(tracking["project_id"]))
    if tracking.get("list_id"):
        aliases.add(str(tracking["list_id"]))
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        aliases.add(title.strip())
        first = re.split(r"[^A-Za-z0-9]+", title.strip(), maxsplit=1)[0]
        if first:
            aliases.add(first)
    return {alias for alias in aliases if alias}


def _alias_variants(path, data):
    variants = set()
    for alias in _tracker_aliases(path, data):
        variants |= _slug_variants(alias)
    return variants


def _configured_projects():
    """Map project id -> readable label from the optional local config. Missing or unreadable is empty."""
    raw = os.environ.get("OBSERVABILITY_CONFIG") or str(_DEFAULT_CONFIG)
    path = Path(raw).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, dict):
        return {}
    labels = {}
    for key, value in projects.items():
        if not isinstance(key, str) or not key.strip():
            continue
        label = key
        if isinstance(value, dict) and isinstance(value.get("label"), str) and value["label"].strip():
            label = value["label"].strip()
        labels[key] = label
    return labels


def _project_matches(project, path, data, configured=None):
    if not project or not isinstance(project, str):
        return False
    wanted = _slug_variants(project)
    configured = _configured_projects() if configured is None else configured
    if project in configured:
        wanted |= _slug_variants(configured[project])
    # A request for a repo slug should also match a configured alias, and vice versa.
    have = _alias_variants(path, data)
    for key, label in configured.items():
        config_variants = _slug_variants(key) | _slug_variants(label)
        if config_variants & have:
            have |= config_variants
            have |= _slug_variants(key)
    if wanted & have:
        return True
    folder = path.parent.name
    return (
        folder == project
        or folder.endswith(f"--{project}")
        or project == path.stem
    )


def _canonical_project(path, data, configured=None):
    """Stable project id for a tracker: a configured id when one matches, otherwise the repo/folder slug."""
    configured = _configured_projects() if configured is None else configured
    tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
    explicit = tracking.get("project_id")
    if isinstance(explicit, str) and explicit.strip():
        explicit = explicit.strip()
        if explicit in configured or not configured:
            return explicit
    have = _alias_variants(path, data)
    scored = []
    for key, label in configured.items():
        variants = _slug_variants(key) | _slug_variants(label)
        if not (variants & have):
            continue
        compact = re.sub(r"[^a-z0-9]+", "", key.lower())
        score = len(compact)
        if compact in have or re.sub(r"[^a-z0-9]+", "", (label or "").lower()) in have:
            score += 100
        scored.append((score, key))
    if scored:
        scored.sort()
        return scored[-1][1]
    if isinstance(explicit, str) and explicit:
        return explicit
    folder = path.parent.name
    if "--" in folder:
        return folder.split("--", 1)[1]
    repo = tracking.get("repo")
    if isinstance(repo, str) and repo.strip():
        return Path(repo.rstrip("/")).name
    return folder


def _project_label(project_id, configured=None):
    configured = _configured_projects() if configured is None else configured
    if project_id in configured:
        return configured[project_id]
    if project_id == "agent-plugins":
        return "Agent Plugins"
    return project_id


def _tracker_id_for(path, data):
    tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
    return (
        _tracker_id(tracking.get("list_id"))
        or _pr_number(tracking.get("pull_request"))
        or _tracker_id(path.stem)
        or path.stem
    )


def _read_tracker(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _is_tracker_document(data):
        return None
    return data


def _iter_all_trackers(state_dir=None):
    """Yield (path, data, id) for every change-list tracker. Read only."""
    root = _state_root(state_dir)
    if not root.exists():
        return
    for path in sorted(root.glob("*/*.json")):
        if not _is_tracker_path(path):
            continue
        data = _read_tracker(path)
        if data is None:
            continue
        yield path, data, _tracker_id_for(path, data)


def _list_projects(state_dir=None, current=None):
    """Every project that has a tracker, with a readable label. agent-plugins stays first."""
    configured = _configured_projects()
    rows = []
    seen = set()
    for path, data, _ident in _iter_all_trackers(state_dir):
        project_id = _canonical_project(path, data, configured)
        if project_id in seen:
            continue
        seen.add(project_id)
        rows.append({"id": project_id, "label": _project_label(project_id, configured)})
    def sort_key(row):
        if row["id"] == "agent-plugins":
            return (0, row["label"].lower())
        if current and row["id"] == current:
            return (1, row["label"].lower())
        return (2, row["label"].lower())
    rows.sort(key=sort_key)
    return rows


def _plan_groups(data):
    tracking = data.get("tracking") if isinstance(data, dict) and isinstance(data.get("tracking"), dict) else {}
    plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else (data.get("plan") if isinstance(data, dict) and isinstance(data.get("plan"), dict) else {})
    groups = plan.get("groups") if isinstance(plan, dict) and isinstance(plan.get("groups"), list) else []
    return [group for group in groups if isinstance(group, dict)]


def _tracker_sort_key(item):
    path, data, found = item
    tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
    written = _parse_time(tracking.get("last_written_at")) or datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        pr_num = int(found or 0)
    except ValueError:
        pr_num = 0
    return (pr_num, written, path.stat().st_mtime)


def _latest_tracker_key(item):
    """Rank lists when one is selected (default view, round owner, focused PR): a live list (unmerged
    and written by the live tracker, so it has last_written_at) beats a merged or legacy one, then the
    most recently written list wins, then the PR number. A live non-numeric list (bcl-r1) is not hidden
    by an older or merged numbered list (19); a legacy unmerged list with no last_written_at (112) does
    not beat newer lists. History and picker order keep _tracker_sort_key."""
    pr_num, written, mtime = _tracker_sort_key(item)
    tracking = item[1].get("tracking") if isinstance(item[1].get("tracking"), dict) else {}
    live = not isinstance(tracking.get("merged"), dict) and _parse_time(tracking.get("last_written_at")) is not None
    return (live, written, pr_num, mtime)


def _iter_trackers(project, state_dir=None):
    """Yield (path, data, pr) for every tracker that belongs to this project. Read only."""
    if not project or not isinstance(project, str):
        raise ValueError("unknown_project: project is required")
    root = _state_root(state_dir)
    if not root.exists():
        raise ValueError("tracker_unavailable: no change-list state directory")
    configured = _configured_projects()
    for path, data, ident in _iter_all_trackers(state_dir):
        if _project_matches(project, path, data, configured):
            yield path, data, ident


def list_trackers(project, state_dir=None):
    """Summaries for every list of this project, newest PR first. Read only."""
    rows = []
    for path, data, found_pr in _iter_trackers(project, state_dir):
        tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
        plan = tracking.get("plan") if isinstance(tracking.get("plan"), dict) else {}
        groups = _plan_groups(data)
        pull = tracking.get("pull_request") if isinstance(tracking.get("pull_request"), dict) else {}
        rows.append({
            "pr": found_pr,
            "label": pull.get("label") or (f"PR #{found_pr}" if str(found_pr or "").isdigit() else (found_pr or path.stem)),
            "url": pull.get("url"),
            "cycle": tracking.get("cycle") or plan.get("cycle"),
            "round": tracking.get("round") or plan.get("round"),
            "group_count": len(groups),
            "has_lanes": bool(groups),
        })
    def sort_key(row):
        ident = str(row["pr"] or "")
        if ident.isdigit():
            return (0, -int(ident), "")
        return (1, 0, ident.lower())
    rows.sort(key=sort_key)
    return rows


def find_tracker(project, pr=None, state_dir=None, prefer_lanes=None):
    """Locate the live tracker JSON for a project (and optional PR). Read only.

    When no PR is named, prefer the newest list that has lanes, so a fresh
    rollover list with no groups does not hide the previous list's work.
    """
    wanted = _tracker_id(pr) or _pr_number(pr)
    matches = list(_iter_trackers(project, state_dir))
    if wanted:
        exact = [item for item in matches
                 if str(item[2]) == wanted or item[0].stem == wanted or _tracker_id(item[0].stem) == wanted]
        if exact:
            matches = exact
    if not matches:
        raise ValueError("tracker_unavailable: no tracker for this project")
    if wanted is None and prefer_lanes is not False:
        with_lanes = [item for item in matches if _plan_groups(item[1])]
        if with_lanes:
            matches = with_lanes
    return max(matches, key=_latest_tracker_key)


# Issue trail events share events.jsonl but are not job lifecycle rows (see issue_trail.py).
_TRAIL_EVENT_NAMES = frozenset({"issue_failed_first", "root_cause_identified", "fix_verified"})


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
            if not job_id or item.get("event") in _TRAIL_EVENT_NAMES:
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
    explicit_group = bool(group_id)
    shared_group_ids = []
    round_number = job.get("round")
    if isinstance(round_number, str) and round_number.isdigit():
        round_number = int(round_number)
    if not isinstance(round_number, int):
        round_number = None
    repo = str(job.get("repo") or "")
    if not group_id:
        inferred = _group_id_from_repo(repo)
        if inferred:
            group_id = inferred
        else:
            hits = []
            for group in groups:
                worktree = group.get("worktree")
                if worktree and repo and (repo == worktree or repo.endswith(f"/{worktree}") or worktree.endswith(f"/{repo}")):
                    hits.append(group)
            if len(hits) == 1:
                group_id = hits[0].get("id")
            elif len(hits) > 1:
                shared_group_ids = [group.get("id") for group in hits if group.get("id")]
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
        "group_id_inferred": bool(group_id and not explicit_group),
        "shared_group_ids": shared_group_ids,
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
    """Chips for this list's own cycle only, plus the live round if it has not ended.

    Untagged history is often copied onto a new list at rollover. If that
    trailing cycle's last completed round is after this list's live round,
    it belongs to the previous list and is dropped.
    """
    tagged = [item for item in history if isinstance(item, dict) and item.get("cycle") == current_cycle]
    if any(isinstance(item, dict) and item.get("cycle") is not None for item in history):
        history = tagged
    cycles = _infer_history_cycles(history)
    selected = cycles[-1] if cycles else []
    max_round = max((item["round"] for item in selected), default=None)
    if isinstance(current_round, int) and max_round is not None and max_round > current_round:
        selected = []
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


def _historical_group(group_id, job, findings_by_id=None):
    finding = _finding_for_group_id(group_id, findings_by_id or {})
    finding_ids = [finding["id"]] if finding and finding.get("id") else []
    status = "verified" if finding and finding.get("status") in {"verified", "verified_fixed"} else "historical"
    title = (finding or {}).get("title") or re.sub(r"([A-Za-z]+)(\d+)", r"\1-\2", str(group_id).split("-", 1)[-1])
    return {
        "id": group_id,
        "name": title,
        "label": (finding or {}).get("id") or group_id,
        "status": status,
        "view_status": status,
        "phase": job.get("phase") if isinstance(job, dict) else None,
        "finding_ids": finding_ids,
        "findings": ([{"id": finding.get("id"), "title": finding.get("title"), "status": finding.get("status")}] if finding else []),
        "reason": None,
        "why": None,
        "worktree": (job or {}).get("repo") if isinstance(job, dict) else None,
        "reconstructed": True,
    }


def _round_groups(groups, jobs, selected_round, current_round, findings_by_id=None):
    """Lanes for the selected round. Past rounds are rebuilt from job history, not another list."""
    if selected_round is None or selected_round == current_round:
        return list(groups)
    matching = [group for group in groups if _round_from_group_id(group.get("id")) == selected_round]
    seen = {group.get("id") for group in matching}
    for job in jobs:
        job_round = job.get("round")
        if job_round is None:
            job_round = _round_from_group_id(job.get("group_id")) or _round_from_group_id(_group_id_from_repo(job.get("repo")))
        if job_round != selected_round:
            continue
        group_id = job.get("group_id") or _group_id_from_repo(job.get("repo"))
        if not group_id or group_id in seen:
            continue
        matching.append(_historical_group(group_id, job, findings_by_id))
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
        add(2, "incomplete_jobs",
            f"{len(incomplete)} job never recorded a finish" if len(incomplete) == 1
            else f"{len(incomplete)} jobs never recorded a finish",
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
            add(3, "parked", f"{len(needs)} parked need still open" if len(needs) == 1 else f"{len(needs)} parked needs still open",
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
    ranked = []
    for index, card in enumerate(unique[:3], start=1):
        ranked.append({**{k: v for k, v in card.items() if k != "priority"}, "rank": index})
    fyi = []
    for index, card in enumerate(unique[3:], start=4):
        fyi.append({**{k: v for k, v in card.items() if k != "priority"}, "rank": index})
    return ranked, fyi



def _looks_like_model(value):
    if not isinstance(value, str) or not value.strip():
        return False
    if _MODEL_BAD_RE.search(value) and not _MODEL_HINT_RE.search(value):
        return False
    if re.search(r"(?i)check\.py|\.sh\b", value):
        return False
    return bool(_MODEL_HINT_RE.search(value) or not _MODEL_BAD_RE.search(value))


def _format_model(value, version=None):
    name = _public_text(str(value).strip())
    if not name:
        return None
    if version and not re.search(r"\d", name):
        return f"{name} {version}"
    if re.search(r"\d", name):
        return name
    return f"{name} (version not recorded)"


def _lane_model(group, jobs=None):
    """A model name with version. Never a script or command."""
    for job in jobs or []:
        if _looks_like_model(job.get("model")):
            return _format_model(job.get("model"), job.get("model_version"))
    stage = group.get("stage_model") if isinstance(group.get("stage_model"), dict) else {}
    if _looks_like_model(stage.get("model")):
        return _format_model(stage.get("model"), stage.get("version") or stage.get("model_version"))
    codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
    job = codex.get("job") if isinstance(codex.get("job"), dict) else {}
    if _looks_like_model(job.get("model")):
        return _format_model(job.get("model"), job.get("model_version") or job.get("version"))
    reviewer = codex.get("reviewer_model")
    if isinstance(reviewer, str) and _MODEL_HINT_RE.search(reviewer):
        if re.search(r"(?i)grok bot", reviewer):
            return "Grok Bot (version not recorded)"
        match = re.search(r"(Astra|Terra|Sol|Luna|Codex|GPT|Claude)[^,;)]*", reviewer)
        if match:
            return _format_model(match.group(0).strip())
    return None


def _lane_model_note(group):
    codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
    reviewer = codex.get("reviewer_model")
    if isinstance(reviewer, str) and reviewer.strip():
        return _public_text(reviewer)
    return None


def _phase_track(group):
    status = _view_status(group)
    if status == "verified":
        return 4, "verify"
    raw = str(group.get("phase") or "").strip().lower()
    name = _PHASE_ALIASES.get(raw, raw if raw in _PHASES else None)
    if status in {"parked", "deferred"} and name is None:
        return None, None
    if name not in _PHASES:
        return None, name
    return _PHASES.index(name) + 1, name


def _enrich_group(group, jobs):
    lane_jobs = [job for job in jobs if job.get("group_id") == group.get("id")]
    shared = [job for job in jobs if group.get("id") in (job.get("shared_group_ids") or [])]
    related = lane_jobs + [job for job in shared if job not in lane_jobs]
    related = sorted(related, key=lambda job: job.get("finished_at") or job.get("started_at") or "")
    group["reason"] = _public_text(group.get("reason"))
    group["brief"] = group.get("reason")
    # Row why comes from this lane's own jobs; a shared run is a separate note.
    group["why"] = _lane_why(group, lane_jobs)
    group["shared_why"] = _lane_why({"status": "historical", "view_status": "historical"}, shared) if shared else None
    group["duration_ms"] = sum(
        job["duration_ms"] for job in related if isinstance(job.get("duration_ms"), int) and job["duration_ms"] > 0
    )
    group["model"] = _lane_model(group, related)
    group["model_note"] = _lane_model_note(group)
    # Reconstructed lanes take their phase from the last recorded job, unless already verified.
    if group.get("reconstructed") and related and _view_status(group) != "verified":
        last_phase = next((job.get("phase") for job in reversed(related) if job.get("phase")), None)
        if last_phase:
            group["phase"] = last_phase
    index, label = _phase_track(group)
    group["phase_index"] = index
    group["phase_label"] = label
    fails = [
        job for job in related
        if isinstance(job.get("exit_code"), int) and job.get("exit_code") != 0
    ]
    group["flagged"] = bool(fails) and _view_status(group) == "verified"
    group["flagged_exits"] = [
        {
            "exit_code": job.get("exit_code"),
            "phase": job.get("phase"),
            "at": job.get("finished_at") or job.get("started_at"),
        }
        for job in fails
    ]
    return group


def build_round(project, pr=None, round=None, state_dir=None):
    """Return plan groups, annotated jobs and the gate for one project/PR/round. Read only."""
    requested_pr = pr
    path, data, found_pr = find_tracker(project, pr, state_dir, prefer_lanes=(pr in (None, "")))
    lists = list_trackers(project, state_dir)
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
    groups = _round_groups(all_groups, visible_jobs, selected_round, current_round, findings_by_id)
    for group in groups:
        _enrich_group(group, visible_jobs)

    viewing_past = (
        isinstance(selected_round, int)
        and isinstance(current_round, int)
        and selected_round != current_round
    )
    newest = lists[0] if lists else None
    suggest = next((row for row in lists if row.get("has_lanes") and row.get("pr") != found_pr), None)
    fallback = None
    if viewing_past and not groups:
        fallback = {
            "auto": False,
            "from_pr": found_pr,
            "from_label": next((row.get("label") for row in lists if row.get("pr") == found_pr), f"PR #{found_pr}"),
            "missing_round": selected_round,
        }
    elif not groups and suggest:
        fallback = {
            "auto": False,
            "from_pr": found_pr,
            "from_label": next((row.get("label") for row in lists if row.get("pr") == found_pr), f"PR #{found_pr}"),
            "suggest_pr": suggest.get("pr"),
            "suggest_label": suggest.get("label"),
        }
    elif newest and newest.get("pr") != found_pr and not newest.get("has_lanes"):
        fallback = {
            "auto": requested_pr in (None, ""),
            "from_pr": newest.get("pr"),
            "from_label": newest.get("label"),
            "viewing_pr": found_pr,
            "viewing_label": next((row.get("label") for row in lists if row.get("pr") == found_pr), f"PR #{found_pr}"),
            "persist": True,
        }

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
    # Surface the stored merge bar, never the lower report threshold.
    threshold = None
    for source in (verification, tracking.get("gate") if isinstance(tracking.get("gate"), dict) else {}, tracking):
        if not isinstance(source, dict):
            continue
        if source.get("cycle") not in (None, current_cycle):
            continue
        for key in ("merge_previous_pr_threshold", "merge_threshold"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                threshold = value
                break
        if threshold is not None:
            break

    parked = tracking.get("parked") if isinstance(tracking.get("parked"), dict) else {}
    parked_out = {
        "count": parked.get("count"),
        "needs": parked.get("needs") if isinstance(parked.get("needs"), list) else [],
    }

    needs, fyi = _rank_needs(groups, findings_by_id, parked_out, visible_jobs, selected_round)
    more = len(fyi)

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
    shared_jobs = [job for job in round_jobs if (job.get("shared_group_ids") or []) and not job.get("group_id")]
    if shared_jobs:
        covered = [
            job for job in shared_jobs
            if job.get("tokens_source") in {"stamp", "inline"}
            and isinstance(job.get("tokens"), dict)
            and isinstance(job["tokens"].get("total"), int)
        ]
        by_lane.append({
            "group_id": "shared",
            "jobs": len(shared_jobs),
            "covered": len(covered),
            "tokens": sum(job["tokens"]["total"] for job in covered),
            "duration_ms": sum(job["duration_ms"] for job in shared_jobs if isinstance(job.get("duration_ms"), int)),
        })
    # Leave by_lane empty when jobs cannot be attributed; the UI shows unknown, not a fake "jobs" segment.
    unrecorded_spans = []
    for group in groups:
        if group.get("view_status") in {"parked", "deferred"} or group.get("id") == "DEFER":
            continue
        codex = group.get("codex") if isinstance(group.get("codex"), dict) else {}
        session = codex.get("implementer_session")
        if not (isinstance(session, str) and session.strip()):
            continue
        lane_jobs = [job for job in round_jobs if job.get("group_id") == group.get("id")]
        recorded = any(
            (job.get("session") == session or job.get("implementer_session") == session)
            and job.get("event") == "finish"
            for job in lane_jobs
        )
        if recorded:
            continue
        unrecorded_spans.append({
            "id": session,
            "name": f"implementer session {session}",
            "group_id": group.get("id"),
            "unknown": True,
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
        "lists": lists,
        "fallback": fallback,
        "needs_you": needs,
        "needs_you_more": more,
        "needs_you_fyi": fyi,
        "lane_counts": _status_counts([
            {"status": group.get("view_status") or group.get("status")}
            for group in groups
        ]),
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
            "unrecorded_spans": unrecorded_spans,
        },
    }
