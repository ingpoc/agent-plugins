"""Root-cause trail, timing, jobs and rollup for each issue (SPEC-trail v4).

Provenance grammar, shared by the agent API and the viewer:
  recorded  stored as data (tracker check or failure row, state change, job receipt, trail event)
  derived   read from notes or a proxy time (observed prose, lane plan time, merge time)
  missing   the step happened but this field was never stored
  pending   (status, not provenance) the step has not happened yet
A duration is recorded only when both ends are recorded, derived if either end is derived,
and missing if either end is missing. Official and estimated tokens are never summed.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

TRAIL_EVENTS = {"issue_failed_first": "first_fail", "root_cause_identified": "root_cause", "fix_verified": "fix_verified"}
INCLUDES = ("trail", "jobs", "rollup")
FAIL_KINDS = {"fail", "review_failed", "owner_rejected", "regressed"}
PASS_KINDS = {"pass", "resolved"}
RANK = {"recorded": 2, "derived": 1, "missing": 0}
OFFICIAL_SOURCES = {"stamp", "inline", "official"}
ESTIMATED_SOURCES = {"estimate", "estimated"}
TOKEN_METHODS = {"harness_usage", "char_estimate"}
_MODEL_RE = re.compile(r"(?i)\b(gpt|o\d|claude|sonnet|opus|haiku|gemini|grok-\d|llama|qwen|deepseek|mistral|kimi|glm|codex|luna)")
_CHECK_REF = re.compile(r"^check:(?P<run>.+):(?P<id>[^:]+)$")


def parse_include(value):
    if value in (None, "", []):
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    out = []
    for item in items:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in INCLUDES:
            raise ValueError("invalid_include: use trail, jobs or rollup")
        if name not in out:
            out.append(name)
    return out


def _instant(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ist(value):
    stamp = _instant(value)
    if stamp is None:
        return ""
    local = stamp.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return local.strftime("%d %b %H:%M").lstrip("0") + " IST"


def _root(state_dir=None):
    return Path(state_dir or os.environ.get("BCL_STATE_DIR", "~/.agents/state/batched-change-list")).expanduser()


def read_trail_events(state_dir=None):
    """Emitted trail events by issue id, from every events.jsonl. Read only."""
    by_issue = {}
    root = _root(state_dir)
    if not root.exists():
        return by_issue
    for path in sorted(root.glob("**/events.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not any(name in line for name in TRAIL_EVENTS):
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("event") not in TRAIL_EVENTS or not _instant(item.get("at")):
                continue
            ids = item.get("finding_ids") if isinstance(item.get("finding_ids"), list) else [item.get("finding_id")]
            for issue_id in ids:
                if isinstance(issue_id, str) and issue_id:
                    by_issue.setdefault(issue_id, []).append(item)
    return by_issue


_PATH_RE = re.compile(r"(?:[\w.~-]*/)+[\w.-]*(?::\d+(?:-\d+)?)?|\b[\w-]+\.(?:py|mjs|js|ts|tsx|sh|json|jsonl|md|html|css|yaml|yml)(?::\d+(?:-\d+)?)?\b")
_TAIL_WORDS = {"a", "an", "the", "of", "to", "for", "in", "on", "at", "by", "with", "from", "and", "or", "but", "that",
               "which", "is", "are", "was", "were", "be", "does", "do", "did", "not", "no", "can", "cannot", "its", "their",
               "what", "so", "than", "as", "it", "this", "has", "have", "until", "when", "over", "far", "still", "no longer"}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’+-]*|\d+(?:\.\d+)?")


def _plain(text, limit=5):
    """3-5 plain words from free text: no markdown, paths, links or list numbers; None if it can't."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    value = re.sub(r"`[^`]*`|https?://\S+|\*\*|__|#+ ", " ", value)
    value = _PATH_RE.sub(" ", value)
    for piece in re.split(r"[.;:!?\n—–()]|\s-\s|\s\d+[.)]\s", value):
        piece = re.sub(r"^\s*(?:\d+[.)]|[-*•>])\s*", "", piece)
        words = _WORD_RE.findall(piece)
        while words and words[0].lower() in {"the", "a", "an", "and", "but", "so"}:
            words = words[1:]
        words = words[:limit]
        while words and words[-1].lower() in _TAIL_WORDS:
            words = words[:-1]
        if len(words) >= 2:
            out = " ".join(words)
            return out[0].upper() + out[1:]
    return None


