#!/usr/bin/env python3
"""CUAService backend used by the compact MCP adapter."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from state_delta import action_state_text
from compact_verification import (
    apply_screenshot_policy,
    expect_is_new,
    expect_verified,
    expectation_is_new,
    expectation_is_supported,
)
from fail_feedback import apply_failure_feedback, VERIFICATION_REQUIRED
from compact_form_fills import (
    compile_error_payload,
    compile_set_value_step,
    dispatch_labeled_set_value,
    dispatch_set_checked,
    fill_value_from_step,
    has_ui_effect,
    is_app_open_step,
    new_document_split,
    is_label_fill_step,
    observed_window_id,
    plan_must_be_sequential,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parents[2]
SERVICE_DIR = SCRIPT_DIR.parent / "service"
DEFAULT_MAX = 80
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION,) + LEGACY_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
TOOL_NAMES = ("state", "act")
INSTRUCTIONS = (SCRIPT_DIR / "compact_instructions.txt").read_text()
NEW_DOCUMENT_SETTLE_S = 0.6


NO_WINDOW_HINT = "App has no open window: cua_act op=open with just app shows it (no path), then act."


class CUABackend:
    """Lazy JSON-RPC client. Tests replace compact_mcp._BACKEND."""

    def __init__(self) -> None:
        if str(SERVICE_DIR) not in sys.path:
            sys.path.insert(0, str(SERVICE_DIR))
        from cua_client import CUAClient  # noqa: WPS433

        self._client = CUAClient()

    def _cua(self):
        if self._client._sock is None:
            self._client.connect()
        return self._client

    def _reset(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        self._client.connect()

    def _rpc(self, fn, *, retry: bool = True):
        try:
            return fn(self._cua())
        except (ConnectionError, OSError, TimeoutError, socket.timeout):
            self._reset()
            if not retry:
                raise
            return fn(self._cua())

    def volume(self, direction: str) -> dict[str, Any]:
        return self._rpc(lambda client: client.call("system_volume", {"direction": direction}, retry=False), retry=False)

    def credential(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._rpc(lambda client: client.call("credential_" + operation, arguments, retry=False), retry=False)

    def state(self, app: str, **kwargs: Any) -> dict[str, Any]:
        max_elements = int(kwargs.get("max_elements") or DEFAULT_MAX)
        disable_diff = not bool(kwargs.get("diff"))
        want_shot = kwargs.get("screenshot") is True

        def _call(client):
            return client.get_app_state(
                app,
                disableDiff=disable_diff,
                maxElements=max_elements,
                includeScreenshot=want_shot,
            )

        result = self._rpc(_call)
        if not isinstance(result, dict) or not result.get("text"):
            self._reset()
            result = _call(self._cua())
        if not isinstance(result, dict):
            return {"ok": False, "error": "empty CUAService state", "app": app}
        text = str(result.get("text") or "")
        query = kwargs.get("query")
        if query:
            lines = [ln for ln in text.splitlines() if str(query) in ln]
            text = "\n".join(lines) if lines else text
        out = {
            "ok": True,
            "app": result.get("app") or app,
            "text": text,
            "elementCount": result.get("elementCount"),
            "screenshot": result.get("screenshot") if want_shot else None,
            "pid": result.get("pid"),
        }
        if result.get("elementCount") == 0:
            # Running but windowless (closed/hidden): acting blind here only burns retries.
            out["hint"] = NO_WINDOW_HINT
        return out

    def _show_app_window(self, app: str) -> dict[str, Any]:
        """Launch the app, or send a running windowless app the reopen event, then observe it.

        ``open -a`` is what Dock/Finder do: launches when needed and makes a running app with
        no window show its main window (Calendar, Music, Notes...). Absolute path: the voice
        helper runs under launchd's minimal PATH.
        """
        try:
            proc = subprocess.run(
                ["/usr/bin/open", "-a", app],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"could not open {app}: {exc}"[:240], "error_type": "app_not_found"}
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "").strip().splitlines()
            return {
                "ok": False,
                "error": (reason[-1] if reason else f"open -a {app} failed")[:240],
                "error_type": "app_not_found",
                "hint": f"{app} is not installed under that name; tell the user.",
                "retryable": False,
            }
        state: dict[str, Any] = {}
        deadline = time.monotonic() + 6.0
        while True:
            try:
                state = self.state(app)
            except Exception as exc:  # noqa: BLE001 - app still launching
                state = {"ok": False, "error": str(exc)[:240]}
            if state.get("ok") and state.get("elementCount"):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.4)
        shown = bool(state.get("ok") and state.get("elementCount"))
        return {
            "ok": shown,
            "verified": shown,
            "dispatched": True,
            "completion": "verified" if shown else "unverified",
            "method": "open-app",
            "app": state.get("app") or app,
            "text": state.get("text") or "",
            "elementCount": state.get("elementCount"),
            **({} if shown else {"error_type": "observation_incomplete", "hint": NO_WINDOW_HINT}),
        }

    def act(self, app: str, arguments: dict[str, Any]) -> dict[str, Any]:
        steps_in = arguments.get("steps") if isinstance(arguments.get("steps"), list) and arguments.get("steps") else [arguments]
        if any(is_app_open_step(step) for step in steps_in):
            shown = self._show_app_window(app)
            rest = [step for step in steps_in if not is_app_open_step(step)]
            if not shown.get("ok") or not rest:
                return shown
            if arguments.get("steps"):
                arguments = {**arguments, "steps": rest}
            else:
                arguments = {k: v for k, v in arguments.items() if k not in ("op", "action")}
        split = new_document_split(arguments)
        if split is not None:
            # cmd+n then typing in ONE plan pins every step to the window observed before the
            # plan (the old document), so CUAService rightly refuses the type as
            # type_refused_stale_document. Open the new window first, then plan the rest
            # against it, keeping the after-new-document guard on the first step.
            head, rest = split
            opened = self.act(app, {"steps": head, "allow_unverified": True})
            if not opened.get("dispatched"):
                return opened
            time.sleep(NEW_DOCUMENT_SETTLE_S)
            follow = {k: v for k, v in arguments.items() if k != "steps"}
            follow["steps"] = rest
            follow["_after_new_document"] = True
            return self.act(app, follow)
        effectful = has_ui_effect(arguments)
        expect = arguments.get("expect")
        allow_unverified = arguments.get("allow_unverified") is True
        if effectful and not expectation_is_supported(expect) and not allow_unverified:
            early = {
                "ok": False,
                "verified": False,
                "dispatched": False,
                "error": "verification_required: mutating act needs expect",
                "error_type": VERIFICATION_REQUIRED,
            }
            return apply_failure_feedback(
                early, arguments=arguments, before_text="", after_text=""
            )

        def _run(client):
            steps = arguments.get("steps")
            if not isinstance(steps, list) or not steps:
                steps = [arguments]
            # Label fills after nav/click/type must not compile against a stale
            # pre-plan snapshot — sequential _one re-resolves each fill (r9 #3).
            use_execute_plan = hasattr(client, "execute_plan") and not plan_must_be_sequential(steps)
            native_steps: list[dict[str, Any]] = []
            after_new = arguments.get("_after_new_document") is True
            elements_state: dict[str, Any] | None = None
            plan_window = None
            for step in steps:
                if not isinstance(step, dict):
                    native_steps = []
                    break
                if use_execute_plan and is_label_fill_step(step):
                    # Re-resolve immediately before each fill compile.
                    elements_state = self._get_app_state(
                        client, app, includeElements=True, disableDiff=True
                    )
                    seen = observed_window_id(elements_state)
                    if seen is not None:
                        plan_window = seen
                native = self._native_step(
                    step,
                    after_new_document=after_new,
                    elements_state=elements_state,
                )
                if native.get("compile_error"):
                    if not use_execute_plan:
                        native_steps = []; break  # _dispatch resolves labels
                    return compile_error_payload(native["compile_error"], expect)
                native_steps.append(native)
                key = str(step.get("key") or "").lower().replace(" ", "")
                if key in {"cmd+n", "command+n", "cmd+t", "command+t"}:
                    after_new = True
                elif step.get("wait") is None:
                    after_new = False

            if native_steps and use_execute_plan:
                # Pin keyboard plans to the observed window so press_key /
                # type_text children cannot land in a sibling window after a
                # focus switch (Codex r11 confirm major 3). Click-only plans
                # keep prior behavior (label fills already set plan_window).
                needs_keyboard_pin = any(
                    isinstance(s, dict)
                    and (
                        s.get("key")
                        or (
                            s.get("text") is not None
                            and not s.get("label")
                            and s.get("element") is None
                        )
                    )
                    for s in steps
                )
                if plan_window is None and needs_keyboard_pin:
                    elements_state = self._get_app_state(
                        client, app, includeElements=True, disableDiff=True
                    )
                    plan_window = observed_window_id(elements_state)
                plan = client.execute_plan(app, native_steps) if plan_window is None else client.execute_plan(app, native_steps, window_id=plan_window)
                before = plan.get("before") if isinstance(plan, dict) else {}
                after = plan.get("after") if isinstance(plan, dict) else {}
                raw_results = plan.get("results") if isinstance(plan, dict) else []
                results = [
                    self._normalize_step_result(item)
                    for item in (raw_results if isinstance(raw_results, list) else [])
                ]
            else:
                before = self._get_app_state(
                    client, app, disableDiff=True, includeScreenshot=False, includeElements=True
                )
                results = []
                after_new = arguments.get("_after_new_document") is True
                for step in steps:
                    if not isinstance(step, dict):
                        results.append({"ok": False, "error": "step must be an object"})
                        break
                    item = self._normalize_step_result(
                        self._one(client, app, step, after_new_document=after_new)
                    )
                    results.append(item)
                    if item.get("ok") is not True:
                        break
                    key = str(step.get("key") or "").lower().replace(" ", "")
                    if key in {"cmd+n", "command+n", "cmd+t", "command+t"}:
                        after_new = True
                    elif step.get("wait") is None:
                        after_new = False
                after = self._get_app_state(
                    client, app, disableDiff=True, includeScreenshot=False, includeElements=True
                )

            before_text = str(before.get("text") or "") if isinstance(before, dict) else ""
            text = str(after.get("text") or "") if isinstance(after, dict) else ""
            dispatched = all(item.get("ok") is True for item in results) and bool(results)
            if effectful and expectation_is_supported(expect):
                verified = expectation_is_new(expect, before_text, text, results)
                if isinstance(expect, (list, dict)):
                    verified = expectation_is_new(expect, before_text, text, results, after if isinstance(after, dict) else {})
            else:
                verified = bool(dispatched and not effectful and text)
            ok = bool(dispatched and verified)
            last = results[-1] if results else {}
            payload = {
                "ok": ok,
                "verified": verified,
                "dispatched": dispatched,
                "completion": "verified" if verified else "unverified",
                "method": last.get("method"),
                "text": action_state_text(before_text, text) if ok and effectful else text,
                "results": results,
                "expect": expect,
                "error": (
                    "completion_unverified: action was dispatched; read error_type/nearby (or one state) and do not claim done"
                    if dispatched and not verified
                    else last.get("error")
                ),
                "error_type": "completion_unverified" if dispatched and not verified else None,
            }
            fetch = lambda: self._get_app_state(
                client, app, disableDiff=True, includeScreenshot=True
            )
            policy = dict(before=before, after=after, arguments=arguments, fetch=fetch)
            if not ok:
                # allow_unverified without expect: dispatch-only; keep compact delta, not expect_unverified spam
                if (
                    allow_unverified
                    and dispatched
                    and not expectation_is_supported(expect)
                ):
                    payload["error_type"] = "allow_unverified"
                    payload["completion"] = "unverified"
                    delta = action_state_text(before_text, text) if effectful else ""
                    prefix = "allow_unverified: dispatched; do not claim done."
                    if delta.startswith("Changes during action") or delta.startswith("No changes during action"):
                        body = prefix + "\n" + delta
                    else:
                        body = prefix + " One fresh state only if labels unknown; else retry act."
                    payload["text"] = body
                    payload["full_text_omitted"] = True
                else:
                    payload = apply_failure_feedback(
                        payload,
                        arguments=arguments,
                        before_text=before_text,
                        after_text=text,
                    )
            return apply_screenshot_policy(payload, **policy)

        try:
            return self._rpc(_run, retry=False)
        except Exception as exc:
            err = str(exc)
            # Native RPC may raise before a structured step result exists.
            after = ""
            try:
                st = self.state(app)
                after = str(st.get("text") or "") if isinstance(st, dict) else ""
            except Exception:
                after = ""
            payload = {
                "ok": False,
                "verified": False,
                "dispatched": False,
                "completion": "unverified",
                "text": after,
                "results": [{"ok": False, "error": err}],
                "expect": arguments.get("expect"),
                "error": err,
                "error_type": None,
            }
            return apply_failure_feedback(
                payload, arguments=arguments, before_text="", after_text=after
            )

    def _native_step(self, step: dict[str, Any], *, after_new_document: bool = False,
                     elements_state: dict[str, Any] | None = None) -> dict[str, Any]:
        if step.get("wait") is not None:
            return {"method": "wait", "params": {"seconds": step["wait"]}}
        op = str(step.get("op") or "").lower()
        action = str(step.get("action") or "")
        if not op and action.lower() in {
            "open", "reveal", "scroll", "drag", "select_text", "focus", "raise", "activate",
        }:
            op = action.lower()
        if op in {"open", "reveal"}:
            params = {
                key: step[key] for key in ("path", "url") if step.get(key) is not None
            }
            if op == "reveal":
                params["reveal"] = True
            return {"method": "open_item", "params": params}
        if op in {"focus", "raise", "activate"}:
            return {"method": "get_app_state", "params": {"raiseForInput": True}}
        if op == "scroll":
            params = {
                "direction": str(step.get("direction") or "down"),
                "pages": int(step.get("pages") or 1),
            }
            if step.get("element") is not None:
                params["element_index"] = int(step["element"])
            return {"method": "scroll", "params": params}
        if op == "drag":
            return {
                "method": "drag",
                "params": {
                    key: float(step[key])
                    for key in ("from_x", "from_y", "to_x", "to_y")
                },
            }
        if op == "select_text":
            params = {
                "element_index": int(step["element"]),
                "text": str(step.get("text") or ""),
            }
            params.update({
                key: step[key]
                for key in ("prefix", "suffix", "selection_type")
                if step.get(key) is not None
            })
            return {"method": "select_text", "params": params}
        if step.get("key"):
            return {"method": "press_key", "params": {"key": str(step["key"])}}
        element = step.get("element")
        action_l = str(action or "").lower()
        fill_value = fill_value_from_step(step)
        if action_l == "set_value" and fill_value is not None:
            return compile_set_value_step(step, elements_state=elements_state)
        if step.get("text") is not None and element is not None and not step.get("label"):
            return {
                "method": "set_value",
                "params": {"element_index": int(element), "value": str(step["text"])},
            }
        if step.get("text") is not None and not step.get("label"):
            return {
                "method": "type_text",
                "params": {
                    "text": str(step["text"]),
                    "after_new_document": after_new_document,
                },
            }
        if action and element is not None and action_l not in {"click", "set_value", "type"}:
            return {
                "method": "perform_secondary_action",
                "params": {"element_index": int(element), "action": action},
            }
        params: dict[str, Any] = {}
        if step.get("label"):
            params["label"] = str(step["label"])
        if element is not None:
            params["element_index"] = int(element)
        if step.get("x") is not None and step.get("y") is not None:
            params.update({"x": float(step["x"]), "y": float(step["y"])})
        return {"method": "click", "params": params}

    def hide_agent_cursor(self) -> dict[str, Any]:
        def _run(client):
            return client.hide_agent_cursor()

        try:
            result = self._rpc(_run)
            return result if isinstance(result, dict) else {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _one(
        self, client: Any, app: str, step: dict[str, Any], after_new_document: bool = False
    ) -> dict[str, Any]:
        try:
            return self._dispatch(client, app, step, after_new_document)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _dispatch(
        self,
        client: Any,
        app: str,
        step: dict[str, Any],
        after_new_document: bool = False,
    ) -> dict[str, Any]:
        if step.get("wait") is not None:
            seconds = min(max(float(step["wait"]), 0.0), 45.0)
            time.sleep(seconds)
            return {"ok": True, "method": "wait", "wait": seconds}
        op = str(step.get("op") or "").lower()
        action_name = str(step.get("action") or "")
        if not op and action_name.lower() in {
            "open", "reveal", "scroll", "drag", "select_text",
        }:
            op = action_name.lower()
        if op in {"focus", "raise", "activate"}:
            return self._focus_app(client, app)
        if op in {"open", "reveal"}:
            kwargs = {
                key: step[key] for key in ("path", "url") if step.get(key) is not None
            }
            if op == "reveal":
                kwargs["reveal"] = True
            return client.open_item(app, **kwargs)
        if op == "scroll":
            kwargs = {"pages": int(step.get("pages") or 1)}
            if step.get("element") is not None:
                kwargs["element_index"] = int(step["element"])
            return client.scroll(app, str(step.get("direction") or "down"), **kwargs)
        if op == "drag":
            return client.drag(
                app,
                *[float(step[key]) for key in ("from_x", "from_y", "to_x", "to_y")],
            )
        if op == "select_text":
            kwargs = {
                key: step[key]
                for key in ("prefix", "suffix", "selection_type")
                if step.get(key) is not None
            }
            return client.select_text(
                app, int(step["element"]), str(step.get("text") or ""), **kwargs
            )
        key = step.get("key")
        if key:
            # Pin keyboard to the observed window (Codex r11 confirm major 3).
            state = self._get_app_state(
                client, app, includeElements=True, disableDiff=True
            )
            wid = observed_window_id(state)
            if wid is not None:
                return client.press_key(app, str(key), window_id=wid)
            return client.press_key(app, str(key))
        text = step.get("text")
        element = step.get("element")
        action = step.get("action")
        action_name = str(action or "").lower()
        fill_value = fill_value_from_step(step)
        if action_name == "set_checked":
            return dispatch_set_checked(
                client, app, step,
                lambda: self._get_app_state(client, app, includeElements=True, disableDiff=True),
            )
        if action_name == "set_value" and fill_value is not None:
            return dispatch_labeled_set_value(
                client, app, step, element, fill_value,
                lambda: self._get_app_state(client, app, includeElements=True, disableDiff=True),
            )
        if text is not None and element is not None and step.get("label") is None:
            state = self._get_app_state(
                client, app, includeElements=True, disableDiff=True
            )
            wid = observed_window_id(state)
            if wid is not None:
                return client.set_value(app, int(element), str(text), window_id=wid)
            return client.set_value(app, int(element), str(text))
        if text is not None and step.get("label") is None and element is None:
            state = self._get_app_state(
                client, app, includeElements=True, disableDiff=True
            )
            wid = observed_window_id(state)
            if wid is not None:
                return client.type_text(
                    app, str(text), after_new_document=after_new_document, window_id=wid
                )
            return client.type_text(
                app, str(text), after_new_document=after_new_document
            )
        element = step.get("element")
        if action and element is not None and action_name not in {"click", "set_value", "type"}:
            return client.perform_secondary_action(app, int(element), str(action))
        kwargs: dict[str, Any] = {}
        if step.get("label"):
            kwargs["label"] = str(step["label"])
        if element is not None:
            kwargs["element_index"] = int(element)
        if step.get("x") is not None and step.get("y") is not None:
            kwargs["x"] = float(step["x"])
            kwargs["y"] = float(step["y"])
        if not kwargs:
            action = str(step.get("action") or "").lower()
            if step.get("focus") is True or action in {"focus", "raise", "activate"}:
                return self._focus_app(client, app)
            # App-only act (launch + raise) — common for "bring X to front".
            meta = {
                "step_label",
                "expect",
                "risky",
                "app",
                "steps",
                "action",
                "focus",
            }
            if not any(k not in meta and step.get(k) is not None for k in step):
                return self._focus_app(client, app)
            return {"ok": False, "error": "act needs label, element, text, key, or x/y"}
        return client.click(app, **kwargs)

    def _focus_app(self, client: Any, app: str) -> dict[str, Any]:
        result = self._get_app_state(
            client, app, disableDiff=True, raiseForInput=True
        )
        if isinstance(result, dict) and result.get("app"):
            return {"ok": True, "method": "focus", "app": result.get("app")}
        return {"ok": False, "error": f"could not focus {app}"}

    def _get_app_state(self, client: Any, app: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return client.get_app_state(app, **kwargs)
            except Exception as exc:
                if "No window for app:" not in str(exc) or attempt == 2:
                    raise
                time.sleep(0.4)
        raise AssertionError("unreachable")

    def _normalize_step_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"ok": False, "error": "step result must be an object"}
        if result.get("method") != "cgevent-click":
            return result
        point = result.get("point")
        if not isinstance(point, dict):
            return {**result, "ok": False, "error": "nonfinite click point"}
        if point.get("x") is None or point.get("y") is None:
            return {**result, "ok": False, "error": "nonfinite click point"}
        return result
