#!/usr/bin/env python3
"""Build and control the harness-independent macos-cua operator UI."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import time
import uuid


SKILL_DIR = Path(__file__).resolve().parents[1]
SOURCES = tuple(sorted((SKILL_DIR / "operator").glob("*.swift")))
CACHE_DIR = Path(os.path.expanduser("~/.cache/macos-cua/operator"))
APP_HOME = Path(os.path.expanduser("~/Library/Application Support/macos-cua"))
APP_BUNDLE = APP_HOME / "macos-cua Operator.app"
BINARY = APP_BUNDLE / "Contents" / "MacOS" / "macos-cua-operator"
INFO_PLIST = APP_BUNDLE / "Contents" / "Info.plist"
STATE = Path(os.path.expanduser("~/.cache/macos-cua/operator-state.json"))
PID_FILE = CACHE_DIR / "operator.pid"
LOG_FILE = CACHE_DIR / "operator.log"
SERVICE_LABEL = "com.macos-cua.operator"
LAUNCH_AGENT = Path(os.path.expanduser(f"~/Library/LaunchAgents/{SERVICE_LABEL}.plist"))
SERVICE_TARGET = f"gui/{os.getuid()}/{SERVICE_LABEL}"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


@contextmanager
def _state_lock():
    """Serialize Python publishers with the signed Swift acknowledgement writer."""
    lock_path = Path(f"{STATE}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_process_record() -> dict:
    try:
        raw = PID_FILE.read_text().strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = int(raw)
        if isinstance(value, int):
            return {"pid": value}
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _read_pid() -> int | None:
    pid = _read_process_record().get("pid")
    return pid if isinstance(pid, int) else None


def _process_uses_current_binary() -> bool:
    record = _read_process_record()
    try:
        return record.get("binary_mtime_ns") == BINARY.stat().st_mtime_ns
    except OSError:
        return False


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def detect_harness() -> str:
    explicit = (os.environ.get("MACOS_CUA_HARNESS") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_AGENT"):
        return "Cursor"
    if os.environ.get("CLAUDECODE"):
        return "Claude Code"
    term = (os.environ.get("TERM_PROGRAM") or "").lower()
    if "cursor" in term:
        return "Cursor"
    if "vscode" in term:
        return "VS Code"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        return "Codex"
    return os.path.basename(os.environ.get("SHELL") or "shell")


def _run(command, *, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _service_status() -> dict:
    result = _run(["launchctl", "print", SERVICE_TARGET], timeout=10)
    pid_match = re.search(r"\bpid\s*=\s*(\d+)", result.stdout)
    pid = int(pid_match.group(1)) if pid_match else None
    return {
        "installed": LAUNCH_AGENT.is_file(),
        "loaded": result.returncode == 0,
        "running": _alive(pid),
        "pid": pid,
        "label": SERVICE_LABEL,
        "plist": str(LAUNCH_AGENT),
        "target": SERVICE_TARGET,
    }


def _signing_identity() -> str:
    explicit = (os.environ.get("MACOS_CUA_SIGNING_IDENTITY") or "").strip()
    if explicit:
        return explicit
    result = _run(["security", "find-identity", "-v", "-p", "codesigning"])
    identities = re.findall(r'"([^"]+)"', result.stdout)
    for prefix in ("Developer ID Application:", "Apple Development:"):
        match = next(
            (identity for identity in identities if identity.startswith(prefix)), None
        )
        if match:
            return match
    return "-"


def signing_status() -> dict:
    if not APP_BUNDLE.is_dir() or not BINARY.is_file():
        return {"ok": False, "signed": False, "mode": "missing"}
    verify = _run(["codesign", "--verify", "--deep", "--strict", APP_BUNDLE])
    detail = _run(["codesign", "-dvv", APP_BUNDLE])
    combined = f"{detail.stdout}\n{detail.stderr}"
    authority = re.search(r"^Authority=(.+)$", combined, re.MULTILINE)
    signature = re.search(r"^Signature=(.+)$", combined, re.MULTILINE)
    mode = "identity" if authority else "adhoc" if signature else "unknown"
    return {
        "ok": verify.returncode == 0,
        "signed": verify.returncode == 0,
        "mode": mode,
        "authority": authority.group(1) if authority else None,
        "bundle": str(APP_BUNDLE),
        "error": (verify.stderr or verify.stdout).strip() or None,
    }


def _write_bundle_metadata() -> None:
    INFO_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with INFO_PLIST.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleDevelopmentRegion": "en",
                "CFBundleDisplayName": "macos-cua Operator",
                "CFBundleExecutable": BINARY.name,
                "CFBundleIdentifier": SERVICE_LABEL,
                "CFBundleInfoDictionaryVersion": "6.0",
                "CFBundleName": "macos-cua Operator",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "13.0",
                "LSUIElement": True,
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
            },
            handle,
            sort_keys=True,
        )


def _restart_service() -> dict:
    result = _run(["launchctl", "kickstart", "-k", SERVICE_TARGET], timeout=20)
    return {
        "ok": result.returncode == 0,
        "error": (result.stderr or result.stdout).strip() or None,
    }


def build(*, force: bool = False) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    current = (
        BINARY.exists()
        and INFO_PLIST.exists()
        and BINARY.stat().st_mtime >= max(path.stat().st_mtime for path in SOURCES)
        and signing_status().get("ok")
    )
    if current and not force:
        return {
            "ok": True,
            "built": False,
            "binary": str(BINARY),
            "bundle": str(APP_BUNDLE),
            "signing": signing_status(),
        }
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_DIR / f".{BINARY.name}.{os.getpid()}.build"
    result = subprocess.run(
        [
            "swiftc",
            "-parse-as-library",
            *(str(path) for path in SOURCES),
            "-o",
            str(temporary),
            "-framework",
            "AppKit",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "error": (result.stderr or result.stdout).strip(),
            "binary": str(BINARY),
        }
    _write_bundle_metadata()
    os.replace(temporary, BINARY)
    identity = _signing_identity()
    signed = _run(
        [
            "codesign",
            "--force",
            "--deep",
            "--timestamp=none",
            "--options",
            "runtime",
            "--sign",
            identity,
            APP_BUNDLE,
        ],
        timeout=60,
    )
    if signed.returncode != 0:
        return {
            "ok": False,
            "error": (signed.stderr or signed.stdout).strip(),
            "binary": str(BINARY),
            "bundle": str(APP_BUNDLE),
        }
    signing = signing_status()
    if not signing.get("ok"):
        return {
            "ok": False,
            "error": signing.get("error") or "codesign verification failed",
            "binary": str(BINARY),
            "bundle": str(APP_BUNDLE),
            "signing": signing,
        }
    restarted = None
    if _service_status().get("loaded"):
        restarted = _restart_service()
        if not restarted.get("ok"):
            return {
                "ok": False,
                "error": restarted.get("error") or "launchd restart failed",
                "binary": str(BINARY),
                "bundle": str(APP_BUNDLE),
                "signing": signing,
            }
    return {
        "ok": True,
        "built": True,
        "binary": str(BINARY),
        "bundle": str(APP_BUNDLE),
        "identity": identity,
        "signing": signing,
        "service_restarted": bool(restarted),
    }


def _write_launch_agent() -> None:
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT.open("wb") as handle:
        plistlib.dump(
            {
                "Label": SERVICE_LABEL,
                "ProgramArguments": [str(BINARY), str(STATE)],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Interactive",
                "LimitLoadToSessionType": "Aqua",
                "StandardOutPath": str(LOG_FILE),
                "StandardErrorPath": str(LOG_FILE),
            },
            handle,
            sort_keys=True,
        )


def _bootstrap_service() -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        update(
            active=False,
            status="idle",
            message="Waiting for a macos-cua session",
            start=False,
        )
    current = _service_status()
    if current.get("loaded"):
        _run(["launchctl", "bootout", SERVICE_TARGET], timeout=20)
    bootstrap = _run(
        [
            "launchctl",
            "bootstrap",
            f"gui/{os.getuid()}",
            LAUNCH_AGENT,
        ],
        timeout=20,
    )
    if bootstrap.returncode != 0:
        return {
            "ok": False,
            "error": (bootstrap.stderr or bootstrap.stdout).strip(),
            "service": _service_status(),
        }
    kickstart = _restart_service()
    if not kickstart.get("ok"):
        return {"ok": False, "error": kickstart.get("error")}
    for _ in range(40):
        service = _service_status()
        if service.get("running"):
            return {"ok": True, "service": service}
        time.sleep(0.1)
    return {
        "ok": False,
        "error": f"launchd service did not start; inspect {LOG_FILE}",
        "service": _service_status(),
    }


def install_service() -> dict:
    compiled = build(force=True)
    if not compiled.get("ok"):
        return compiled
    manual_pid = _read_pid()
    if _alive(manual_pid):
        os.kill(manual_pid, signal.SIGTERM)
        for _ in range(20):
            if not _alive(manual_pid):
                break
            time.sleep(0.05)
    PID_FILE.unlink(missing_ok=True)
    _write_launch_agent()
    launched = _bootstrap_service()
    return {**compiled, **launched, "installed": launched.get("ok", False)}


def uninstall_service() -> dict:
    service = _service_status()
    if service.get("loaded"):
        _run(["launchctl", "bootout", SERVICE_TARGET], timeout=20)
    LAUNCH_AGENT.unlink(missing_ok=True)
    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)
    return {
        "ok": not LAUNCH_AGENT.exists() and not _service_status().get("loaded"),
        "removed_plist": str(LAUNCH_AGENT),
        "removed_bundle": str(APP_BUNDLE),
    }


def ensure() -> dict:
    compiled = build()
    if not compiled.get("ok"):
        return compiled
    service = _service_status()
    if service.get("installed"):
        was_running = service.get("running", False)
        if not service.get("running"):
            launched = _bootstrap_service()
            if not launched.get("ok"):
                return {**compiled, **launched}
            service = launched["service"]
        return {
            "ok": True,
            "running": service.get("running", False),
            "started": not was_running,
            "pid": service.get("pid"),
            "service": service,
            **compiled,
        }
    pid = _read_pid()
    if _alive(pid) and not _process_uses_current_binary():
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.05)
        pid = None
    if _alive(pid):
        return {"ok": True, "running": True, "started": False, "pid": pid, **compiled}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        update(
            active=False,
            status="idle",
            message="Waiting for a macos-cua session",
            start=False,
        )
    with LOG_FILE.open("ab") as log:
        process = subprocess.Popen(
            [str(BINARY), str(STATE)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _atomic_json(
        PID_FILE,
        {
            "binary_mtime_ns": BINARY.stat().st_mtime_ns,
            "pid": process.pid,
        },
    )
    for _ in range(20):
        if _alive(process.pid):
            return {
                "ok": True,
                "running": True,
                "started": True,
                "pid": process.pid,
                **compiled,
            }
        time.sleep(0.05)
    return {"ok": False, "error": f"operator exited; inspect {LOG_FILE}"}


def update(*, start: bool = True, **fields) -> dict:
    if fields.get("harness") is None:
        fields["harness"] = detect_harness()
    with _state_lock():
        state = _read_json(STATE)
        next_app = fields.get("app")
        if next_app and next_app != state.get("app"):
            if fields.get("screenshot_path") is None:
                state["screenshot_path"] = ""
                state["raw_screenshot_path"] = ""
            state["cursor_visible"] = False
            for key in (
                "snapshot_id", "screenshot_width", "screenshot_height", "window_frame",
                "cursor_x", "cursor_y", "cursor_screen_x", "cursor_screen_y",
                "cursor_update_id", "cursor_rendered_update_id",
            ):
                state.pop(key, None)
        if fields.get("active") and fields.get("message") is None:
            fields["message"] = ""
        if fields.get("active") is False:
            fields.update(cursor_visible=False, app="", window_title="", screenshot_path="", raw_screenshot_path="")
            for key in (
                "pid", "window_id", "snapshot_id", "screenshot_width", "screenshot_height", "window_frame", "cursor_x", "cursor_y",
                "cursor_screen_x", "cursor_screen_y",
                "cursor_update_id", "cursor_rendered_x", "cursor_rendered_y",
                "cursor_rendered_update_id",
            ):
                state.pop(key, None)
        if fields.get("cursor_x") is not None or fields.get("cursor_y") is not None:
            fields["cursor_update_id"] = (
                fields.get("cursor_update_id") or uuid.uuid4().hex
            )
            state.pop("cursor_rendered_x", None)
            state.pop("cursor_rendered_y", None)
            state.pop("cursor_rendered_update_id", None)
            if fields.get("cursor_screen_x") is None and fields.get("cursor_screen_y") is None:
                state.pop("cursor_screen_x", None)
                state.pop("cursor_screen_y", None)
        state.update({key: value for key, value in fields.items() if value is not None})
        state.setdefault("version", 1)
        state.setdefault(
            "session_id", os.environ.get("MACOS_CUA_SESSION") or str(uuid.uuid4())
        )
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(STATE, state)
    running = ensure() if start else {"ok": True, "running": _alive(_read_pid())}
    return {"ok": bool(running.get("ok")), "state": state, "operator": running}


def status() -> dict:
    service = _service_status()
    pid = service.get("pid") if service.get("running") else _read_pid()
    state = _read_json(STATE)
    pip_visible = state.get("pip_visible")
    if not isinstance(pip_visible, bool):
        preference = _run(["defaults", "read", SERVICE_LABEL, "pipVisible"], timeout=5)
        pip_visible = preference.returncode != 0 or preference.stdout.strip() not in {
            "0",
            "false",
            "NO",
        }
    return {
        "ok": True,
        "running": _alive(pid),
        "pid": pid,
        "binary": str(BINARY),
        "bundle": str(APP_BUNDLE),
        "state_path": str(STATE),
        "state": state,
        "pip_visible": pip_visible,
        "log": str(LOG_FILE),
        "service": service,
        "signing": signing_status(),
    }


def set_pip_visible(visible: bool) -> dict:
    result = update(pip_visible=bool(visible))
    return {**result, "pip_visible": bool(visible)}


def stop() -> dict:
    service = _service_status()
    pid = service.get("pid") if service.get("running") else _read_pid()
    stopped = False
    if service.get("loaded"):
        result = _run(["launchctl", "bootout", SERVICE_TARGET], timeout=20)
        stopped = result.returncode == 0
    elif _alive(pid):
        os.kill(pid, signal.SIGTERM)
        stopped = True
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.05)
    if stopped and pid:
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.05)
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    update(active=False, status="idle", message="Operator UI stopped", start=False)
    return {
        "ok": not _service_status().get("loaded") and not _alive(pid),
        "stopped": stopped,
        "pid": pid,
        "service_installed": LAUNCH_AGENT.is_file(),
    }


_OPERATOR_CLI = None


def _operator_cli():
    global _OPERATOR_CLI
    if _OPERATOR_CLI is None:
        path = Path(__file__).with_name("operator_cli.py")
        spec = importlib.util.spec_from_file_location("macos_cua_operator_cli", path)
        module = importlib.util.module_from_spec(spec)
        module.__dict__.update(
            {name: value for name, value in globals().items() if not name.startswith("__")}
        )
        spec.loader.exec_module(module)
        _OPERATOR_CLI = module
    return _OPERATOR_CLI


def main() -> int:
    return _operator_cli().main()


if __name__ == "__main__":
    raise SystemExit(main())