def _norm(text):
    return " ".join(_WORD_RE.findall(str(text or "").lower()))


def _step(cycle, name, ts, ts_p, run_id=None, evidence=None, source=None, what=None, note=None, run_p=None, ev_p=None):
    fields = {"ts": ts_p if ts else "missing",
              "run_id": (run_p or ts_p) if run_id else "missing",
              "evidence_url": (ev_p or ts_p) if evidence else "missing"}
    if ts_p == "missing":
        fields["ts"] = "missing"
    return {"cycle": cycle, "step": name, "status": "done", "ts": ts if ts else None, "run_id": run_id,
            "evidence_url": evidence, "provenance": fields["ts"], "field_provenance": fields,
            "source": source, "what": _plain(what), "note": note}


def _pending(cycle, name, note=None):
    return {"cycle": cycle, "step": name, "status": "pending", "ts": None, "run_id": None, "evidence_url": None,
            "provenance": None, "field_provenance": None, "source": None, "what": None, "note": note}


def _weakest(*values):
    return min(values, key=lambda value: RANK.get(value, 0))


def _evidence_for(ref, tracker, identifier):
    if not ref:
        return None
    match = _CHECK_REF.match(ref)
    if match:
        return f"tracker://{tracker}#/verification_history/{match.group('run')}/checks/{identifier}"
    if ref.startswith("job:"):
        return f"job://{ref[4:]}"
    return ref


def tokens_source(job):
    raw = str(job.get("tokens_source") or "").strip().lower()
    tokens = job.get("tokens") if isinstance(job.get("tokens"), dict) else {}
    total = tokens.get("total") if isinstance(tokens.get("total"), int) else job.get("tokens_total")
    if not isinstance(total, int):
        return "none", None
    if raw in OFFICIAL_SOURCES:
        return "official", total
    if raw in ESTIMATED_SOURCES:
        return "estimated", total
    return "none", None


def job_row(job, provenance, cycle=None):
    source, total = tokens_source(job)
    tokens = job.get("tokens") if isinstance(job.get("tokens"), dict) else {}
    model = job.get("model") if isinstance(job.get("model"), str) and _MODEL_RE.search(job.get("model") or "") \
        and not re.search(r"(?i)\.sh\b|\.py\b", job.get("model")) else None
    method = job.get("tokens_method") if job.get("tokens_method") in TOKEN_METHODS else None
    inp = tokens.get("input", job.get("tokens_in"))
    out = tokens.get("output", job.get("tokens_out"))
    return {"job_id": job.get("job_id"), "cycle": cycle, "phase": job.get("phase") or "job",
            "started_at": job.get("started_at") or job.get("at"), "finished_at": job.get("finished_at"),
            "exit_code": job.get("exit_code"), "model": model.strip() if model else None,
            "model_version": job.get("model_version") if model else None,
            "tokens": total, "tokens_in": inp if source != "none" and isinstance(inp, int) else None,
            "tokens_out": out if source != "none" and isinstance(out, int) else None,
            "tokens_source": source, "tokens_method": method if source != "none" else None,
            "harness": (str(job.get("model")).strip()[:40] or None) if not model and isinstance(job.get("model"), str) else None,
            "link_provenance": provenance}


def _job_ids(job):
    ids = set()
    for key in ("finding_ids", "ids"):
        value = job.get(key)
        if isinstance(value, list):
            ids.update(str(item) for item in value if item)
        elif isinstance(value, str) and value:
            ids.add(value)
    return ids


