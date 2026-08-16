# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Application identity, lifecycle, cache, and window resolution."""
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

def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def _safe_cache_path(app_name):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", app_name)
    return os.path.join(CACHE_DIR, f"{safe}.json")

def _read_cache(app_name, max_age=30, *, expected_pid=None):
    path = _safe_cache_path(app_name)
    if not os.path.exists(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > max_age:
            return None
        with open(path) as f:
            cached = json.load(f)
        if not _pid_alive(cached.get("pid")):
            return None
        if expected_pid and cached.get("pid") != expected_pid:
            return None
        # Verify cached window still belongs to this app (Quartz).
        for w in list_windows().get("windows", []):
            if w.get("pid") == cached.get("pid") and w.get("window_id") == cached.get(
                "window_id"
            ):
                if expected_pid:
                    return cached
                owner = w.get("app_name") or w.get("owner") or ""
                if _matches(app_name, owner):
                    return cached
                return None
        return None
    except (json.JSONDecodeError, KeyError, OSError):
        return None

def _write_cache(app_name, pid, window_id):
    try:
        with open(_safe_cache_path(app_name), "w") as f:
            json.dump({"pid": pid, "window_id": window_id, "ts": time.time()}, f)
    except OSError:
        pass

def _validated_window_override(pid, resolved_window_id, requested_window_id):
    """Allow a modal/window override only when Quartz proves the same owner PID."""
    if requested_window_id is None:
        return resolved_window_id
    for window in list_windows().get("windows", []):
        if window.get("pid") == pid and window.get("window_id") == requested_window_id:
            return requested_window_id
    raise ValueError(
        f"window {requested_window_id} does not belong to resolved PID {pid}"
    )


def clear_resolution_cache():
    """Remove only ephemeral app/window resolution records.

    Operator state, its process record, and other JSON owner surfaces share the
    cache directory and must survive an app-resolution reset.
    """
    removed = []
    for name in os.listdir(CACHE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            with open(path) as file:
                value = json.load(file)
            if not isinstance(value, dict) or not {"pid", "window_id", "ts"}.issubset(
                value
            ):
                continue
            os.unlink(path)
            removed.append(path)
        except (json.JSONDecodeError, OSError, TypeError):
            continue
    return removed

def _matches(app_name, candidate):
    """Case-insensitive substring match; empty candidate never matches."""
    if not candidate or not str(candidate).strip():
        return False
    a = app_name.lower()
    c = str(candidate).lower()
    if len(a) < 2 or len(c) < 2:
        return a == c
    return a in c or c in a


def _window_candidate_score(window):
    """Prefer real app windows over untitled desktop/overlay surfaces.

    Finder owns full-display, untitled desktop windows. Sorting only by area
    therefore resolves Finder to the desktop instead of its visible folder
    window, which produces a menu-only AX tree and a black screenshot.
    Titled windows are the stronger main-window signal; area remains the
    tie-breaker and still handles apps whose only window is untitled.
    """
    bounds = window.get("bounds", {}) or {}
    area = bounds.get("width", 0) * bounds.get("height", 0)
    has_title = bool(str(window.get("title") or "").strip())
    return (
        not bool(window.get("ax_minimized")),
        bool(window.get("ax_main")),
        has_title,
        area,
    )


def _ax_window_candidates(pid, quartz_windows=None, *, include_element=False):
    """Join AX windows to Quartz IDs; Stage Manager thumbnails fall back to Quartz."""
    try:
        import ApplicationServices as services
        import ctypes
        import objc
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
        )

        library = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        get_window = library._AXUIElementGetWindow
        get_window.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        get_window.restype = ctypes.c_int32
        application = AXUIElementCreateApplication(pid)
        try:
            services.AXUIElementSetMessagingTimeout(application, 1.5)
        except (AttributeError, TypeError):
            pass
        error, ax_windows = AXUIElementCopyAttributeValue(
            application, "AXWindows", None
        )
        if error or not ax_windows:
            return []
        by_id = {
            window.get("window_id"): window
            for window in (quartz_windows or [])
            if window.get("pid") == pid
        }

        def attribute(element, name, default=None):
            attr_error, value = AXUIElementCopyAttributeValue(element, name, None)
            return default if attr_error else value

        candidates = []
        for position, element in enumerate(ax_windows):
            window_id = ctypes.c_uint32()
            if (
                get_window(
                    ctypes.c_void_p(objc.pyobjc_id(element)), ctypes.byref(window_id)
                )
                != 0
            ):
                continue
            if include_element:
                candidates.append({"window_id": int(window_id.value), "_ax_element": element})
                continue
            quartz = dict(by_id.get(window_id.value) or {})
            try:
                ax_frame = _frame_rect(_ax_frame(element, services))
            except (AttributeError, TypeError, ValueError):
                ax_frame = None
            quartz.update(
                {
                    "pid": pid,
                    "window_id": int(window_id.value),
                    "ax_frame": ax_frame,
                    "title": str(attribute(element, "AXTitle", "") or ""),
                    "ax_main": bool(attribute(element, "AXMain", False)),
                    "ax_focused": bool(attribute(element, "AXFocused", False)),
                    "ax_minimized": bool(attribute(element, "AXMinimized", False)),
                    "ax_position": position,
                    "source": "ax+quartz",
                }
            )
            quartz.setdefault("bounds", {})
            candidates.append(quartz)
        return candidates
    except (AttributeError, ImportError, OSError):
        return []

def _raise_resolved_ax_window(pid, window_id):
    try:
        import ApplicationServices as services
    except ImportError as error:
        return {"error": f"ApplicationServices unavailable: {error}"}
    candidates = _ax_window_candidates(pid, include_element=True)
    target = next(
        (item for item in candidates if int(item.get("window_id") or 0) == int(window_id)),
        None,
    )
    if target is None:
        return {"error": "resolved AX window is unavailable"}
    result = services.AXUIElementPerformAction(target["_ax_element"], "AXRaise")
    if result != 0:
        return {"error": f"AXRaise failed with {result}"}
    return {"ok": True, "path": "native_ax_raise"}


def _exact_ax_window_frame(pid, window_id):
    """Return logical AX geometry bound to one exact CGWindow ID."""
    matches = []
    for candidate in _ax_window_candidates(pid):
        if int(candidate.get("window_id") or 0) != int(window_id):
            continue
        frame = _frame_rect(candidate.get("ax_frame"))
        if not frame:
            continue
        if (
            float(frame.get("width") or 0) <= 0
            or float(frame.get("height") or 0) <= 0
        ):
            continue
        matches.append(
            {
                "source": "ax-window-id",
                "window_id": int(window_id),
                **frame,
            }
        )
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"multiple AX windows map to exact CGWindow ID {window_id}"
    return None, f"no AX window maps to exact CGWindow ID {window_id}"


def _running_app_identity(app_name):
    """Resolve a running app by bundle/name before inspecting window titles.

    Window titles can contain unrelated app names (for example a Notification
    Center widget titled "Codex").  NSWorkspace is the authoritative PID and
    bundle owner and is fast enough for every resolution call.
    """
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return None
    needle = app_name.strip().lower()
    pid_match = re.fullmatch(r"pid:(\d+)", needle)
    requested_pid = int(pid_match.group(1)) if pid_match else None
    ranked = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        pid = int(app.processIdentifier())
        name = str(app.localizedName() or "")
        bundle_id = str(app.bundleIdentifier() or "")
        if requested_pid is not None:
            if pid != requested_pid:
                continue
            return {
                "pid": pid,
                "name": name,
                "bundle_id": bundle_id,
                "active": bool(app.isActive()),
            }
        name_lower = name.lower()
        bundle_lower = bundle_id.lower()
        bundle_leaf = bundle_lower.rsplit(".", 1)[-1]
        if needle == bundle_lower:
            score = 5
        elif needle == name_lower:
            score = 4
        elif needle == bundle_leaf:
            score = 3
        elif needle and needle in name_lower:
            score = 2
        elif needle and needle in bundle_lower:
            score = 1
        else:
            continue
        ranked.append(
            (
                score,
                {
                    "pid": pid,
                    "name": name,
                    "bundle_id": bundle_id,
                    "active": bool(app.isActive()),
                },
            )
        )
    # Equal-name multi-instance apps are deterministic: active first, then the
    # newest/highest PID. Exact callers should still prefer pid:<number>.
    return max(
        ranked,
        key=lambda item: (item[0], bool(item[1]["active"]), item[1]["pid"]),
    )[1] if ranked else None


def _activate_running_identity(identity):
    """Dispatch activation for a running app; later fresh state proves it."""
    pid = int(identity.get("pid") or 0)
    app = None
    appkit_dispatched = False
    appkit_error = None
    try:
        from AppKit import (
            NSApplicationActivateAllWindows,
            NSApplicationActivateIgnoringOtherApps,
            NSWorkspace,
        )
        app = next(
            (
                candidate
                for candidate in NSWorkspace.sharedWorkspace().runningApplications()
                if int(candidate.processIdentifier()) == pid
            ),
            None,
        )
        if app is not None:
            app.unhide()
            options = NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows
            appkit_dispatched = bool(app.activateWithOptions_(options))
            if not appkit_dispatched:
                appkit_error = f"NSWorkspace activation was rejected pid={pid}"
        else:
            appkit_error = f"PID is not registered with NSWorkspace pid={pid}"
    except (AttributeError, ImportError, TypeError, ValueError) as error:
        appkit_error = f"AppKit activation unavailable: {error}"
    frontmost_script = (
        'tell application "System Events" to set frontmost of '
        f'(first process whose unix id is {pid}) to true'
    )
    system_events_error = None
    try:
        frontmost = subprocess.run(
            ["osascript", "-e", frontmost_script],
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        )
        if frontmost.returncode != 0:
            system_events_error = (frontmost.stderr or frontmost.stdout).strip()
    except subprocess.TimeoutExpired:
        system_events_error = "System Events foreground dispatch timed out after 3s"
    if system_events_error and not appkit_dispatched:
        return {
            "error": system_events_error or appkit_error or "activation failed",
            "pid": pid,
            "appkit_error": appkit_error,
        }
    # Activation is a dispatched state change, not proof. Stage Manager can
    # report isActive=false while transitioning sets. PID-specific System
    # Events is a bounded second dispatch; the driver foreground + fresh
    # snapshot below still own the observable outcome.
    result = {
        "ok": True,
        "method": (
            "nsworkspace+system-events-dispatch"
            if appkit_dispatched
            else "system-events-pid-dispatch"
        ),
        "pid": pid,
    }
    if system_events_error:
        result["activation_warning"] = system_events_error
    if appkit_error:
        result["appkit_warning"] = appkit_error
    return result


def _reopen_running_identity(pid):
    """Ask a running app with no window to handle the macOS reopen event."""
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return {"error": "AppKit is unavailable for running-app reopen"}

    app = next(
        (
            candidate
            for candidate in NSWorkspace.sharedWorkspace().runningApplications()
            if int(candidate.processIdentifier()) == int(pid)
        ),
        None,
    )
    if app is None:
        return {"error": f"running app disappeared before reopen pid={pid}"}
    bundle_id = str(app.bundleIdentifier() or "")
    name = str(app.localizedName() or "")
    command = ["open", "-b", bundle_id] if bundle_id else ["open", "-a", name]
    if not command[-1]:
        return {"error": f"running app has no reopen identity pid={pid}"}
    try:
        reopened = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"app reopen timed out after 5s pid={pid}"}
    if reopened.returncode != 0:
        return {"error": (reopened.stderr or reopened.stdout).strip()}
    return {
        "ok": True,
        "method": "bundle-reopen" if bundle_id else "name-reopen",
        "pid": int(pid),
        "bundle_id": bundle_id or None,
        "name": name or None,
    }


