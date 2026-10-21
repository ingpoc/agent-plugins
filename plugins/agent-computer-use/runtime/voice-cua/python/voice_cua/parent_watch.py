"""Exit the voice helper when its CUAService parent goes away.

A CUAService restart used to leave the helper running with ppid 1: old code,
mic and Realtime socket open, no island. The supervisor passes its pid in
``VOICE_CUA_PARENT_PID``; this watchdog polls it (and re-parenting to launchd)
and, once the parent is gone, runs the normal SIGTERM shutdown path, with a
hard exit as the fallback.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Callable


def parent_alive(parent_pid: int, *, original_ppid: int, getppid: Callable[[], int] = os.getppid) -> bool:
    if parent_pid <= 1:
        return getppid() == original_ppid and original_ppid > 1
    if getppid() != parent_pid:
        return False  # re-parented (launchd) → our service is gone
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_on_gone() -> None:
    try:
        from voice_cua.activity_log import log_event

        log_event("parent_gone", detail="CUAService exited; stopping voice helper")
    except Exception:  # noqa: BLE001
        pass
    print("voice-cua: parent CUAService gone — shutting down", flush=True)
    os.kill(os.getpid(), signal.SIGTERM)  # normal path: comet_end, cursor hide, mic close
    time.sleep(5.0)
    os._exit(0)  # fallback if the graceful path hangs


def start_parent_watch(
    *,
    parent_pid: int | None = None,
    interval: float = 2.0,
    on_gone: Callable[[], None] = _default_on_gone,
    getppid: Callable[[], int] = os.getppid,
) -> threading.Thread | None:
    """Start the watchdog; returns None when there is no supervising parent."""
    if parent_pid is None:
        raw = os.environ.get("VOICE_CUA_PARENT_PID", "").strip()
        parent_pid = int(raw) if raw.isdigit() else 0
    original = getppid()
    if parent_pid <= 1 and original <= 1:
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            if not parent_alive(parent_pid, original_ppid=original, getppid=getppid):
                on_gone()
                return

    t = threading.Thread(target=_loop, name="parent-watch", daemon=True)
    t.start()
    return t
