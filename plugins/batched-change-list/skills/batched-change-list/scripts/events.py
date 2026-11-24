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
    """Persist job fields, filling explicit finding membership from a JSON tracker when available."""
    if kind not in {"start", "finish", "failed_start", "incomplete"}:
        raise ValueError(f"unsupported job event: {kind}")
    if "finding_ids" not in fields:
        fields["finding_ids"] = _job_finding_ids(list_path, fields.get("group_id"))
    elif not isinstance(fields["finding_ids"], list):
        raise ValueError("finding_ids must be a list")
    else:
        fields["finding_ids"] = list(dict.fromkeys(str(value) for value in fields["finding_ids"] if str(value)))
    append(list_path, {"event": kind, "job_id": job_id, "at": datetime.now(timezone.utc).isoformat(), **fields}, state_dir)


def _job_finding_ids(list_path, group_id):
    if not group_id:
        return []
    try:
        document = json.loads(Path(list_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tracking = document.get("tracking") if isinstance(document, dict) else None
    plan = tracking.get("plan") if isinstance(tracking, dict) and isinstance(tracking.get("plan"), dict) else {}
    groups = plan.get("groups") if isinstance(plan.get("groups"), list) else []
    return list(next((group.get("finding_ids", []) for group in groups
                      if isinstance(group, dict) and group.get("id") == group_id
                      and isinstance(group.get("finding_ids"), list)), []))


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
