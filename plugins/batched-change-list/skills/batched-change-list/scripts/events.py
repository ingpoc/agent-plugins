"""Append-only BCL job and stage events for local collection."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone


def _list_key(list_path: str | Path) -> str:
    return hashlib.sha256(str(Path(list_path).resolve()).encode("utf-8")).hexdigest()


def events_path(list_path: str | Path, state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir or os.environ.get("BCL_STATE_DIR", "~/.agents/state/batched-change-list")).expanduser()
    return root / _list_key(list_path) / "events.jsonl"


def append(list_path: str | Path, event: dict, state_dir: str | Path | None = None) -> None:
    path = events_path(list_path, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")


def job_event(list_path: str | Path, job_id: str, kind: str, *, state_dir=None, **fields) -> None:
    if kind not in {"start", "finish", "failed_start", "incomplete"}:
        raise ValueError(f"unsupported job event: {kind}")
    append(list_path, {"event": kind, "job_id": job_id, "at": datetime.now(timezone.utc).isoformat(), **fields}, state_dir)


def stage_event(list_path: str | Path, stage: str, kind: str, *, state_dir=None, **fields) -> None:
    if kind not in {"start", "finish"}:
        raise ValueError(f"unsupported stage event: {kind}")
    append(list_path, {"event": f"stage_{kind}", "stage": stage, "at": datetime.now(timezone.utc).isoformat(), **fields}, state_dir)


def fold(rows: list[dict]) -> list[dict]:
    """Fold start/finish by stable ID; retain failed/incomplete rows and never total them."""
    jobs: dict[str, dict] = {}
    visible: list[dict] = []
    for row in rows:
        job_id = row.get("job_id")
        kind = row.get("event")
        if not job_id:
            visible.append(dict(row))
        elif kind == "start":
            jobs[job_id] = {**jobs.get(job_id, {}), **row}
        elif kind == "finish":
            jobs[job_id] = {**jobs.get(job_id, {}), **row}
        elif kind in {"failed_start", "incomplete"}:
            jobs[job_id] = {**jobs.get(job_id, {}), **row}
    visible.extend({**row, "event": "incomplete"} if row.get("event") == "start" else row
                   for row in jobs.values())
    return visible


def read(list_path: str | Path, state_dir: str | Path | None = None) -> list[dict]:
    path = events_path(list_path, state_dir)
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []
    return fold([row for row in rows if isinstance(row, dict)])
