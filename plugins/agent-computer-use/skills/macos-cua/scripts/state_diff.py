#!/usr/bin/env python3
"""Codex-style AX text diffs: only added/removed/changed lines."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


CACHE = Path.home() / ".cache/macos-cua/last-state"
LINE_RE = re.compile(r"\[(\d+)\]")


def cache_key(app: str, pid: int | None, query: str | None) -> str:
    raw = f"{app}\0{pid or 0}\0{query or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _index(line: str) -> str:
    match = LINE_RE.search(line)
    return match.group(1) if match else line.strip()


def diff_text(previous: str, current: str) -> dict:
    old = {_index(line): line for line in (previous or "").splitlines() if line.strip()}
    new = {_index(line): line for line in (current or "").splitlines() if line.strip()}
    added = [new[key] for key in new.keys() - old.keys()]
    removed = [old[key] for key in old.keys() - new.keys()]
    changed = [new[key] for key in new.keys() & old.keys() if new[key] != old[key]]
    unchanged = len(new.keys() & old.keys()) - len(changed)
    lines = [f"diff unchanged={unchanged} +{len(added)} -{len(removed)} ~{len(changed)}"]
    lines.extend(f"+ {line.lstrip()}" for line in added)
    lines.extend(f"- {line.lstrip()}" for line in removed)
    lines.extend(f"~ {line.lstrip()}" for line in changed)
    return {
        "text": "\n".join(lines),
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
        "unchanged": unchanged,
    }


def apply(app: str, pid: int | None, query: str | None, text: str, *, enabled: bool) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{cache_key(app, pid, query)}.json"
    previous = ""
    try:
        previous = json.loads(path.read_text()).get("text") or ""
    except (OSError, json.JSONDecodeError):
        previous = ""
    path.write_text(json.dumps({"text": text}, ensure_ascii=False))
    if not enabled or not previous:
        return {"text": text, "diff": False}
    payload = diff_text(previous, text)
    payload["diff"] = True
    return payload