def linked_jobs(issue, jobs, lane_credit=None):
    """Jobs stamped with this issue id (recorded), else on the lane the issue was in when the job ran
    (derived, shared credit). lane_credit(job, issue) decides lane membership; default is the current lane."""
    issue_id = issue.get("id")
    lane = issue.get("lane") if isinstance(issue.get("lane"), dict) else {}
    rows = []
    for job in jobs:
        if not job.get("job_id") or job.get("event") in TRAIL_EVENTS:
            continue
        if issue_id in _job_ids(job):
            rows.append((job, "recorded"))
        elif lane_credit(job, issue) if lane_credit else (lane.get("id") and job.get("group_id") == lane.get("id")):
            rows.append((job, "derived"))
    rows.sort(key=lambda row: _instant(row[0].get("started_at") or row[0].get("at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def _first_fail(issue, emitted, events, tracker, end_at):
    identifier = issue.get("id")
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    candidates = []
    for row in emitted:
        if TRAIL_EVENTS.get(row.get("event")) == "first_fail":
            candidates.append(_step(1, "first_fail", row["at"], "recorded", row.get("run_id"), row.get("evidence") or row.get("evidence_ref"),
                                    "issue_failed_first event", row.get("summary") or "First failing run"))
    for event in events:
        # A failed fix or review job is an attempt, not the issue's first failure.
        if event.get("kind") in FAIL_KINDS and not str(event.get("ref") or "").startswith("job:"):
            ref = event.get("ref")
            run = _CHECK_REF.match(ref).group("run") if ref and _CHECK_REF.match(ref) else None
            source_tracker = f"{str(event['pr']).lstrip('#')}.json" if event.get("carried") and event.get("pr") else tracker
            candidates.append(_step(1, "first_fail", event["at"], "recorded", run, _evidence_for(ref, source_tracker, identifier)
                                    or f"tracker://{source_tracker}#/findings/{identifier}",
                                    ("failure_history" if not run else "verification_history.checks") + (f" (carried from #{str(event['pr']).lstrip('#')})" if event.get("carried") else ""),
                                    event.get("summary")))
    seen = issue.get("first_seen") if isinstance(issue.get("first_seen"), dict) else {}
    source = seen.get("source")
    if _instant(seen.get("at")) and source != "legacy_gap":
        derived = source in {"observed_text", "created_at"}
        path = "observed" if source == "observed_text" else "created_at" if source == "created_at" else "first_seen"
        candidates.append(_step(1, "first_fail", seen["at"], "derived" if derived else "recorded", None,
                                f"tracker://{tracker}#/findings/{identifier}/{path}",
                                "findings[].observed text" if source == "observed_text" else f"findings[].{path}",
                                raw.get("title") or issue.get("title")))
    candidates = [row for row in candidates if end_at is None or _instant(row["ts"]) <= end_at]
    if not candidates:
        return _step(1, "first_fail", None, "missing", None, None, "no dated failure or first-seen on record", None)
    # Recorded sources win; notes are the fallback (SPEC-trail §3).
    recorded = [row for row in candidates if row["provenance"] == "recorded"]
    return min(recorded or candidates, key=lambda row: _instant(row["ts"]))


def _root_cause(cycle, start, stop, emitted, raw, jobs, lane_plan, tracker, identifier):
    lo, hi = _instant(start["ts"]) if start else None, stop
    def inside(ts):
        stamp = _instant(ts)
        return stamp is not None and (lo is None or stamp >= lo) and (hi is None or stamp <= hi)
    recorded, derived = [], []
    for row in emitted:
        if TRAIL_EVENTS.get(row.get("event")) == "root_cause" and inside(row["at"]):
            recorded.append(_step(cycle, "root_cause", row["at"], "recorded", row.get("run_id"),
                                  row.get("evidence") or row.get("evidence_ref"), "root_cause_identified event", row.get("summary")))
    stored = raw.get("root_cause") if isinstance(raw.get("root_cause"), dict) else None
    if stored and inside(stored.get("at")):
        recorded.append(_step(cycle, "root_cause", stored["at"], "recorded", stored.get("run_id"), stored.get("evidence"),
                              "findings[].root_cause", stored.get("summary") or stored.get("cause")))
    for job, link in jobs:
        at = job.get("started_at") or job.get("at")
        if job.get("phase") == "plan_review" and link == "recorded" and inside(at):
            recorded.append(_step(cycle, "root_cause", at, "recorded", job.get("group_id"), f"job://{job['job_id']}",
                                  "plan_review job receipt", "Plan reviewed"))
    if lane_plan and inside(lane_plan.get("at")):
        derived.append(_step(cycle, "root_cause", lane_plan["at"], "derived", lane_plan.get("lane"),
                             f"tracker://{tracker}#/tracking/plan/groups/{lane_plan.get('lane')}", lane_plan.get("source"),
                             lane_plan.get("what"), run_p="recorded", ev_p="recorded"))
    pool = recorded or derived
    return min(pool, key=lambda row: _instant(row["ts"])) if pool else None


def build_trail(issue, emitted, jobs, tracker, lane_plan=None, merged_at=None, end_at=None, now=None,
                full_events=None, inherited=None):
    """full_events: the issue's unbounded events (past-round views bound issue.events).
    inherited: failure events carried from earlier PRs' trackers for the same id."""
    now = end_at or now or datetime.now(timezone.utc)
    identifier = issue.get("id")
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    all_emitted = list(emitted)
    own = list(issue.get("events") or [])
    seen_keys = {(event.get("kind"), event.get("at")) for event in own}
    own += [event for event in (inherited or []) if (event.get("kind"), event.get("at")) not in seen_keys]
    events = sorted((event for event in own
                     if _instant(event.get("at")) and (end_at is None or _instant(event["at"]) <= end_at)
                     and event.get("kind") not in {"first_seen", "job"}),
                    key=lambda event: _instant(event["at"]))
    emitted = [row for row in emitted if end_at is None or _instant(row["at"]) <= end_at]
    jobs = [(job, link) for job, link in jobs
            if end_at is None or (_instant(job.get("started_at") or job.get("at")) or now) <= end_at]
    if lane_plan and end_at is not None and (_instant(lane_plan.get("at")) or end_at) > end_at:
        lane_plan = None
    state = issue.get("state")
    first = _first_fail(issue, emitted, events, tracker, end_at)
    cycles = [{"start": first, "verified": None, "inconclusive": None, "fails": 1 if first["ts"] else 0}]
    after = []
    start_at = _instant(first["ts"])
    for event in events:
        stamp = _instant(event["at"])
        if start_at and stamp < start_at:
            continue
        cycle = cycles[-1]
        kind = event.get("kind")
        ref = event.get("ref")
        run = _CHECK_REF.match(ref).group("run") if ref and _CHECK_REF.match(ref) else None
        if cycle["verified"] is not None and run and kind == "pass" and cycle["verified"]["run_id"] is None:
            # A check pass right after a bare "resolved" transition is the better-evidenced verify step.
            cycle["verified"] = _step(len(cycles), "fix_verified", event["at"], "recorded", run,
                                      _evidence_for(ref, tracker, identifier), "verification_history.checks", "Verify fixed")
            continue
        if cycle["verified"] is None:
            if kind in PASS_KINDS:
                cycle["verified"] = _step(len(cycles), "fix_verified", event["at"], "recorded", run,
                                          _evidence_for(ref, tracker, identifier) or f"tracker://{tracker}#/findings/{identifier}/state_changes",
                                          "verification_history.checks" if run else "state_changes", "Verify fixed" if run else "Marked resolved")
            elif kind == "inconclusive":
                cycle["inconclusive"] = event
            elif kind in FAIL_KINDS and stamp != start_at:
                cycle["fails"] += 1
        else:
            if kind in FAIL_KINDS:
                cycles.append({"start": _step(len(cycles) + 1, "regressed", event["at"], "recorded", run,
                                              _evidence_for(ref, tracker, identifier) or f"tracker://{tracker}#/findings/{identifier}",
                                              "state_changes" if kind in {"owner_rejected", "regressed"} else "failure_history",
                                              event.get("summary")),
                               "verified": None, "inconclusive": None, "fails": 1})
            elif kind == "inconclusive":
                after.append({"ts": event["at"], "kind": "inconclusive", "run_id": run,
                              "text": "Verify inconclusive after the pass"})
    for row in emitted:
        if TRAIL_EVENTS.get(row.get("event")) == "fix_verified":
            cycle = cycles[-1]
            if cycle["verified"] is None or cycle["verified"]["provenance"] != "recorded":
                cycle["verified"] = _step(len(cycles), "fix_verified", row["at"], "recorded", row.get("run_id"),
                                          row.get("evidence") or row.get("evidence_ref"), "fix_verified event", row.get("summary") or "Verify fixed")
    last = cycles[-1]
    if state == "regressed" and last["verified"] is not None:
        cycles.append({"start": _step(len(cycles) + 1, "regressed", None, "missing", None, None,
                                      "tracker verification note", "Rejected after fix"),
                       "verified": None, "inconclusive": None, "fails": 1})
        last = cycles[-1]
    if state == "resolved" and last["verified"] is None:
        for key in ("verified_at", "resolved_at", "fixed_at", "closed_at"):
            if _instant(raw.get(key)) and (end_at is None or _instant(raw[key]) <= end_at):
                last["verified"] = _step(len(cycles), "fix_verified", raw[key], "derived", None,
                                         f"tracker://{tracker}#/findings/{identifier}/{key}", f"findings[].{key}", "Marked verified")
                break
        if last["verified"] is None and _instant(merged_at) and (end_at is None or _instant(merged_at) <= end_at):
            last["verified"] = _step(len(cycles), "fix_verified", merged_at, "derived", None, None,
                                     f"status verified_fixed at #{tracker.split('.')[0]} merge", "Marked verified at merge")
        # Status-only fallback only when no dated verify evidence exists anywhere; otherwise the verify
        # happened after this round or run ended and the step stays pending (SPEC-trail r2).
        dated_later = end_at is not None and (
            any(_instant(raw.get(key)) for key in ("verified_at", "resolved_at", "fixed_at", "closed_at"))
            or _instant(merged_at) is not None
            or any(event.get("kind") in PASS_KINDS and _instant(event.get("at")) for event in (full_events or []) + own)
            or any(TRAIL_EVENTS.get(row.get("event")) == "fix_verified" for row in all_emitted))
        if last["verified"] is None and not dated_later:
            last["verified"] = _step(len(cycles), "fix_verified", None, "missing", None, None, "status verified_fixed", "Marked verified")
    steps, timing, starts = [], [], []
    for number, cycle in enumerate(cycles, 1):
        begin = cycle["start"]
        begin["cycle"] = number
        verified = cycle["verified"]
        stop = _instant(verified["ts"]) if verified and verified["ts"] else None
        cause = _root_cause(number, begin, stop, emitted, raw, jobs, lane_plan, tracker, identifier)
        if cause is None:
            if verified is not None:
                cause = _step(number, "root_cause", None, "missing", None, None, "no root cause stored", None)
            else:
                note = f"{cycle['fails']} failed reviews, no cause recorded" if cycle["fails"] > 1 else "No cause recorded yet"
                if end_at is not None:
                    note += " · not yet at round end"
                cause = _pending(number, "root_cause", note)
        if verified is None:
            note = None
            if cycle["inconclusive"] is not None:
                note = f"Verify inconclusive {_ist(cycle['inconclusive']['at'])}, still open"
            elif end_at is not None:
                note = "not yet at round end"
            verified = _pending(number, "fix_verified", note)
        steps.extend([begin, cause, verified])
        starts.append(_instant(begin["ts"]))
        timing.append({"cycle": number, "ttr": _duration(begin, cause, now), "ttv": _duration(begin, verified, now),
                       "fix_to_verify": _duration(cause, verified, now) if cause["status"] == "done" else None})
    title = _norm(raw.get("title") or issue.get("title"))
    for step in steps:
        # A summary that only repeats the issue title adds nothing; leave it out.
        if step.get("what") and title and title.startswith(_norm(step["what"])):
            step["what"] = None
    linked = []
    for job, link in jobs:
        at = _instant(job.get("started_at") or job.get("at"))
        number = 1
        for index, stamp in enumerate(starts, 1):
            if stamp and at and at >= stamp:
                number = index
        linked.append(job_row(job, link, number))
    return {"trail": steps, "cycle": len(cycles), "timing": timing, "after": after, "jobs": linked}


def _duration(begin, end, now):
    start_p = begin["field_provenance"]["ts"] if begin.get("field_provenance") else "missing"
    if end["status"] == "pending":
        start = _instant(begin["ts"])
        return {"ms": None, "status": "open", "so_far_ms": int((now - start).total_seconds() * 1000) if start else None,
                "provenance": start_p}
    end_p = end["field_provenance"]["ts"]
    provenance = _weakest(start_p, end_p)
    a, b = _instant(begin["ts"]), _instant(end["ts"])
    if provenance == "missing" or a is None or b is None:
        return {"ms": None, "status": "done", "so_far_ms": None, "provenance": "missing"}
    if b < a:
        return {"ms": None, "status": "done", "so_far_ms": None, "provenance": "missing", "note": "ends out of order"}
    return {"ms": int((b - a).total_seconds() * 1000), "status": "done", "so_far_ms": None, "provenance": provenance}


def token_counts(rows):
    counts = {"official": 0, "estimated": 0, "none": 0}
    sums = {"official": 0, "estimated": 0}
    for row in rows:
        counts[row["tokens_source"]] += 1
        if row["tokens_source"] in sums:
            sums[row["tokens_source"]] += row["tokens"]
    return counts, sums


def _trail_for(issue, ctx, end_at=None, now=None):
    linked = linked_jobs(issue, ctx["jobs"], ctx.get("lane_credit"))
    full = ctx["full_events_for"](issue) if ctx.get("full_events_for") else None
    inherited = ctx["inherited_for"](issue) if ctx.get("inherited_for") else None
    return build_trail(issue, ctx["emitted"].get(issue.get("id"), []), linked, ctx["tracker_for"](issue),
                       ctx["lane_plan_for"](issue, linked), ctx["merged_for"](issue), end_at=end_at, now=now,
                       full_events=full, inherited=inherited)


def decorate(issues, ctx, include, end_at=None, now=None):
    """ctx: jobs, emitted, tracker_for, lane_plan_for, merged_for and optional lane_credit,
    full_events_for, inherited_for."""
    for issue in issues:
        result = _trail_for(issue, ctx, end_at=end_at, now=now)
        if "trail" in include:
            issue.update(trail=result["trail"], cycle=result["cycle"], timing=result["timing"], after=result["after"])
        if "jobs" in include:
            issue["jobs_usage"] = result["jobs"]
            counts, sums = token_counts(result["jobs"])
            issue["tokens"] = {"jobs": len(result["jobs"]), "by_source": counts, "official": sums["official"],
                               "estimated": sums["estimated"]}
    return issues


def _median(values):
    if not values:
        return None
    return int(statistics.median(values))


def rollup(issue_sets, rounds, ctx, now=None):
    """rollup[] per (pr, round) plus one '#pr all' row per PR; medians over cycles verified in scope."""
    now = now or datetime.now(timezone.utc)
    cycles_by_pr = {}
    for pr, issues in issue_sets.items():
        rows = []
        for issue in issues:
            result = _trail_for(issue, ctx, now=now)
            for timing in result["timing"]:
                verified = next((step for step in result["trail"] if step["cycle"] == timing["cycle"]
                                 and step["step"] == "fix_verified"), None)
                if verified and verified["status"] == "done":
                    rows.append({"at": _instant(verified["ts"]), "ttr": timing["ttr"], "ttv": timing["ttv"]})
        cycles_by_pr[pr] = rows
    all_jobs = [job for job in ctx["jobs"] if job.get("job_id")]
    def summary(scope, pr, number, cycles, scoped_jobs):
        def med(key):
            if not cycles:
                return None
            values = [row[key]["ms"] for row in cycles if row[key] and row[key]["ms"] is not None]
            if not values:
                return {"ms": None, "provenance": "missing"}
            derived = any(row[key] and row[key]["provenance"] != "recorded" for row in cycles if row[key] and row[key]["ms"] is not None)
            return {"ms": _median(values), "provenance": "derived" if derived else "recorded", "inputs": len(values)}
        derived_inputs = sum(1 for row in cycles if any(row[key] and row[key]["provenance"] != "recorded" for key in ("ttr", "ttv")))
        usage = [job_row(job, "time_span") for job in scoped_jobs]
        counts, sums = token_counts(usage)
        return {"scope": scope, "pr": pr, "round": number, "closed": len(cycles), "ttr_median": med("ttr"),
                "ttv_median": med("ttv"), "derived_inputs": derived_inputs, "jobs": len(usage),
                "tokens": counts, "token_sums": sums, "job_basis": "started within the round's time span"}
    out = []
    for pr in issue_sets:
        pr_rounds = sorted((row for row in rounds if str(row.get("pr")) == str(pr) and _instant(row.get("started_at"))),
                           key=lambda row: row.get("round") or 0, reverse=True)
        pr_jobs = set()
        for row in pr_rounds:
            start = _instant(row["started_at"])
            end = _instant(row.get("ended_at")) or now
            scoped = [job for job in all_jobs if start <= (_instant(job.get("started_at") or job.get("at")) or start.replace(year=1)) <= end]
            pr_jobs.update(job["job_id"] for job in scoped)
            cycles = [cycle for cycle in cycles_by_pr[pr] if cycle["at"] and start <= cycle["at"] <= end]
            out.append(summary(f"#{pr} R{row.get('round')}", int(pr) if str(pr).isdigit() else pr, row.get("round"), cycles, scoped))
        scoped = [job for job in all_jobs if job["job_id"] in pr_jobs]
        out.append(summary(f"#{pr} all", int(pr) if str(pr).isdigit() else pr, None, cycles_by_pr[pr], scoped))
    return out
