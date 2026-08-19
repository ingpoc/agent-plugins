#!/usr/bin/env python3
"""Persistent in-process owner for the compact macos-cua MCP."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


COMPACT_STATE_KEYS = (
    "ok",
    "app",
    "pid",
    "window_id",
    "snapshot_id",
    "text",
    "element_count",
    "hidden_element_count",
    "screenshot",
    "capture_error",
)


class SessionRuntime:
    """Load macos-cua once and dispatch every MCP tool in the held process."""

    def __init__(self, facade_path: Path):
        self.facade_path = Path(facade_path)
        self._facade: ModuleType | None = None
        self._state_diff: ModuleType | None = None

    def _load(self, name: str, path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load runtime module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @property
    def cua(self) -> ModuleType:
        if self._facade is None:
            self._facade = self._load("macos_cua_mcp_facade", self.facade_path)
        return self._facade

    @property
    def state_diff(self) -> ModuleType:
        if self._state_diff is None:
            self._state_diff = self._load(
                "macos_cua_mcp_state_diff", self.facade_path.with_name("state_diff.py")
            )
        return self._state_diff

    def telemetry_reset(self) -> None:
        self.cua.telemetry_reset()

    def telemetry_read(self) -> dict[str, float | int]:
        return self.cua.telemetry_read()

    def start_driver(self, session: str) -> dict[str, Any]:
        os.environ["MACOS_CUA_SESSION"] = session
        self.cua.CUA_SESSION = session
        self.cua.clear_resolution_cache()
        return self.cua.call_driver("start_session", {"session": session}, timeout=8)

    def end_driver(self, session: str) -> dict[str, Any]:
        return self.cua.call_driver("end_session", {"session": session}, timeout=8)

    def ensure_operator(self) -> dict[str, Any]:
        return self.cua._operator_ui().ensure()

    def closeout(self) -> dict[str, Any]:
        cua = self.cua
        cleared = cua.clear_resolution_cache()
        cleanup = cua._cleanup_driver_cursors(include_named=True)
        operator = cua.operator_update(
            status="idle", active=False, message="No controlled app"
        )
        daemon = cua.call_driver("check_permissions", {"prompt": False}, timeout=5)
        daemon_ready = "error" not in daemon and daemon.get("ok") is not False
        return {
            "success": daemon_ready,
            "cleared_cache": True,
            "cleared_entries": cleared,
            "ended_cursor_sessions": len(cleanup.get("ended") or []),
            "operator": operator,
            "daemon_ready": daemon_ready,
        }

    def _resolve(
        self, app: str
    ) -> tuple[int | None, int | None, str | None, dict[str, Any] | None]:
        cua = self.cua
        identity = cua._running_app_identity(app)
        authorized_pid = identity.get("pid") if identity else None
        if authorized_pid:
            packet = cua.check_browser_coexistence(authorized_pid, "native-app")
            if not packet.get("safe"):
                return None, None, None, {"ok": False, **packet}
        pid, window_id, name, error = cua.resolve_app(
            app, activate_if_inactive=False
        )
        if error:
            return None, None, None, {
                "ok": False,
                "error": f"Cannot resolve '{app}': {error}",
            }
        if authorized_pid != pid:
            packet = cua.check_browser_coexistence(pid, "native-app")
            if not packet.get("safe"):
                return None, None, None, {"ok": False, **packet}
        return pid, window_id, name or app, None

    def _with_target(
        self,
        app: str,
        operation: Callable[[ModuleType, int, int, str], dict[str, Any]],
    ) -> dict[str, Any]:
        cua = self.cua
        visual = cua._acquire_visual_focus(f"macos-cua:mcp:{app[:80]}")
        if not visual.get("ok"):
            return {"ok": False, **visual}
        payload: dict[str, Any]
        try:
            pid, window_id, name, error = self._resolve(app)
            payload = error or operation(cua, int(pid), int(window_id), str(name))
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
        finally:
            claim_release = cua._release_browser_coexistence_claim()
            visual_release = cua._release_visual_focus()
        release_failures = [
            packet
            for packet in (claim_release, visual_release)
            if packet.get("safe", packet.get("ok")) is not True
        ]
        if release_failures:
            return {
                "ok": False,
                "error": "runtime lease release failed",
                "result": payload,
                "release_failures": release_failures,
            }
        return payload

    def state(
        self,
        app: str,
        *,
        query: str | None,
        diff: bool,
        max_elements: int,
    ) -> dict[str, Any]:
        def observe(cua: ModuleType, pid: int, window_id: int, name: str):
            result = cua.app_state(
                name,
                pid,
                window_id,
                max_elements=max_elements,
                query=query,
                include_screenshot=False,
                prepare_foreground=False,
            )
            if result.get("ok"):
                result = {
                    key: result[key] for key in COMPACT_STATE_KEYS if key in result
                }
                result.update(
                    self.state_diff.apply(
                        result.get("app") or app,
                        result.get("pid"),
                        query,
                        result.get("text") or "",
                        enabled=diff,
                    )
                )
            return result

        return self._with_target(app, observe)

    def act(
        self,
        app: str,
        arguments: dict[str, Any],
        ax_actions: set[str] | frozenset[str],
    ) -> dict[str, Any]:
        plan = arguments.get("plan")
        text = arguments.get("text")
        label = arguments.get("label")
        element = arguments.get("element")
        action = str(arguments.get("action") or "").strip()
        expect = arguments.get("expect")
        if plan is not None and not isinstance(plan, dict):
            return {"ok": False, "error": "plan must be an object"}

        def dispatch(cua: ModuleType, pid: int, window_id: int, name: str):
            if plan is not None:
                payload = dict(plan)
                if expect is not None and "expect" not in payload:
                    payload["expect"] = (
                        {"text": expect} if isinstance(expect, str) else expect
                    )
                return cua.run_actions(
                    pid, window_id, payload, app_name=name, foreground_prepared=False
                )
            if text is not None:
                if label:
                    return cua.type_label_action(
                        pid, window_id, str(label), str(text), app_name=name
                    )
                return cua.type_text(
                    pid,
                    window_id,
                    int(element) if element is not None else None,
                    str(text),
                )
            if action and action not in {"click", "press"} and action in ax_actions:
                fresh = cua.snapshot(pid, window_id, max_elements=200)
                target = int(element) if element is not None else None
                if target is None and label:
                    target = cua.find_clickable_index(fresh, str(label))
                if target is None:
                    return {"ok": False, "error": "element not found"}
                preflight = cua.pointer_preflight(
                    True,
                    name,
                    pid,
                    window_id,
                    fresh,
                    target,
                    f"Moving to {label or target}",
                )
                if preflight and not preflight.get("ok"):
                    return preflight
                return cua.merge_pointer_proof(
                    cua.perform_action(
                        pid, window_id, target, action, snapshot_data=fresh
                    ),
                    preflight,
                )
            if label:
                return cua.click_label_pointer(
                    pid, window_id, str(label), app_name=name
                )
            if element is not None:
                return cua.click(pid, window_id, int(element), app_name=name)
            return {
                "ok": False,
                "error": "act needs label, element, text, or plan",
            }

        return self._with_target(app, dispatch)
