# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Browser coexistence, focus leases, and lazy service adapters.

Loaded behind the stable macos-cua compatibility facade.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

def _process_command(pid):
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _visual_focus_module():
    global _VISUAL_FOCUS_MOD
    if _VISUAL_FOCUS_MOD is None:
        path = Path(__file__).resolve().with_name("visual_focus_lock.py")
        spec = importlib.util.spec_from_file_location("macos_cua_visual_focus", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load visual focus owner: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VISUAL_FOCUS_MOD = module
    return _VISUAL_FOCUS_MOD


def _acquire_visual_focus(owner):
    global _VISUAL_FOCUS_LEASE
    if _VISUAL_FOCUS_LEASE is not None:
        return {
            "ok": True,
            "version": "visual-focus-v1",
            "wait_ms": _VISUAL_FOCUS_LEASE.wait_ms,
            "reused": True,
        }
    try:
        lease = _visual_focus_module().acquire(
            owner,
            timeout=float(os.environ.get("MACOS_CUA_VISUAL_LOCK_TIMEOUT", "30")),
        )
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "VISUAL_FOCUS_BUSY",
            "error": str(exc),
        }
    _VISUAL_FOCUS_LEASE = lease
    return {
        "ok": True,
        "version": "visual-focus-v1",
        "wait_ms": lease.wait_ms,
        "reused": False,
    }


def _release_visual_focus():
    global _VISUAL_FOCUS_LEASE
    lease = _VISUAL_FOCUS_LEASE
    _VISUAL_FOCUS_LEASE = None
    if lease is None:
        return {"ok": True, "released": False}
    try:
        lease.release()
        return {"ok": True, "released": True}
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "VISUAL_FOCUS_RELEASE_FAILED",
            "error": str(exc),
        }


def _command_user_data_dir(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for index, part in enumerate(parts):
        if part.startswith("--user-data-dir="):
            value = part.split("=", 1)[1]
            return Path(value).expanduser().resolve() if value else None
        if part == "--user-data-dir" and index + 1 < len(parts):
            return Path(parts[index + 1]).expanduser().resolve()
    return None


def _command_is_chrome_family(command):
    lowered = command.lower()
    return (
        "/google chrome.app/" in lowered
        or "/chromium.app/" in lowered
        or "chrome helper" in lowered
        or "chromium helper" in lowered
    )


def check_browser_coexistence(
    pid,
    intent=None,
    session_id=None,
    claim_token=None,
    *,
    acquire=False,
):
    """Authorize a target without serializing disjoint native-app work.

    Hermes owns the dedicated Chrome runtime. Its token-free guard is the
    source of truth for lease state; if that guard itself fails, only the
    attested user-data-dir target fails closed. Ordinary apps remain fast and
    available even when Hermes is absent.
    """
    selected_intent = str(intent or "native-app").strip() or "native-app"
    command = [
        sys.executable,
        str(HERMES_CUA_GUARD),
        "--target-pid",
        str(int(pid)),
        "--intent",
        selected_intent,
    ]
    if session_id:
        command.extend(["--session-id", str(session_id)])
    if claim_token:
        command.extend(["--claim-token", str(claim_token)])
    if acquire:
        command.append("--acquire")
    try:
        if not HERMES_CUA_GUARD.is_file():
            raise FileNotFoundError(HERMES_CUA_GUARD)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        packet = json.loads(result.stdout.strip())
        if (
            not isinstance(packet, dict)
            or packet.get("safe") is not (result.returncode == 0)
            or packet.get("version") != "coexistence-v1"
            or int(packet.get("target_pid") or 0) != int(pid)
            or packet.get("intent") != selected_intent
        ):
            raise ValueError("guard result disagrees with its exit status")
        return packet
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
        target_command = _process_command(pid)
        managed = _command_user_data_dir(target_command) == HERMES_USER_DATA_DIR
        browser = _command_is_chrome_family(target_command)
        if not managed and not browser:
            return {
                "version": "coexistence-v1",
                "safe": True,
                "managed_browser": False,
                "target_pid": int(pid),
                "intent": selected_intent,
                "reason": "disjoint-native-app",
            }
        return {
            "version": "coexistence-v1",
            "safe": False,
            "managed_browser": managed,
            "target_pid": int(pid),
            "intent": selected_intent,
            "error_code": "HERMES_RUNTIME_UNAVAILABLE" if managed else "BROWSER_PAGE_BOUNDARY",
            "error": f"cannot verify browser ownership: {exc}",
        }


def _enforce_browser_coexistence(pid, args):
    global _HERMES_RUNTIME_CLAIM
    intent = getattr(args, "browser_intent", None) or "native-app"
    supplied_claim = getattr(args, "browser_claim_token", None)
    packet = check_browser_coexistence(
        pid,
        intent,
        getattr(args, "browser_session_id", None),
        supplied_claim,
        acquire=intent == "chrome-admin",
    )
    if packet.get("safe"):
        claim_token = packet.pop("claim_token", None)
        if claim_token:
            _HERMES_RUNTIME_CLAIM = {
                "token": claim_token,
                "claim_id": packet.get("claim_id"),
                "target_pid": int(pid),
                "intent": intent,
            }
        return packet
    _emit_json({"ok": False, **packet}, require_ok=True)


def _release_browser_coexistence_claim():
    global _HERMES_RUNTIME_CLAIM
    claim = _HERMES_RUNTIME_CLAIM
    _HERMES_RUNTIME_CLAIM = None
    if not claim:
        return {"safe": True, "released": False}
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(HERMES_CUA_GUARD),
                "--release-claim",
                claim["token"],
            ],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        packet = json.loads(result.stdout.strip())
        if (
            result.returncode != 0
            or not isinstance(packet, dict)
            or packet.get("safe") is not True
        ):
            raise ValueError(packet.get("error") if isinstance(packet, dict) else "invalid release result")
        return packet
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
        return {
            "version": "coexistence-v1",
            "safe": False,
            "error_code": "CUA_CLAIM_RELEASE_FAILED",
            "error": f"managed Chrome remains locked until claim expiry: {exc}",
            "claim_id": claim.get("claim_id"),
        }


