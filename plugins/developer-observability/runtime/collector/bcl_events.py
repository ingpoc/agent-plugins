"""Batched Change List job usage source (kept separate from generic source discovery)."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _root(state_dir=None):
    return Path(state_dir or os.environ.get("BCL_STATE_DIR", "~/.agents/state/batched-change-list")).expanduser()


def iter_events(stats=None, state_dir=None):
    """Yield only completed jobs with verified token sources; incomplete jobs never total."""
    stats = stats if stats is not None else {}
    jobs = []
    root = _root(state_dir)
    if root.exists():
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
                job_id = item.get("job_id")
                if job_id:
                    folded[job_id] = {**folded.get(job_id, {}), **item}
            jobs.extend({**item, "event": "incomplete"} if item.get("event") == "start" else item
                        for item in folded.values())
    completed = [j for j in jobs if j.get("event") == "finish"]
    eligible = [j for j in completed if j.get("tokens_source") in {"stamp", "inline"}
                and isinstance(j.get("tokens"), dict) and isinstance(j["tokens"].get("total"), int)]
    gaps = [j for j in completed if j not in eligible]
    total = sum(j["tokens"]["total"] for j in eligible)
    incomplete = sum(j.get("event") == "incomplete" for j in jobs)
    stats["bcl_events"] = {"jobs": len(completed), "covered": len(eligible),
                            "coverage": f"{len(eligible)}/{len(completed)}", "gaps": [j.get("agent", "unknown") for j in gaps],
                            "tokens": total, "incomplete": incomplete,
                            "failed_starts": sum(j.get("event") == "failed_start" for j in jobs)}
    for j in eligible:
        yield {"project": j.get("repo", "batched-change-list"), "source": "bcl_events", "kind": "bcl_job",
               "step": j.get("agent", "agent"), "error": False, "message": "completed BCL job token usage",
               "ref": None, "ts": None, "duration_ms": None, "metric": "tokens", "value": j["tokens"]["total"],
               "job_id": j.get("job_id"), "tokens": j["tokens"], "session": j.get("session"),
               "mirror": False, "test": False, "version_attribution": None, "caused_by": None,
               "response_ref": None, "step_ref": None, "turn_ctx": None}


def summary(state_dir=None):
    stats = {}
    list(iter_events(stats, state_dir))
    return stats.get("bcl_events", {"jobs": 0, "covered": 0, "coverage": "0/0", "gaps": [], "tokens": 0,
                                     "incomplete": 0, "failed_starts": 0})
