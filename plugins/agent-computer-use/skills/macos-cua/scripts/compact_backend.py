#!/usr/bin/env python3
"""CUAService backend used by the compact MCP adapter."""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

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
INSTRUCTIONS = (
    "Thin MCP over CUAService (state + act only). Do not call start_session, "
    "verify, end_session, list_apps, or raw cua-driver. "
    "Two wall clocks: within-app batch one asserted act (steps+expect); "
    "cross-app one act per surface then switch — each extra tool is a full "
    "agent turn. Act-first when labels are known; state first only for "
    "discovery or after act miss. Never probe with state then state then act "
    "on the same app. Do not verify when act.verified is true — act already "
    "returns the settled tree. "
    "Friction or rooted inefficiency: encode an app-agnostic fast_path grader "
    "that fails the old trace, then retry; no named-app helpers; do not leave "
    "the fix in chat. "
    "Best first: overlay tip lands, then AX press (ax_timeout fail-closed). "
    "Fallback only on miss: at most one fresh state; then screenshot/PIXEL_CLICK. "
    "Never silent pixel fallback. Dispatch ok is never proof; no desktop-global click. "
    "Each batched act captures screenshot_before then screenshot_after (same tool, not a "
    "screenshot catalog). Inspect those pixels before trusting overlay/AX landing; if the "
    "before shot is the wrong window or a Stage Manager thumb, stop and correct bounds — "
    "do not add a third MCP tool. "
    "WhatsApp send/attach: $whatsapp skill, not these tools."
)
_BIDI = dict.fromkeys(
    map(
        ord,
        "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
        "\u2066\u2067\u2068\u2069\ufeff",
    )
)


_DASH = str.maketrans({
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
    0x2014: "-", 0x2015: "-", 0x2212: "-", 0xFE63: "-", 0xFF0D: "-",
})


def _norm_text(s: str) -> str:
    return (s or "").translate(_BIDI).translate(_DASH).strip().lower()


def expect_verified(expect: str, tree_text: str) -> bool:
    """Match expect against AXStaticText, AXTextArea, AXTextField, AXCell values — never button titles."""
    needle = _norm_text(expect)
    if not needle:
        return True
    values = [
        _norm_text(v)
        for v in re.findall(
            r'AX(?:StaticText|TextArea|TextField|Cell)[^\n]*value="([^"]*)"',
            tree_text,
        )
    ]
    values.extend(
        _norm_text(v) for v in re.findall(r'AXCell "([^"]*)"', tree_text)
    )
    for v in values:
        if not v:
            continue
        if needle == v:
            return True
        if len(needle) >= 2 and needle in v:
            return True
        if v.endswith("...") and len(v) > 8:
            stem = v[:-3]
            if stem and (needle.startswith(stem) or stem in needle):
                return True
    return False


def expect_is_new(expect: str, before_tree: str, after_tree: str) -> bool:
    """True when expect is a new value. Empty expect skips the check."""
    if not _norm_text(expect):
        return True
    return expect_verified(expect, after_tree) and not expect_verified(
        expect, before_tree
    )


def expectation_is_supported(expect: Any) -> bool:
    if isinstance(expect, str):
        return bool(_norm_text(expect))
    if isinstance(expect, list):
        return bool(expect) and all(expectation_is_supported(item) for item in expect)
    if not isinstance(expect, dict) or not expect:
        return False
    return any(
        bool(_norm_text(str(expect.get(key) or "")))
        for key in ("text", "not_text", "path")
    )


def expectation_is_new(
    expect: Any,
    before_tree: str,
    after_tree: str,
    results: list[dict[str, Any]] | None = None,
) -> bool:
    """Verify compact positive/negative postconditions against settled AX values."""
    if isinstance(expect, str):
        return expect_is_new(expect, before_tree, after_tree)
    if isinstance(expect, list):
        return bool(expect) and all(
            expectation_is_new(item, before_tree, after_tree, results) for item in expect
        )
    if not isinstance(expect, dict):
        return False
    checks: list[bool] = []
    if expect.get("text"):
        checks.append(expect_is_new(str(expect["text"]), before_tree, after_tree))
    if expect.get("not_text"):
        needle = str(expect["not_text"])
        checks.append(
            expect_verified(needle, before_tree)
            and not expect_verified(needle, after_tree)
        )
    if expect.get("path"):
        wanted = os.path.normpath(str(expect["path"]))
        checks.append(any(
            os.path.normpath(str(item.get("path"))) == wanted
            for item in (results or [])
            if item.get("path")
        ))
    return bool(checks) and all(checks)


