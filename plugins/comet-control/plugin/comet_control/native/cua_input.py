"""Real keyboard/mouse/window capture for a leased Comet window through CUAService.

Chrome 155 policy (Comet ExtensionManagement runtime_blocked_hosts) disables
chrome.debugger for the extension on every tab. The extension then asks the
broker for real macOS input on its own leased window:

  raise   -> resolve the leased window (bounds + title, never another Comet window),
             remember the frontmost app, raise that exact window (CUAService)
  steps   -> keys / text / clicks posted by CUAService to the pinned window
  release -> restore the previous frontmost app (debounced)
  capture -> CUAService window capture of the leased window (no raise)

Never logs typed text (lengths only). Never touches a window it did not resolve
for the same session in `raise`/`capture`.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

COMET_BUNDLE_ID = "ai.perplexity.comet"
DEFAULT_CUA_SOCKET = Path("~/.cache/macos-cua/cua-service.sock").expanduser()
BOUNDS_TOLERANCE = 2.0
RESTORE_DELAY_S = 0.6
PASTE_SETTLE_S = 0.12
KEYSTROKE_MAX_CHARS = 40
LOG_MAX_BYTES = 256 * 1024

_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "minus", "+": "equal", "{": "bracketleft", "}": "bracketright",
    "|": "backslash", ":": "semicolon", '"': "apostrophe", "<": "comma", ">": "period",
    "?": "slash", "~": "grave",
}
_PLAIN = {
    " ": "space", "-": "minus", "=": "equal", "[": "bracketleft", "]": "bracketright",
    "\\": "backslash", ";": "semicolon", "'": "apostrophe", ",": "comma", ".": "period",
    "/": "slash", "`": "grave",
}
# Comet / CDP key names -> CUAService key spec names.
_NAMED_KEYS = {
    "enter": "return", "return": "return", "tab": "tab", "escape": "escape", "esc": "escape",
    "backspace": "delete", "space": "space", " ": "space", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right", "up": "up", "down": "down", "left": "left",
    "right": "right", "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
}
_MODIFIERS = {"meta": "cmd", "command": "cmd", "cmd": "cmd", "control": "ctrl", "ctrl": "ctrl",
              "alt": "alt", "option": "alt", "shift": "shift"}


class NativeInputError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


def char_key_spec(ch: str) -> str | None:
    """US-layout key spec for one printable ASCII char, else None."""
    if len(ch) != 1:
        return None
    if "a" <= ch <= "z" or "0" <= ch <= "9":
        return ch
    if "A" <= ch <= "Z":
        return f"shift+{ch.lower()}"
    if ch in _PLAIN:
        return _PLAIN[ch]
    if ch in _SHIFTED:
        return f"shift+{_SHIFTED[ch]}"
    return None


def key_spec(key: str, modifiers: list[str] | None = None) -> str:
    """'Enter' / 'a' / 'Meta+a' / ('a', ['Meta']) -> CUAService spec ('cmd+a')."""
    raw = str(key or "")
    parts = [p for p in raw.split("+")] if len(raw) > 1 and "+" in raw.strip("+") else [raw]
    mods = [str(m) for m in (modifiers or [])] + parts[:-1]
    base = parts[-1]
    if base == "Delete":
        raise NativeInputError("UNSUPPORTED_KEY", "forward Delete has no CUAService key; select then Backspace")
    name = _NAMED_KEYS.get(base.lower())
    if name is None:
        if len(base) == 1:
            single = char_key_spec(base)
            if single is None:
                raise NativeInputError("UNSUPPORTED_KEY", f"no US key for {base!r}; type it as text")
            name = single
        elif base.lower().startswith("f") and base[1:].isdigit() and 1 <= int(base[1:]) <= 12:
            name = base.lower()
        else:
            raise NativeInputError("UNSUPPORTED_KEY", f"unsupported key {base!r}")
    flags: list[str] = []
    for mod in mods:
        mapped = _MODIFIERS.get(mod.strip().lower())
        if mapped is None:
            raise NativeInputError("UNSUPPORTED_KEY", f"unsupported modifier {mod!r}")
        if mapped not in flags:
            flags.append(mapped)
    if name.startswith("shift+"):
        if "shift" not in flags:
            flags.append("shift")
        name = name[len("shift+"):]
    order = ["cmd", "ctrl", "alt", "shift"]
    return "+".join([m for m in order if m in flags] + [name])


def text_mode(text: str, requested: str, keystrokes_allowed: bool) -> str:
    """Deterministic text path: 'keystrokes' (per-key HID) or 'paste' (clipboard + Cmd+V)."""
    if requested == "paste":
        return "paste"
    ascii_ok = bool(text) and all(char_key_spec(ch) for ch in text)
    if requested == "keystrokes":
        if not ascii_ok:
            raise NativeInputError("KEYSTROKES_UNSUPPORTED_TEXT", "keystrokes mode needs printable US ASCII without newlines")
        if not keystrokes_allowed:
            raise NativeInputError("KEYSTROKES_LAYOUT", "current input source is not U.S./ABC; use paste")
        return "keystrokes"
    if ascii_ok and len(text) <= KEYSTROKE_MAX_CHARS and keystrokes_allowed:
        return "keystrokes"
    return "paste"


def match_windows(windows: list[dict[str, Any]], bounds: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        want = [float(bounds[k]) for k in ("left", "top", "width", "height")]
    except (KeyError, TypeError, ValueError) as exc:
        raise NativeInputError("WINDOW_BOUNDS_INVALID", "window bounds need left/top/width/height") from exc
    out = []
    for win in windows:
        have = [float(win[k]) for k in ("x", "y", "w", "h")]
        if all(abs(a - b) <= BOUNDS_TOLERANCE for a, b in zip(have, want)):
            out.append(win)
    return out


AX_CLICK_WALK = 800
_PRESS_ROLES = {"AXButton", "AXLink", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXPopUpButton",
                "AXDisclosureTriangle", "AXTab", "AXMenuButton", "AXImage", "AXCell", "AXRow", "AXStaticText"}


def ax_press_target(elements: list[dict[str, Any]], x: float, y: float) -> int | None:
    """Deepest pressable AX element (AXPress) whose frame contains the global point."""
    best = None
    for el in elements:
        frame = el.get("frame") or {}
        try:
            fx, fy, fw, fh = (float(frame[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        if fw <= 0 or fh <= 0 or not (fx <= x <= fx + fw and fy <= y <= fy + fh):
            continue
        if "AXPress" not in (el.get("actions") or []) or el.get("role") == "AXWindow":
            continue
        if el.get("role") not in _PRESS_ROLES and not str(el.get("role", "")).startswith("AX"):
            continue
        key = (int(el.get("depth") or 0), -(fw * fh))
        if best is None or key > best[0]:
            best = (key, int(el["element_index"]))
    return best[1] if best else None


def title_matches(ax_title: str, tab_title: str) -> bool:
    ax_title = str(ax_title or "").strip()
    tab_title = str(tab_title or "").strip()
    if not tab_title:
        return False
    return ax_title == tab_title or ax_title.startswith(tab_title + " - ")


class CuaRpc:
    """Length-prefixed JSON-RPC to CUAService. No auto-relaunch (never kills CUAService)."""

    def __init__(self, socket_path: Path = DEFAULT_CUA_SOCKET):
        self.socket_path = Path(socket_path)
        self._seq = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            req_id = self._seq
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}).encode()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect(str(self.socket_path))
            except OSError as exc:
                raise NativeInputError("CUA_UNAVAILABLE", f"CUAService socket unavailable: {exc}") from exc
            sock.sendall(struct.pack("<I", len(body)) + body)

            def recv_exact(n: int) -> bytes:
                data = b""
                while len(data) < n:
                    chunk = sock.recv(n - len(data))
                    if not chunk:
                        raise NativeInputError("CUA_UNAVAILABLE", "CUAService closed the socket")
                    data += chunk
                return data

            for _ in range(8):
                length = struct.unpack("<I", recv_exact(4))[0]
                if length > 16_000_000:
                    raise NativeInputError("CUA_PROTOCOL", f"implausible frame {length}")
                response = json.loads(recv_exact(length))
                if response.get("id") != req_id:
                    continue
                if response.get("error"):
                    err = response["error"]
                    raise NativeInputError("CUA_ERROR", f"CUAService {method}: {err.get('message')}", cua_code=err.get("code"))
                result = response.get("result")
                return result if isinstance(result, dict) else {"result": result}
            raise NativeInputError("CUA_PROTOCOL", f"no response for {method}")
        except socket.timeout as exc:
            raise NativeInputError("CUA_TIMEOUT", f"CUAService {method} timed out") from exc
        finally:
            sock.close()


class MacDesktop:
    """pyobjc adapter: windows, frontmost app, pasteboard, input source."""

    def __init__(self) -> None:
        import AppKit  # noqa: F401  (fail at construction, not mid-input)
        import Quartz  # noqa: F401

    def comet_pids(self) -> list[int]:
        import AppKit
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(COMET_BUNDLE_ID)
        return [int(a.processIdentifier()) for a in apps or []]

    def windows(self, pid: int) -> list[dict[str, Any]]:
        import Quartz
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
        ) or []
        out = []
        for win in info:
            if int(win.get("kCGWindowOwnerPID", 0)) != pid or int(win.get("kCGWindowLayer", 1)) != 0:
                continue
            b = win.get("kCGWindowBounds") or {}
            out.append({"id": int(win["kCGWindowNumber"]), "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
                        "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0)),
                        "onscreen": bool(win.get("kCGWindowIsOnscreen"))})
        return out

    def frontmost(self) -> dict[str, Any] | None:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return {"pid": int(app.processIdentifier()), "bundle_id": str(app.bundleIdentifier() or "")}

    def activate(self, pid: int) -> bool:
        import AppKit
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None or app.isTerminated():
            return False
        return bool(app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))

    def clipboard_save(self) -> list[dict[str, bytes]]:
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        items = []
        for item in pb.pasteboardItems() or []:
            entry = {}
            for kind in item.types() or []:
                data = item.dataForType_(kind)
                if data is not None:
                    entry[str(kind)] = bytes(data)
            items.append(entry)
        return items

    def clipboard_put_text(self, text: str) -> None:
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        item = AppKit.NSPasteboardItem.alloc().init()
        item.setString_forType_(text, AppKit.NSPasteboardTypeString)
        # nspasteboard.org markers: clipboard managers skip transient/concealed items.
        for marker in ("org.nspasteboard.TransientType", "org.nspasteboard.ConcealedType", "org.nspasteboard.AutoGeneratedType"):
            item.setString_forType_("", marker)
        pb.writeObjects_([item])

    def clipboard_restore(self, items: list[dict[str, bytes]]) -> None:
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        objects = []
        for entry in items:
            item = AppKit.NSPasteboardItem.alloc().init()
            for kind, data in entry.items():
                item.setData_forType_(AppKit.NSData.dataWithBytes_length_(data, len(data)), kind)
            objects.append(item)
        if objects:
            pb.writeObjects_(objects)

    def keystrokes_allowed(self) -> bool:
        """True only for a plain U.S./ABC keyboard layout with no input-method (IME) source selected."""
        import plistlib
        path = Path.home() / "Library/Preferences/com.apple.HIToolbox.plist"
        try:
            with path.open("rb") as handle:
                prefs = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return False
        sources = prefs.get("AppleSelectedInputSources") or []
        layouts = []
        for source in sources:
            kind = str(source.get("InputSourceKind") or "")
            if kind == "Keyboard Layout":
                layouts.append(str(source.get("KeyboardLayout Name") or ""))
            elif kind in {"Input Mode", "Keyboard Input Method"}:
                return False
        return bool(layouts) and all(name in {"U.S.", "ABC", "US"} for name in layouts)


class NativeInput:
    def __init__(
        self,
        *,
        browser_pid: Callable[[], int | None],
        rpc: Any | None = None,
        desktop: Any | None = None,
        screenshot_dir: Callable[[], Path] | None = None,
        log_path: Callable[[], Path] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timer: Callable[..., Any] = threading.Timer,
    ) -> None:
        self.browser_pid = browser_pid
        self.rpc = rpc or CuaRpc()
        self._desktop = desktop
        self.screenshot_dir = screenshot_dir
        self.log_path = log_path
        self.sleep = sleep
        self.timer = timer
        self.lock = threading.RLock()
        self.session_windows: dict[str, int] = {}
        self.saved_front: dict[str, Any] | None = None
        self.restore_timer: Any = None

    @property
    def desktop(self) -> Any:
        if self._desktop is None:
            self._desktop = MacDesktop()
        return self._desktop

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = str(request.get("op") or "")
        session = str(request.get("sessionId") or "")
        started = time.monotonic()
        try:
            if not session and op not in {"probe"}:
                raise NativeInputError("SESSION_REQUIRED", "native input is lease-scoped; sessionId required")
            handler = {
                "probe": self.op_probe, "raise": self.op_raise, "steps": self.op_steps,
                "release": self.op_release, "capture": self.op_capture, "log_path": self.op_log_path,
                "prepare_raise": self.op_prepare_raise,
            }.get(op)
            if handler is None:
                raise NativeInputError("UNKNOWN_OP", f"unknown native op {op!r}")
            with self.lock:
                result = handler(session, request.get("params") or {})
            response = {"success": True, **result}
        except NativeInputError as exc:
            response = {"success": False, "error_code": exc.code, "error": str(exc), **({"details": exc.details} if exc.details else {})}
        except Exception as exc:  # never crash the broker loop
            response = {"success": False, "error_code": "NATIVE_INPUT_FAILED", "error": f"{type(exc).__name__}: {exc}"}
        response["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        self._log(op, session, request.get("params") or {}, response)
        return response

    # ── ops ──────────────────────────────────────────────────────────────
    def op_probe(self, _session: str, _params: dict[str, Any]) -> dict[str, Any]:
        apps = self.rpc.call("list_apps", {}, timeout=5)
        return {"cua": True, "apps": len(apps.get("result") or []) if isinstance(apps, dict) else 0}

    def _comet_pid(self) -> int:
        pid = self.browser_pid()
        pids = self.desktop.comet_pids()
        if not pid or pids != [int(pid)]:
            raise NativeInputError("COMET_NOT_EXACT", f"attested Comet pid {pid} is not the only Comet instance {pids}")
        return int(pid)

    def _resolve_window(self, session: str, params: dict[str, Any]) -> tuple[int, str]:
        pid = self._comet_pid()
        candidates = match_windows(self.desktop.windows(pid), params.get("window") or {})
        if not candidates:
            raise NativeInputError("WINDOW_NOT_FOUND", "no Comet window matches the leased window bounds")
        tab_title = str(params.get("title") or "")
        chosen = []
        titles = []
        for cand in candidates:
            state = self.rpc.call("get_app_state", {
                "app": COMET_BUNDLE_ID, "windowId": cand["id"], "includeScreenshot": False, "maxElements": 1,
            }, timeout=8)
            title = str(state.get("windowTitle") or "")
            titles.append(len(title))
            if title_matches(title, tab_title) or (len(candidates) == 1 and not tab_title):
                chosen.append((cand["id"], title))
        if len(chosen) != 1:
            raise NativeInputError(
                "WINDOW_AMBIGUOUS" if len(chosen) > 1 else "WINDOW_TITLE_MISMATCH",
                f"{len(candidates)} Comet window(s) match the lease bounds; {len(chosen)} match the leased tab title",
            )
        self.session_windows[session] = chosen[0][0]
        return chosen[0]

    def _pinned(self, session: str, params: dict[str, Any]) -> int:
        wid = self.session_windows.get(session)
        if not wid or int(params.get("window_id") or 0) != wid:
            raise NativeInputError("WINDOW_NOT_RAISED", "raise the leased window first (window_id mismatch)")
        return wid

    def op_prepare_raise(self, session: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record the user's front app before the browser window comes forward.

        The leased Comet window must be made visible and focused before its
        physical bounds can be matched, so the prior app is captured here
        rather than after the raise.
        """
        if self.restore_timer is not None:
            self.restore_timer.cancel()
            self.restore_timer = None
        front = self.desktop.frontmost()
        if self.saved_front is None and front and front.get("bundle_id") != COMET_BUNDLE_ID:
            self.saved_front = front
        return {}

    def op_raise(self, session: str, params: dict[str, Any]) -> dict[str, Any]:
        self.op_prepare_raise(session, params)
        wid, title = self._resolve_window(session, params)
        state = self.rpc.call("get_app_state", {
            "app": COMET_BUNDLE_ID, "windowId": wid, "raiseForInput": True, "includeScreenshot": False, "maxElements": 1,
        }, timeout=10)
        if int(state.get("windowId") or 0) != wid:
            raise NativeInputError("WINDOW_RAISE_FAILED", "CUAService raised a different window")
        return {"window_id": wid, "window_title_len": len(title),
                "saved_front": (self.saved_front or {}).get("bundle_id", "")}

    def op_steps(self, session: str, params: dict[str, Any]) -> dict[str, Any]:
        wid = self._pinned(session, params)
        steps = params.get("steps") or []
        if not isinstance(steps, list) or not steps or len(steps) > 64:
            raise NativeInputError("STEPS_INVALID", "steps must be a non-empty list (<=64)")
        allowed = None
        results: list[dict[str, Any]] = []
        pending_keys: list[str] = []

        def flush() -> None:
            if not pending_keys:
                return
            plan = self.rpc.call("execute_plan", {
                "app": COMET_BUNDLE_ID, "windowId": wid,
                "steps": [{"method": "press_key", "params": {"key": k}} for k in pending_keys],
            }, timeout=10 + len(pending_keys) * 0.2)
            rows = plan.get("results") or []
            if plan.get("ok") is False or len(rows) != len(pending_keys) or not all(r.get("ok") for r in rows):
                bad = next((r for r in rows if not r.get("ok")), {})
                raise NativeInputError("KEY_FAILED", f"CUAService key plan failed: {bad.get('error') or plan.get('error') or 'incomplete'}")
            pending_keys.clear()

        for step in steps:
            kind = str(step.get("kind") or "")
            if kind == "key":
                spec = key_spec(str(step.get("key") or ""), step.get("modifiers") or [])
                pending_keys.append(spec)
                results.append({"kind": "key", "key": spec})
            elif kind == "text":
                text = str(step.get("text") or "")
                if not text:
                    continue
                if allowed is None:
                    allowed = bool(self.desktop.keystrokes_allowed())
                mode = text_mode(text, str(step.get("mode") or "auto"), allowed)
                if mode == "keystrokes":
                    pending_keys.extend(char_key_spec(ch) for ch in text)
                else:
                    flush()
                    self._paste(wid, text)
                results.append({"kind": "text", "mode": mode, "length": len(text)})
            elif kind == "click":
                flush()
                x, y = float(step["x"]), float(step["y"])
                count = int(step.get("count") or 1)
                # Chromium drops pid-posted mouse events on web content; an AX press on the
                # web element under the point is a trusted (isTrusted) activation.
                index = None
                if count == 1:
                    walk = self.rpc.call("get_app_state", {
                        "app": COMET_BUNDLE_ID, "windowId": wid, "includeScreenshot": False,
                        "includeElements": True, "maxElements": AX_CLICK_WALK,
                    }, timeout=15)
                    index = ax_press_target(walk.get("elements") or [], x, y)
                params = {"app": COMET_BUNDLE_ID, "windowId": wid}
                params.update({"element_index": index} if index is not None else {"x": x, "y": y, "click_count": count})
                out = self.rpc.call("click", params, timeout=10)
                if out.get("ok") is not True:
                    raise NativeInputError("CLICK_FAILED", f"CUAService click failed: {out.get('error') or out.get('method')}")
                results.append({"kind": "click", "x": round(x, 1), "y": round(y, 1), "method": out.get("method"),
                                "ax_element": index is not None})
            elif kind == "wait":
                flush()
                self.sleep(min(max(float(step.get("ms") or 0), 0), 2000) / 1000)
            else:
                raise NativeInputError("STEPS_INVALID", f"unknown step kind {kind!r}")
        flush()
        return {"window_id": wid, "steps": results}

    def _paste(self, wid: int, text: str) -> None:
        saved = self.desktop.clipboard_save()
        try:
            self.desktop.clipboard_put_text(text)
            out = self.rpc.call("press_key", {"app": COMET_BUNDLE_ID, "windowId": wid, "key": "cmd+v"}, timeout=10)
            if out.get("ok") is not True:
                raise NativeInputError("KEY_FAILED", f"Cmd+V failed: {out.get('error')}")
            self.sleep(PASTE_SETTLE_S)
        finally:
            self.desktop.clipboard_restore(saved)

    def op_release(self, session: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.saved_front is None:
            return {"restore": "none"}
        delay = min(max(float(params.get("delay_ms", RESTORE_DELAY_S * 1000)), 0), 5000) / 1000
        if self.restore_timer is not None:
            self.restore_timer.cancel()
        if delay == 0:
            restored = self._restore_front()
            return {"restore": "done" if restored else "failed"}
        self.restore_timer = self.timer(delay, self._restore_front)
        self.restore_timer.daemon = True
        self.restore_timer.start()
        return {"restore": "scheduled", "delay_ms": int(delay * 1000), "to": (self.saved_front or {}).get("bundle_id", "")}

    def _restore_front(self) -> bool:
        with self.lock:
            front, self.saved_front, self.restore_timer = self.saved_front, None, None
            if not front:
                return False
            current = self.desktop.frontmost() or {}
            # The human (or another app) took focus meanwhile: never yank it back.
            if current.get("bundle_id") != COMET_BUNDLE_ID:
                return False
            return self.desktop.activate(int(front["pid"]))

    def op_capture(self, session: str, params: dict[str, Any]) -> dict[str, Any]:
        wid, _title = self._resolve_window(session, params)
        state = self.rpc.call("get_app_state", {
            "app": COMET_BUNDLE_ID, "windowId": wid, "includeScreenshot": True, "maxElements": 1,
        }, timeout=15)
        url = str((state.get("screenshot") or {}).get("url") or "")
        if not url.startswith("file://"):
            raise NativeInputError("CAPTURE_FAILED", "CUAService returned no window capture (Screen Recording?)",
                                   screen_capture=state.get("screenCapture"))
        source = Path(url[len("file://"):])
        ext = source.suffix.lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        dest_dir = self.screenshot_dir() if self.screenshot_dir else source.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in session if ch.isalnum() or ch in "-_")[:64] or "lease"
        dest = dest_dir / f"cua-{safe}-{int(time.time() * 1000)}.{ext}"
        shutil.copyfile(source, dest)
        os.chmod(dest, 0o600)
        out = {"window_id": wid, "screenshot_path": str(dest), "format": ext,
               "window_width": state.get("windowWidth"), "window_height": state.get("windowHeight")}
        if params.get("inline"):
            import base64
            out["base64"] = base64.b64encode(dest.read_bytes()).decode("ascii")
        return out

    def op_log_path(self, _session: str, _params: dict[str, Any]) -> dict[str, Any]:
        return {}

    # ── flight recorder ──────────────────────────────────────────────────
    def _log(self, op: str, session: str, params: dict[str, Any], response: dict[str, Any]) -> None:
        if not self.log_path:
            return
        entry: dict[str, Any] = {
            "ts": int(time.time() * 1000), "op": op, "session": session,
            "ok": bool(response.get("success")), "ms": response.get("elapsed_ms"),
        }
        for key in ("kind", "path", "reason", "action"):
            if key in params:
                entry[key] = str(params[key])[:80]
        for key in ("restore", "saved_front", "window_id"):
            if key in response:
                entry[key] = response[key]
        if op == "steps":
            entry["steps"] = [
                {k: v for k, v in s.items() if k in {"kind", "mode", "length", "key", "method"}}
                for s in response.get("steps") or []
            ] or [str(s.get("kind")) for s in params.get("steps") or [] if isinstance(s, dict)]
        if not response.get("success"):
            entry["error_code"] = response.get("error_code")
        try:
            path = self.log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.chmod(path, 0o600)
        except OSError:
            pass
