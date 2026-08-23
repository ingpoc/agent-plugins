"""Single supervised process: localhost gateway + Realtime session.

Used by CUAService Voice ▶ (no VOICE_CUA_REMOTE_ISLAND — ISLAND bus is in-process).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.request

from voice_cua.gateway import DEFAULT_HOST, DEFAULT_PORT, serve


def _ensure_output_streams() -> None:
    """PyInstaller's macOS app bootloader has no inherited console."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = os.path.expanduser("~/.cache/macos-cua/voice.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = stream
    sys.stderr = stream


def _wait_for_health(host: str, port: int, *, timeout: float = 15.0) -> bool:
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.2)
    return False


def main(argv: list[str] | None = None) -> int:
    _ensure_output_streams()
    from voice_cua.startup_trace import begin, mark
    from voice_cua.voice_settings import apply_env

    args = list(argv or sys.argv[1:])
    if "--preflight-mic" in args:
        from voice_cua.audio_io import mic_preflight

        return mic_preflight()

    begin()
    apply_env()
    host = os.environ.get("VOICE_CUA_HOST", DEFAULT_HOST)
    port = int(os.environ.get("VOICE_CUA_PORT", str(DEFAULT_PORT)))
    # Co-located with gateway — island_publish uses in-process ISLAND bus.
    os.environ.pop("VOICE_CUA_REMOTE_ISLAND", None)

    gateway_thread = threading.Thread(
        target=serve,
        args=(host, port),
        name="voice-cua-gateway",
        daemon=True,
    )
    mark("gateway_thread_start")
    gateway_thread.start()
    print(f"voice-cua stack gateway http://{host}:{port}", flush=True)

    if not _wait_for_health(host, port, timeout=25.0):
        mark("gateway_health", status="fail", error="health timeout")
        print("voice-cua stack: gateway health timeout", file=sys.stderr, flush=True)
        return 1
    mark("gateway_health", status="ok")

    text_only = "--text" in args or "--no-audio" in args
    if text_only:
        mark("mic_preflight", status="skip", detail="text mode")
    else:
        mark("mic_preflight_begin")

        def start_preflight() -> None:
            from voice_cua.audio_io import start_mic_preflight_async

            start_mic_preflight_async()

        threading.Thread(target=start_preflight, name="mic-import", daemon=True).start()

    from voice_cua.island_facade import island_publish

    island_publish("listening", title="Connecting", detail="")
    mark("island_connecting")

    from voice_cua.realtime_session import main as realtime_main

    code = realtime_main(argv)
    mark("stack_exit", status="ok" if code == 0 else "fail", detail=f"exit={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
