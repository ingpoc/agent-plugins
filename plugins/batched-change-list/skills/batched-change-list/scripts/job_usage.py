#!/usr/bin/env python3
"""Attach official developer-observability token totals to a finished change-list job."""
from __future__ import annotations

from datetime import datetime, timezone

TOKEN_PARTS = ("uncached_input", "cache_read", "cache_write", "output")


def _instant(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def official_tokens(tokens):
    """Return official parts plus a total that includes cache_read, or None."""
    if not isinstance(tokens, dict):
        return None
    parts = {key: tokens.get(key) for key in TOKEN_PARTS}
    if all(type(parts[key]) is int for key in TOKEN_PARTS):
        return {**parts, "total": sum(parts[key] for key in TOKEN_PARTS)}
    if type(tokens.get("total")) is int:
        payload = {"total": tokens["total"]}
        for key in TOKEN_PARTS:
            if type(parts[key]) is int:
                payload[key] = parts[key]
        return payload
    return None


def _ids(job):
    return {
        str(job.get("id") or "").strip(),
        str(job.get("job_id") or "").strip(),
        str(job.get("session") or "").strip(),
        str(job.get("session_id") or "").strip(),
    } - {""}


def _row_ids(row):
    return {
        str(row.get("job_id") or "").strip(),
        str(row.get("id") or "").strip(),
        str(row.get("session_id") or "").strip(),
        str(row.get("session") or "").strip(),
        str(row.get("run_id") or "").strip(),
    } - {""}


def _in_window(job, row):
    start = _instant(job.get("started_at"))
    end = _instant(job.get("finished_at")) or _instant(job.get("ended_at"))
    at = _instant(row.get("started_at")) or _instant(row.get("at"))
    if start is None or at is None:
        return False
    if at < start:
        return False
    if end is not None and at > end:
        return False
    return True


def matching_harness_rows(job, harness_rows):
    """Rows that share a job or session id, or a single row in the job's time window."""
    rows = [row for row in harness_rows or [] if isinstance(row, dict)]
    wanted = _ids(job)
    exact = [row for row in rows if wanted & _row_ids(row)]
    if exact:
        return exact
    windowed = [row for row in rows if _in_window(job, row)]
    return windowed


def attach_official_usage(job, harness_rows=None, *, session_usage=None, session_source=None):
    """Attach official totals from observability rows, or mark them not_reported.

    A Codex session sidecar is used only when no unique harness match exists, and
    only when that sidecar already yields an official total. Ambiguous matches are
    not invented as a total.
    """
    matches = matching_harness_rows(job or {}, harness_rows)
    official = None
    if len(matches) == 1:
        official = official_tokens(matches[0].get("tokens"))
        cost = matches[0].get("cost")
        cost_source = matches[0].get("cost_source") or "not_reported"
        if official is not None:
            if not (isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost_source == "official"):
                cost, cost_source = None, "not_reported"
            return {"tokens": official, "tokens_source": "official", "cost": cost, "cost_source": cost_source}
    if session_usage is not None:
        official = official_tokens(session_usage)
        if official is not None and session_source in {"stamp", "inline", "official"}:
            return {"tokens": official, "tokens_source": "official", "cost": None, "cost_source": "not_reported"}
    return {"tokens": None, "tokens_source": "not_reported", "cost": None, "cost_source": "not_reported"}