def list_apps():
    return call_driver("list_apps")


def list_windows():
    """Quartz-first window list; avoids hung cua-driver list_windows."""
    wr_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "window_resolve.py"
    )
    spec = importlib.util.spec_from_file_location("window_resolve", wr_path)
    wr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wr)
    data = wr.list_windows(prefer="quartz")
    return {"windows": data["windows"], "method": data["method"]}


def _resolve_bundle_id(app_name):
    """Return (bundle_id, display_name) if we can find one via list_apps."""
    apps = list_apps()
    for entry in apps.get("apps", []):
        bundle_id = entry.get("bundle_id") or ""
        name = entry.get("name") or entry.get("app_name") or ""
        if _matches(app_name, name) or _matches(app_name, bundle_id):
            return bundle_id, name
    return None, None


def resolve_app(app_name, *, launch_if_missing=True, activate_if_inactive=False):
    """Return (pid, window_id, display_name, error)."""
    identity = _running_app_identity(app_name)
    reactivated = False
    if (
        identity
        and activate_if_inactive
        and identity.get("active") is False
    ):
        activated = launch_or_activate(app_name)
        if "error" in activated:
            return None, None, None, activated["error"]
        time.sleep(0.25)
        identity = _running_app_identity(app_name) or identity
        reactivated = True
    expected_pid = identity.get("pid") if identity else None
    # Stage Manager can keep a Quartz thumbnail for an inactive app while its
    # AX window disappears. Never trust that cached window across reactivation;
    # resolve the restored live window again.
    cached = None if reactivated else _read_cache(app_name, expected_pid=expected_pid)
    if cached:
        return (
            cached["pid"],
            cached["window_id"],
            (identity or {}).get("name") or app_name,
            None,
        )

    # 1. Try to find a live top-level window whose owner matches.
    windows = list_windows()
    if "error" in windows:
        return None, None, None, windows["error"]
    # Foreground commands already have an authoritative NSWorkspace PID and a
    # bounded driver foreground gate. Prefer its Quartz window here: querying
    # AX synchronously before that gate can hang when Stage Manager exposes a
    # thumbnail but no live accessibility window.
    if expected_pid and not activate_if_inactive:
        ax_candidates = _ax_window_candidates(expected_pid, windows.get("windows", []))
        ax_candidates.sort(key=_window_candidate_score, reverse=True)
        if ax_candidates:
            window = ax_candidates[0]
            pid, wid = window["pid"], window["window_id"]
            _write_cache(app_name, pid, wid)
            return pid, wid, (identity or {}).get("name") or app_name, None

    exact_owner = []
    fuzzy_owner = []
    for w in windows.get("windows", []):
        owner = w.get("app_name") or w.get("owner") or ""
        bounds = w.get("bounds", {}) or {}
        if bounds.get("width", 0) <= 200:
            continue
        if expected_pid:
            if w.get("pid") == expected_pid:
                exact_owner.append(w)
            continue
        if owner.lower() == app_name.lower():
            exact_owner.append(w)
        elif _matches(app_name, owner):
            fuzzy_owner.append(w)
    candidates = exact_owner or fuzzy_owner
    # Prefer a titled app window, then the largest remaining candidate.
    # Finder's untitled desktop surfaces are larger than its folder windows.
    candidates.sort(key=_window_candidate_score, reverse=True)
    if candidates:
        w = candidates[0]
        pid, wid = w["pid"], w["window_id"]
        _write_cache(app_name, pid, wid)
        return pid, wid, (identity or {}).get("name") or w.get("app_name"), None

    # 2. Nothing matched — maybe the app isn't running / isn't frontmost.
    if not launch_if_missing:
        return None, None, None, f"No on-screen window matches '{app_name}'"

    launched = _reopen_running_identity(expected_pid) if expected_pid else launch_or_activate(app_name)
    if "error" in launched:
        return None, None, None, launched["error"]
    time.sleep(1.0)
    # Retry once.
    return resolve_app(
        app_name,
        launch_if_missing=False,
        activate_if_inactive=activate_if_inactive,
    )


