#!/usr/bin/env python3
"""Crash-safe, process-scoped lock for macOS visual focus and capture."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import threading
import time


LOCK_PATH = Path(
    os.environ.get(
        "MACOS_CUA_VISUAL_LOCK",
        os.path.expanduser("~/.cache/macos-cua/visual-focus.lock"),
    )
)
VERSION = "visual-focus-v1"
_LOCAL_LOCK = threading.Lock()


class VisualFocusBusy(TimeoutError):
    pass


class VisualFocusLease:
    def __init__(self, handle, path: Path, owner: str, wait_ms: int, local_lock) -> None:
        self.handle = handle
        self.path = path
        self.owner = owner
        self.wait_ms = wait_ms
        self.local_lock = local_lock
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self.handle.close()
            finally:
                self.local_lock.release()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.release()


def acquire(owner: str, *, timeout: float = 30.0, poll: float = 0.05) -> VisualFocusLease:
    started = time.monotonic()
    bounded_timeout = max(0.0, float(timeout))
    if bounded_timeout == 0:
        local_acquired = _LOCAL_LOCK.acquire(blocking=False)
    else:
        local_acquired = _LOCAL_LOCK.acquire(timeout=bounded_timeout)
    if not local_acquired:
        raise VisualFocusBusy(
            f"macOS visual focus is busy after {bounded_timeout:.2f}s"
        )
    handle = None
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = LOCK_PATH.open("a+", encoding="utf-8")
        os.chmod(LOCK_PATH, 0o600)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= bounded_timeout:
                    raise VisualFocusBusy(
                        f"macOS visual focus is busy after {bounded_timeout:.2f}s"
                    )
                time.sleep(max(0.01, poll))
        wait_ms = round((time.monotonic() - started) * 1000)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "version": VERSION,
            "owner": str(owner)[:160],
            "pid": os.getpid(),
            "acquired_at": time.time(),
        }, separators=(",", ":")))
        handle.flush()
        return VisualFocusLease(handle, LOCK_PATH, owner, wait_ms, _LOCAL_LOCK)
    except BaseException:
        if handle is not None:
            handle.close()
        _LOCAL_LOCK.release()
        raise
