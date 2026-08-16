#!/usr/bin/env python3
"""Bounded retention for macos-cua proof images."""

from __future__ import annotations

import json
import time
from pathlib import Path


def prune(
    cache_dir: str | Path,
    *,
    max_bytes: int = 256 * 1024 * 1024,
    max_age_seconds: int = 30 * 24 * 60 * 60,
    now: float | None = None,
) -> dict:
    root = Path(cache_dir).expanduser().resolve()
    screenshots = root / "screenshots"
    if not screenshots.is_dir():
        return {"ok": True, "removed": 0, "freed_bytes": 0, "remaining_bytes": 0}
    protected: set[Path] = set()
    try:
        state = json.loads((root / "operator-state.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    for key in ("screenshot_path", "raw_screenshot_path"):
        if state.get(key):
            protected.add(Path(state[key]).expanduser().resolve())
    current_time = time.time() if now is None else float(now)
    files = []
    for path in screenshots.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, stat.st_size, path.resolve()))
    total = sum(size for _mtime, size, _path in files)
    removed = 0
    freed = 0
    for mtime, size, path in sorted(files):
        expired = current_time - mtime > max_age_seconds
        oversized = total > max_bytes
        if not (expired or oversized) or path in protected:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
        removed += 1
    return {
        "ok": True,
        "removed": removed,
        "freed_bytes": freed,
        "remaining_bytes": total,
        "max_bytes": max_bytes,
        "max_age_seconds": max_age_seconds,
    }