def launch_or_activate(app_name):
    """Launch and activate by authoritative bundle id when one is available."""
    identity = _running_app_identity(app_name)
    if identity:
        activated = _activate_running_identity(identity)
        if activated.get("error"):
            return activated
        return {
            **activated,
            "bundle_id": identity.get("bundle_id"),
            "name": identity.get("name") or app_name,
        }

    bundle_id = (identity or {}).get("bundle_id")
    display_name = (identity or {}).get("name")
    if not bundle_id:
        bundle_id, display_name = _resolve_bundle_id(app_name)
    open_command = ["open", "-b", bundle_id] if bundle_id else ["open", "-a", app_name]
    try:
        launched = subprocess.run(
            open_command,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"app launch timed out after 5s: {' '.join(open_command)}"}
    if launched.returncode != 0:
        return {"error": (launched.stderr or launched.stdout).strip()}
    target = bundle_id or app_name
    tell = (
        f"tell application id {json.dumps(target)} to activate"
        if bundle_id
        else (f"tell application {json.dumps(target)} to activate")
    )
    try:
        activated = subprocess.run(
            ["osascript", "-e", tell],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"error": "app activation AppleScript timed out after 5s"}
    if activated.returncode != 0:
        return {"error": (activated.stderr or activated.stdout).strip()}
    return {
        "ok": True,
        "method": "bundle+osascript" if bundle_id else "open+osascript",
        "bundle_id": bundle_id,
        "name": display_name or app_name,
    }