def _has_ui_effect(arguments: dict[str, Any]) -> bool:
    keys = (
        "label", "element", "action", "op", "text", "key", "x", "y",
        "path", "url", "direction", "from_x", "from_y", "to_x", "to_y",
    )
    steps = arguments.get("steps")
    candidates = steps if isinstance(steps, list) and steps else [arguments]
    return any(
        isinstance(step, dict)
        and any(step.get(key) is not None for key in keys)
        and str(step.get("op") or step.get("action") or "").lower()
        not in {"focus", "raise", "activate"}
        for step in candidates
    )


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

    def state(self, app: str, **kwargs: Any) -> dict[str, Any]:
        max_elements = int(kwargs.get("max_elements") or DEFAULT_MAX)
        disable_diff = not bool(kwargs.get("diff"))

        def _call(client):
            return client.get_app_state(
                app, disableDiff=disable_diff, maxElements=max_elements
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
        return {
            "ok": True,
            "app": result.get("app") or app,
            "text": text,
            "elementCount": result.get("elementCount"),
            "screenshot": result.get("screenshot"),
            "pid": result.get("pid"),
        }

    def act(self, app: str, arguments: dict[str, Any]) -> dict[str, Any]:
        effectful = _has_ui_effect(arguments)
        expect = arguments.get("expect")
        allow_unverified = arguments.get("allow_unverified") is True
        if effectful and not expectation_is_supported(expect) and not allow_unverified:
            return {
                "ok": False,
                "verified": False,
                "dispatched": False,
                "error": "verification_required: mutating act needs expect",
                "error_type": "verification_required",
            }

        def _run(client):
            steps = arguments.get("steps")
            if not isinstance(steps, list) or not steps:
                steps = [arguments]
            native_steps: list[dict[str, Any]] = []
            after_new = False
            for step in steps:
                if not isinstance(step, dict):
                    native_steps = []
                    break
                native_steps.append(self._native_step(step, after_new_document=after_new))
                key = str(step.get("key") or "").lower().replace(" ", "")
                if key in {"cmd+n", "command+n", "cmd+t", "command+t"}:
                    after_new = True
                elif step.get("wait") is None:
                    after_new = False

            if native_steps and hasattr(client, "execute_plan"):
                plan = client.execute_plan(app, native_steps)
                before = plan.get("before") if isinstance(plan, dict) else {}
                after = plan.get("after") if isinstance(plan, dict) else {}
                raw_results = plan.get("results") if isinstance(plan, dict) else []
                results = [
                    self._normalize_step_result(item)
                    for item in (raw_results if isinstance(raw_results, list) else [])
                ]
            else:
                before = self._get_app_state(client, app, disableDiff=True)
                if isinstance(before, dict) and not before.get("screenshot"):
                    before = self._get_app_state(client, app, disableDiff=True)
                results = []
                after_new = False
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
                after = self._get_app_state(client, app, disableDiff=True)
                if isinstance(after, dict) and not after.get("screenshot"):
                    after = self._get_app_state(client, app, disableDiff=True)

            before_text = str(before.get("text") or "") if isinstance(before, dict) else ""
            text = str(after.get("text") or "") if isinstance(after, dict) else ""
            dispatched = all(item.get("ok") is True for item in results) and bool(results)
            verified = (
                expectation_is_new(expect, before_text, text, results)
                if effectful and expectation_is_supported(expect)
                else bool(dispatched and not effectful and text)
            )
            ok = bool(dispatched and verified)
            last = results[-1] if results else {}
            shot_before = before.get("screenshot") if isinstance(before, dict) else None
            shot_after = after.get("screenshot") if isinstance(after, dict) else None
            return {
                "ok": ok,
                "verified": verified,
                "dispatched": dispatched,
                "completion": "verified" if verified else "unverified",
                "method": last.get("method"),
                "text": text,
                "screenshot_before": shot_before,
                "screenshot_after": shot_after,
                "screenshot": shot_after,
                "results": results,
                "expect": expect,
                "error": (
                    "completion_unverified: action was dispatched; inspect settled state and do not claim done"
                    if dispatched and not verified
                    else last.get("error")
                ),
                "error_type": "completion_unverified" if dispatched and not verified else None,
            }

        return self._rpc(_run, retry=False)

    def _native_step(
        self, step: dict[str, Any], *, after_new_document: bool = False
    ) -> dict[str, Any]:
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
        if action and element is not None:
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
            return client.press_key(app, str(key))
        text = step.get("text")
        element = step.get("element")
        if text is not None and element is not None and step.get("label") is None:
            return client.set_value(app, int(element), str(text))
        if text is not None and step.get("label") is None and element is None:
            return client.type_text(
                app, str(text), after_new_document=after_new_document
            )
        action = step.get("action")
        element = step.get("element")
        if action and element is not None:
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