def _displays():
    """Load displays.py once per process (window placement + coord conversion)."""
    global _DISPLAYS_MOD
    if _DISPLAYS_MOD is None:
        disp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "displays.py"
        )
        spec = importlib.util.spec_from_file_location("displays", disp_path)
        _DISPLAYS_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_DISPLAYS_MOD)
    return _DISPLAYS_MOD


def _operator_ui():
    """Load the native operator UI controller once per process."""
    global _OPERATOR_UI_MOD
    if _OPERATOR_UI_MOD is None:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "operator_ui.py"
        )
        spec = importlib.util.spec_from_file_location("operator_ui", path)
        _OPERATOR_UI_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_OPERATOR_UI_MOD)
    return _OPERATOR_UI_MOD


def _plan_contract():
    """Load the deterministic plan/result registry once per process."""
    global _PLAN_CONTRACT_MOD
    if _PLAN_CONTRACT_MOD is None:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "plan_contract.py"
        )
        spec = importlib.util.spec_from_file_location("plan_contract", path)
        _PLAN_CONTRACT_MOD = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("plan_contract", _PLAN_CONTRACT_MOD)
        spec.loader.exec_module(_PLAN_CONTRACT_MOD)
    return _PLAN_CONTRACT_MOD


def _cli_parser():
    """Load the declarative CLI argument schema once per process."""
    global _CLI_PARSER_MOD
    if _CLI_PARSER_MOD is None:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cli_parser.py"
        )
        spec = importlib.util.spec_from_file_location("cli_parser", path)
        _CLI_PARSER_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CLI_PARSER_MOD)
    return _CLI_PARSER_MOD


def _native_input():
    """Load native input mechanics once per process."""
    global _NATIVE_INPUT_MOD
    if _NATIVE_INPUT_MOD is None:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "native_input.py"
        )
        spec = importlib.util.spec_from_file_location("native_input", path)
        _NATIVE_INPUT_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_NATIVE_INPUT_MOD)
    return _NATIVE_INPUT_MOD


def _native_text_pointer():
    """Load AX text coordinate mechanics once per process."""
    global _NATIVE_TEXT_POINTER_MOD
    if _NATIVE_TEXT_POINTER_MOD is None:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "native_text_pointer.py"
        )
        spec = importlib.util.spec_from_file_location("native_text_pointer", path)
        _NATIVE_TEXT_POINTER_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_NATIVE_TEXT_POINTER_MOD)
    return _NATIVE_TEXT_POINTER_MOD


def operator_update(
    app_name=None,
    pid=None,
    window_id=None,
    *,
    status="active",
    active=True,
    screenshot_path=None,
    raw_screenshot_path=None,
    snapshot_id=None,
    screenshot_width=None,
    screenshot_height=None,
    window_frame=None,
    cursor_x=None,
    cursor_y=None,
    cursor_screen_x=None,
    cursor_screen_y=None,
    cursor_duration_ms=None,
    cursor_visible=None,
    cursor_image_path=CURSOR_ICON,
    message=None,
):
    """Publish operator-visible state without making UI availability fatal."""
    if os.environ.get("MACOS_CUA_OPERATOR_UI", "1") == "0":
        return {"ok": True, "disabled": True}
    try:
        if cursor_image_path == CURSOR_ICON:
            cursor_image_path = cursor_raster_path()
        return _operator_ui().update(
            active=active,
            app=app_name,
            pid=pid,
            window_id=window_id,
            window_title=app_name,
            screenshot_path=screenshot_path,
            raw_screenshot_path=raw_screenshot_path,
            snapshot_id=snapshot_id,
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            window_frame=window_frame,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            cursor_screen_x=cursor_screen_x,
            cursor_screen_y=cursor_screen_y,
            cursor_duration_ms=cursor_duration_ms,
            cursor_visible=cursor_visible,
            cursor_image_path=cursor_image_path,
            status=status,
            message=message,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"operator UI unavailable: {error}"}
