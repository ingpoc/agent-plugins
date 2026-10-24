"""Realtime-facing tools — CUA + confirm + secrets. No named-app helpers."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit
from typing import Any

from voice_cua.catalog import (
    load_catalog,
)
from voice_cua.cua_bridge import get_backend, get_tool_schemas, redact_for_model
from voice_cua.comet_bridge import (
    ALLOWED_ACTIONS,
    comet_act,
    comet_begin,
    comet_end,
    comet_is_active,
    redact_actions,
    redact_url,
)
from voice_cua.inventory import build_inventory
from voice_cua.inject import clipboard_paste_secret, clear_clipboard, set_clipboard
from voice_cua.labels_tracker import labels_tracker_path, refresh_labels_tracker
from voice_cua.keychain_access import describe_ref, enrich_items, fetch_value, list_keychain, resolve_ref
from voice_cua.activity_log import log_event, log_exception, log_tool_result, log_tool_start
from voice_cua.island_facade import island_confirm, island_publish
from voice_cua.risk_gate import GATE, fingerprint
from voice_cua.credential_capture import capture_credential
from voice_cua import workflow_store

RISKY_HINTS = (
    "delete",
    "remove",
    "send",
    "purchase",
    "pay",
    "overwrite",
    "format",
    "erase",
)

_NO_WINDOW_APPS = frozenset({
    "system events",
    "loginwindow",
    "windowmanager",
    "control center",
    "notification center",
})


_listening_reset_timer: threading.Timer | None = None
_listening_reset_lock = threading.Lock()


def _schedule_listening_reset(delay: float = 2.0) -> None:
    global _listening_reset_timer

    def _reset() -> None:
        island_publish("listening", title="Listening", detail="")

    with _listening_reset_lock:
        if _listening_reset_timer is not None:
            _listening_reset_timer.cancel()
        _listening_reset_timer = threading.Timer(delay, _reset)
        _listening_reset_timer.daemon = True
        _listening_reset_timer.start()


COMPACT_ACT_DESCRIPTION = (
    "Drive one Mac app in one native plan (op, or steps for a batch). op=open with only app "
    "launches it or shows its window (use when state is empty); with path opens that file. ok/verified only when expect matches. Failures return error_type, hint and "
    "retryable; max 2 tries per step."
)
# Fields the voice schema keeps out of the per-step list (top-level only).
_ACT_TOP_ONLY = ("app", "steps", "expect", "allow_unverified", "screenshot")


def _slim(prop: dict[str, Any]) -> dict[str, Any]:
    """Keep only type + enum (the backend validates ranges and nesting)."""
    return {k: v for k, v in prop.items() if k in {"type", "enum"}}


def compact_act_parameters(backend: dict[str, Any]) -> dict[str, Any]:
    """Voice cua_act schema derived from the compact backend schema (same names/ops).

    Halves the tokens: per-step fields appear once (steps items are "same fields
    as top level"), ranges/descriptions are dropped and expect is a short union.
    """
    props = backend["properties"]
    out: dict[str, Any] = {"app": _slim(props["app"])}
    for key, prop in props.items():
        if key in _ACT_TOP_ONLY:
            continue
        out[key] = _slim(prop)
    out["steps"] = {
        "type": "array",
        "items": {"type": "object"},
        "description": "Batch: each item is one op using the fields above",
    }
    out["expect"] = {
        "oneOf": [
            {"type": "string"},
            {"type": "object"},
            {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "object"}]}},
        ],
        "description": "Postcondition: text, {text|not_text|path|value:{label,equals}}, or a list",
    }
    out["allow_unverified"] = {"type": "boolean", "description": "Dispatch-only; never report done"}
    out["screenshot"] = {"type": "boolean"}
    out["step_label"] = {"type": "string"}
    out["risky"] = {"type": "boolean", "description": "Irreversible: needs the user's OK"}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": out,
        "required": list(backend.get("required") or ["app"]),
    }


TOOL_GROUPS = {
    # Lazy-loaded: never used in normal turns, ~850 tokens on every response.
    "secrets": ("secrets_list", "secrets_label", "secrets_get", "secrets_put", "secrets_inject",
                "secrets_provide", "confirm_risky"),
    "workflows": ("workflow_read", "workflow_save"),
    # Loaded by the risk gate the moment a risky action is refused.
    "confirm": ("confirm_risky",),
}


def _lazy_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "confirm_risky",
            "description": "Ask the user to OK the risky action that was just refused; then retry that exact call.",
            "parameters": {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
            },
        },
        {
            "type": "function",
            "name": "secrets_list",
            "description": (
                "List login Keychain secrets the agent can use (metadata only — no values). "
                "Searches label, service, account. Optional catalog hints when registered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filter by label/service/account substring"},
                    "limit": {"type": "integer"},
                    "platform": {"type": "string", "description": "Filter catalog-registered platform only"},
                },
            },
        },
        {
            "type": "function",
            "name": "secrets_label",
            "description": "Look up one Keychain item by exact label (metadata only).",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        },
        {
            "type": "function",
            "name": "secrets_get",
            "description": (
                "Verify a Keychain item exists by catalog id, label, service+account, or search query. "
                "Never returns the secret value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "service": {"type": "string"},
                    "account": {"type": "string"},
                    "query": {"type": "string"},
                },
            },
        },
        {
            "type": "function",
            "name": "secrets_put",
            "description": (
                "Open the Dynamic Island secure entry form to save a password or API key in login Keychain. "
                "Pass metadata only; the user enters the value in the native form and approves Save."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "platform": {"type": "string"},
                    "env": {"type": "string"},
                    "label": {"type": "string"},
                    "service": {"type": "string"},
                    "account": {"type": "string"},
                },
                "additionalProperties": False,
                "required": ["id", "label", "service"],
            },
        },
        {
            "type": "function",
            "name": "secrets_inject",
            "description": (
                "Fetch from login Keychain by id/label/service/query, paste into target app via clipboard, "
                "then clear clipboard. Never type_text the secret. Requires user confirm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "service": {"type": "string"},
                    "account": {"type": "string"},
                    "query": {"type": "string"},
                    "app": {"type": "string"},
                    "navigate_steps": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "CUA steps to focus the secret field before paste",
                    },
                },
                "required": ["app"],
            },
        },
        {
            "type": "function",
            "name": "secrets_provide",
            "description": (
                "On user request: put a Keychain secret on clipboard briefly, or inject into an app. "
                "Resolve by label/service/query. Never return the value in tool output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["clipboard", "inject"],
                        "description": "clipboard = copy for user to paste elsewhere; inject = paste into app",
                    },
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "service": {"type": "string"},
                    "account": {"type": "string"},
                    "query": {"type": "string"},
                    "app": {"type": "string", "description": "Required when action=inject"},
                    "seconds": {
                        "type": "number",
                        "description": "Clipboard lifetime for action=clipboard (default 30, then cleared)",
                    },
                    "navigate_steps": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                "required": ["action"],
            },
        },
    ]


def tool_definitions(
    *,
    comet_active: bool | None = None,
    groups: frozenset[str] | set[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    cua = get_tool_schemas()
    wanted = {name for group in groups for name in TOOL_GROUPS.get(group, ())}
    tools = [
        {
            "type": "function",
            "name": "cua_state",
            "description": (
                "Compact AX state for any Mac app (name or bundle id). "
                "Discovery or after act miss only — not before every act."
            ),
            "parameters": cua["state"]["inputSchema"],
        },
        {
            "type": "function",
            "name": "cua_act",
            "description": COMPACT_ACT_DESCRIPTION,
            "parameters": compact_act_parameters(cua["act"]["inputSchema"]),
        },
    ]
    tools.append({"type": "function", "name": "system_volume",
        "description": "Read or adjust the Mac output volume one step, with actual system-level readback. Use instead of Settings sliders or guessed function keys. Only claim a change when verified; at_limit means already at the limit.",
        "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["read", "up", "down"]}},
                       "required": ["direction"], "additionalProperties": False}})
    if not all(set(TOOL_GROUPS[group]).issubset(wanted) for group in ("secrets", "workflows")):
        tools.append({
            "type": "function",
            "name": "more_tools",
            "description": "Load extra tools. secrets: secure Island entry/Keychain. workflows: read or create/repair task procedures.",
            "parameters": {
                "type": "object",
                "properties": {"group": {"type": "string", "enum": ["secrets", "workflows"]}},
                "required": ["group"],
            },
        })
    tools.append({
        "type": "function",
        "name": "show_details",
        "description": "Show a result card on the Dynamic Island (e.g. a deal: price, seller, link).",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "detail": {"type": "string"}},
            "required": ["title"],
        },
    })
    tools.extend(t for t in _lazy_tools() + _workflow_tools() if t["name"] in wanted)
    # Browser schemas stay available; connection remains lazy until comet_begin.
    tools.append({
        "type": "function",
        "name": "comet_begin",
        "description": (
            "Open a website in Comet (isolated visible window), or — if the user's Comet window is "
            "already open — load the URL in that same window. purpose=user_task (default) for "
            "anything the user asked; that window is never closed afterwards. purpose=self_test "
            "only for your own checks; that window closes on comet_end."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL, e.g. https://www.netflix.com"},
                "purpose": {"type": "string", "enum": ["user_task", "self_test"]},
            },
            "required": ["url"],
        },
    })
    tools.extend([
            {
                "type": "function",
                "name": "comet_act",
                "description": (
                    "Run one batch in an active isolated Comet lease; call comet_begin first. Common actions: page_context; "
                    "goto {url}; click_text {text}; click_selector {selector}; fill_selector "
                    "{selector,value}; wait_for_selector; wait_for_url_change; screenshot {format:'png'}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
                                    "url": {"type": "string", "description": "goto: absolute https URL"},
                                    "text": {"type": "string", "description": "click_text: visible text"},
                                    "selector": {"type": "string"},
                                    "value": {"type": "string", "description": "fill_selector value"},
                                },
                                "required": ["type"],
                            },
                            "minItems": 1,
                        },
                        "timeout": {"type": "number", "minimum": 1, "maximum": 90},
                        "risky": {"type": "boolean", "description": "Confirm before irreversible or external actions"},
                    },
                    "required": ["actions"],
                },
            },
            {
                "type": "function",
                "name": "comet_end",
                "description": (
                    "Finish the Comet task after comet_begin. A self_test window closes. The user's window stays open "
                    "unless user_requested=true because the user explicitly asked to close it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"user_requested": {"type": "boolean"}},
                },
            },
        ])
    return tools


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "cua_state": _cua_state,
        "cua_act": _cua_act,
        "comet_begin": _comet_begin,
        "comet_act": _comet_act,
        "comet_end": _comet_end,
        "confirm_risky": _confirm_risky,
        "secrets_list": _secrets_list,
        "secrets_label": _secrets_label,
        "secrets_get": _secrets_get,
        "secrets_put": _secrets_put,
        "secrets_inject": _secrets_inject,
        "secrets_provide": _secrets_provide,
        "more_tools": _more_tools,
        "show_details": _show_details,
        "workflow_read": workflow_store.read_workflow,
        "workflow_save": workflow_store.save_workflow,
        "system_volume": _system_volume,
    }
    fn = handlers.get(name)
    if not fn:
        log_event("tool_error", tool=name, ok=False, error=f"unknown tool {name}")
        return {"ok": False, "error": f"unknown tool {name}"}
    args = arguments or {}
    app = str(args.get("app") or "")
    log_tool_start(name, args)
    blocked = workflow_store.mutation_guard(name, args)
    if blocked is not None:
        return blocked
    try:
        result = fn(args)
        if not result.get("cancelled") and (name != "system_volume" or args.get("direction", "read") != "read"):
            workflow_store.note_failure(name, result if isinstance(result, dict) else {"ok": False})
        log_tool_result(name, result if isinstance(result, dict) else {"ok": False}, app=app)
        return result
    except Exception as exc:  # noqa: BLE001 — surface to model
        log_exception(name, exc, app=app)
        island_publish("error", title="Tool error", detail=str(exc)[:120], app=app)
        return {"ok": False, "error": str(exc)}


def _cua_state(args: dict[str, Any]) -> dict[str, Any]:
    app = str(args["app"])
    island_publish("driving", title=app, app=app, step="discover", detail=app)
    backend = get_backend()
    kwargs: dict[str, Any] = {}
    if args.get("query"):
        kwargs["query"] = args["query"]
    if args.get("diff"):
        kwargs["diff"] = True
    if args.get("max") is not None:
        kwargs["max_elements"] = int(args["max"])
    result = backend.state(app, **kwargs)
    return redact_for_model(result if isinstance(result, dict) else {"ok": False})


def _looks_risky(args: dict[str, Any]) -> bool:
    if args.get("risky") is True:
        return True
    blob = " ".join(
        str(args.get(k) or "")
        for k in ("label", "text", "key", "step_label", "expect")
    ).lower()
    steps = args.get("steps")
    action_groups = [steps, args.get("actions")]
    for group in action_groups:
        if not isinstance(group, list):
            continue
        for s in group:
            if isinstance(s, dict):
                blob += " " + " ".join(str(v) for v in s.values()).lower()
    return any(h in blob for h in RISKY_HINTS)


def _cua_act(args: dict[str, Any]) -> dict[str, Any]:
    app = str(args["app"])
    if app.strip().lower() in _NO_WINDOW_APPS:
        return {
            "ok": False,
            "error": f"{app} has no window — use a regular app (Calculator, Music, Finder, TextEdit).",
        }
    step = str(args.get("step_label") or args.get("label") or args.get("key") or "act")
    # Retry identity = the action, not how it is verified (a new expect is still the same step).
    step_fp = fingerprint("cua_act", args, ignore=_STEP_IGNORE)
    capped = _retry_capped(step_fp)
    if capped is not None:
        return capped
    if _looks_risky(args):
        refused = GATE.check("cua_act", args, f"Allow in {app}: {_describe_steps(args, step)}?")
        if refused is not None:
            island_publish("acting", title="Needs your OK", app=app, detail=step)
            return refused
    island_publish("driving", title=f"Using {app}" if app else "Working", app=app, step=step, detail=step)
    backend = get_backend()
    result = backend.act(app, args)
    out = redact_for_model(result if isinstance(result, dict) else {"ok": False})
    if out.get("ok"):
        _STEP_FAILS.pop(step_fp, None)
        island_publish("done", title="Done", app=app, step=step, detail=f"{app} · {step}")
    elif out.get("needs_confirmation") or str(out.get("error_type") or "").startswith("needs_confirm"):
        # Not a failure: the confirm card follows (no "Missed that, retrying" on the island).
        island_publish("acting", title="Needs your OK", app=app, detail=step)
    else:
        _note_failure(step_fp, out)
        err = str(out.get("error") or out.get("error_type") or "")
        # Raw error goes in; IslandBus maps it to human copy and logs the raw text.
        island_publish("error", title=err or "error", app=app, step=step)
        _schedule_listening_reset()
    return out


# -- cua_act failure hints + per-step retry cap -------------------------------
MAX_STEP_TRIES = 2
_STEP_IGNORE = frozenset({"expect", "allow_unverified", "wait"})
STEP_FAIL_TTL = 600.0
_STEP_FAILS: dict[str, tuple[int, float]] = {}
_STEP_LOCK = threading.Lock()
FAIL_HINTS = {
    "target_missing": ("Control not found: cua_state with a query, then retry by label.", True),
    "stale_id": ("Ids changed: retry by label, not element id.", True),
    "observation_incomplete": ("No fresh view: cua_state once, then decide; don't replay blindly.", True),
    "action_failed": ("Step failed: try one other route (label, key or coordinates).", True),
    "verification_required": ("Add expect (or allow_unverified for dispatch-only) and retry.", True),
    "expect_unverified": ("It ran but isn't confirmed: read the settled state in this result (no cua_state); don't repeat it.", False),
    "user_denied": ("User said no: don't retry.", False),
}
CAP_HINT = "Stopped after 2 tries: tell the user what blocked it, or take a different approach."


def _describe_steps(args: dict[str, Any], step: str) -> str:
    ops = [str(s.get("op") or s.get("key") or s.get("label") or "") for s in args.get("steps") or [] if isinstance(s, dict)]
    text = step if not ops else ", ".join(o for o in ops if o)[:80] or step
    return text[:100]


def _nav_note(navigate: Any) -> str:
    """Confirm copy must name the navigation that runs before a paste/fill."""
    if not isinstance(navigate, list) or not navigate:
        return ""
    return f" First: {_describe_steps({'steps': navigate}, 'navigate')}."


def _retry_capped(step_fp: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _STEP_LOCK:
        for key in [k for k, (_, ts) in _STEP_FAILS.items() if now - ts > STEP_FAIL_TTL]:
            _STEP_FAILS.pop(key, None)
        count = _STEP_FAILS.get(step_fp, (0, now))[0]
    if count < MAX_STEP_TRIES:
        return None
    log_event("cua_act_retry_capped", tries=count)
    return {"ok": False, "error": "retry cap reached for this step", "error_type": "retry_cap",
            "retryable": False, "hint": CAP_HINT}


def _note_failure(step_fp: str, out: dict[str, Any]) -> None:
    error_type = str(out.get("error_type") or "")
    if not error_type and str(out.get("error") or "") == "user denied":
        error_type = "user_denied"
    with _STEP_LOCK:
        count = _STEP_FAILS.get(step_fp, (0, 0.0))[0] + 1
        _STEP_FAILS[step_fp] = (count, time.monotonic())
    hint, retryable = FAIL_HINTS.get(error_type, ("Try one different approach, then tell the user.", True))
    if count >= MAX_STEP_TRIES:
        hint, retryable = CAP_HINT, False
    out["hint"] = hint
    out["retryable"] = bool(retryable)
    out["tries"] = count


def reset_session_state() -> None:
    """New Realtime conversation: fresh retry budget, no carried-over approvals."""
    with _STEP_LOCK:
        _STEP_FAILS.clear()
    GATE.reset()
    workflow_store.reset_session()


SHOW_DETAILS_MAX = 400


def _show_details(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()[:80]
    detail = str(args.get("detail") or "").strip()[:SHOW_DETAILS_MAX]
    if not title:
        return {"ok": False, "error": "title required"}
    island_publish("done", title=title, detail=detail)
    return {"ok": True, "shown": True}


def _more_tools(args: dict[str, Any]) -> dict[str, Any]:
    group = str(args.get("group") or "").strip().lower()
    if group not in TOOL_GROUPS or group == "confirm":
        return {"ok": False, "error": "unknown group", "hint": "group: secrets or workflows"}
    return {"ok": True, "group": group, "loaded": list(TOOL_GROUPS[group])}


def comet_begin_label(url: str, purpose: str, *, reuse: bool = False) -> tuple[str, str]:
    """Island (title, detail) for comet_begin: the site, not lease internals."""
    host = (urlsplit(str(url or "")).hostname or "").removeprefix("www.") or "website"
    if purpose == "self_test":
        return f"Self-check · {host}", "Comet"
    return (f"Loading {host}" if reuse else f"Opening {host}"), "Comet"


def _comet_begin(args: dict[str, Any]) -> dict[str, Any]:
    purpose = str(args.get("purpose") or "user_task")
    title, detail = comet_begin_label(
        str(args.get("url") or ""), purpose, reuse=comet_is_active() and purpose == "user_task"
    )
    island_publish("acting", title=title, detail=detail, app="Comet")
    log_event("comet_call", tool="comet_begin", url=redact_url(str(args.get("url") or "")), purpose=purpose)
    result = comet_begin(str(args.get("url") or ""), purpose=purpose)
    host = (urlsplit(str(args.get("url") or "")).hostname or "").removeprefix("www.")
    if result.get("ok"):
        island_publish("done", title=f"{host or 'Page'} open", detail="Comet", app="Comet")
        result.setdefault("next", "Loaded, not read yet: read it with comet_act [{type: text}] before answering.")
    else:
        reason = str(result.get("error") or "").removeprefix("Comet Control unavailable: ")
        island_publish("error", title="Comet unavailable", detail=(reason or host)[:120], app="Comet")
    return result


_GOTO_URL_ALIASES = ("href", "link", "target", "value", "text", "uri", "location")


def _normalize_comet_action(action: dict[str, Any]) -> dict[str, Any]:
    """Models sometimes put a goto's URL under another key; move it to ``url``."""
    if action.get("type") == "navigate":
        action = {**action, "type": "goto"}
    if action.get("type") != "goto" or str(action.get("url") or "").startswith(("http://", "https://")):
        return action
    for key in ("url",) + _GOTO_URL_ALIASES:
        value = str(action.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return {**{k: v for k, v in action.items() if k != key}, "url": value}
    for key in ("url",) + _GOTO_URL_ALIASES:
        value = str(action.get(key) or "").strip().lstrip("/")
        if value and "." in value.split("/")[0] and " " not in value:
            return {**{k: v for k, v in action.items() if k != key}, "url": "https://" + value}
    return action


def _comet_act(args: dict[str, Any]) -> dict[str, Any]:
    if not comet_is_active():
        return {"ok": False, "error": "no active Comet task lease; call comet_begin first"}
    actions = args.get("actions")
    if not isinstance(actions, list) or not all(isinstance(action, dict) for action in actions):
        return {"ok": False, "error": "actions must be an array of objects"}
    actions = [_normalize_comet_action(action) for action in actions]
    bad_goto = next((a for a in actions if a.get("type") == "goto" and not str(a.get("url") or "").startswith(("http://", "https://"))), None)
    if bad_goto is not None:
        # A goto with no url ran 17 s and timed out as "goto undefined" (c17b); fail fast instead.
        log_event("tool_error", tool="comet_act", ok=False, error="goto without an absolute url",
                  error_type="bad_arguments", keys=sorted(k for k in bad_goto if k != "type")[:6])
        return {"ok": False, "error": "goto needs {type: 'goto', url: 'https://...'} (absolute URL)",
                "error_type": "bad_arguments", "retryable": True}
    log_event("comet_call", tool="comet_act", actions=redact_actions(actions))
    # Same per-step retry cap as cua_act: an identical failing action list runs at most twice
    # (matrix: one 'goto' miss was replayed 6x until the turn timed out).
    step_fp = fingerprint("comet_act", {"actions": actions}, ignore=frozenset({"timeout"}))
    capped = _retry_capped(step_fp)
    if capped is not None:
        return capped
    if _looks_risky(args):
        refused = GATE.check("comet_act", args, f"Allow in Comet: {', '.join(redact_actions(actions))[:90]}?")
        if refused is not None:
            island_publish("acting", title="Needs your OK", app="Comet", detail="Browser action")
            return refused
    island_publish("driving", title="Driving Comet", detail="Browser task")
    result = comet_act(actions, timeout=max(1.0, min(90.0, float(args.get("timeout") or 45))))
    island_publish("done" if result.get("ok") else "error", title="Comet done" if result.get("ok") else "Comet miss", detail="Browser task")
    if isinstance(result, dict):
        if result.get("ok"):
            _STEP_FAILS.pop(step_fp, None)
        else:
            _note_failure(step_fp, result)
    return result


def _comet_end(args: dict[str, Any]) -> dict[str, Any]:
    user_requested = bool(args.get("user_requested"))
    log_event("comet_call", tool="comet_end", user_requested=user_requested)
    result = comet_end(user_requested=user_requested)
    if not result.get("ok"):
        island_publish("error", title="Couldn't close Comet", detail="")
    elif result.get("kept_open"):
        island_publish("done", title="Comet stays open", detail="Ready for follow-ups")
    else:
        island_publish("done", title="Comet window closed", detail="Comet")
    return result


def _confirm_risky(args: dict[str, Any]) -> dict[str, Any]:
    # The island shows the server's description of the parked action, never the
    # model's prompt, so the user approves exactly what will run.
    def _ask(cid: str, text: str) -> bool:
        t0 = time.monotonic()
        log_event("confirm_request", confirm_id=cid[:40], prompt_chars=len(text or ""))
        approved = island_confirm(cid, text)
        log_event("confirm_answer", confirm_id=cid[:40], approved=bool(approved),
                  waited_s=round(time.monotonic() - t0, 1))
        return approved

    result = GATE.confirm(_ask)
    if result.get("error"):
        return result
    approved = bool(result.get("approved"))
    island_publish(
        "done" if approved else "error",
        title="Confirmed" if approved else "Denied",
        detail="",
    )
    return result


def _resolve_secret_args(args: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    return resolve_ref(
        id=str(args.get("id") or ""),
        label=str(args.get("label") or ""),
        service=str(args.get("service") or ""),
        account=str(args.get("account") or ""),
        query=str(args.get("query") or ""),
    )


def _display_name(item: dict[str, str]) -> str:
    return str(item.get("label") or item.get("display") or item.get("service") or "secret")


def _secrets_list(args: dict[str, Any]) -> dict[str, Any]:
    platform = str(args.get("platform") or "").lower().strip()
    if platform:
        inv = build_inventory(platform=platform, query=str(args.get("query") or ""))
        island_publish("secrets", title="Secrets", detail=f"{inv['available_count']} catalog", app="Keychain Access")
        return {
            "ok": True,
            "source": "catalog",
            "available_labels": inv["available_labels"],
            "missing_labels": inv["missing_labels"],
            "keys": inv["keys"],
        }
    limit = int(args.get("limit") or 80)
    items = enrich_items(list_keychain(query=str(args.get("query") or ""), limit=limit))
    tracker = refresh_labels_tracker()
    island_publish("secrets", title="Keychain", detail=f"{len(items)} items", app="Keychain Access")
    return {
        "ok": True,
        "source": "keychain",
        "count": len(items),
        "keys": items,
        "labels_file": str(labels_tracker_path()),
        "available": tracker["available"],
        "missing": tracker["missing"],
        "passwords_app_note": "Passwords-app-only items need one-time mirror into login Keychain",
    }


def _secrets_label(args: dict[str, Any]) -> dict[str, Any]:
    label = str(args.get("label") or "").strip()
    if not label:
        return {"ok": False, "error": "label required"}
    item, err = resolve_ref(label=label)
    if err or not item:
        island_publish("error", title="Couldn't find secret", detail="")
        return {"ok": False, "error": err or f"Keychain miss for {label}"}
    island_publish("secrets", title="Using Keychain", detail=_display_name(item))
    return describe_ref(item)


def _secrets_get(args: dict[str, Any]) -> dict[str, Any]:
    item, err = _resolve_secret_args(args)
    if err or not item:
        island_publish("error", title="Couldn't find secret", detail="")
        return {"ok": False, "error": err or "not found"}
    name = _display_name(item)
    island_publish("secrets", title="Using Keychain", detail=f"{name} ready")
    return describe_ref(item)


def _run_inject(item: dict[str, str], app: str, navigate: Any) -> dict[str, Any]:
    name = _display_name(item)
    backend = get_backend()
    if isinstance(navigate, list) and navigate:
        nav = backend.act(app, {"app": app, "steps": navigate})
        if not isinstance(nav, dict) or nav.get("ok") is not True:
            clear_clipboard()
            return {"ok": False, "error": "navigate failed", "display": name}
    secret = fetch_value(item)
    try:
        result = clipboard_paste_secret(app, secret, backend=backend)
    finally:
        secret = ""
        clear_clipboard()
        time.sleep(0.05)
        clear_clipboard()
    verified = isinstance(result, dict) and result.get("ok") is True and result.get("verified") is True
    return {"ok": verified, "display": name, "app": app, "injected": verified,
            "dispatched": bool(isinstance(result, dict) and result.get("dispatched")), "verified": verified,
            **({} if verified else {"error": "Credential paste was not confirmed.", "error_type": "injection_unverified"})}


def _secrets_inject(args: dict[str, Any]) -> dict[str, Any]:
    app = str(args.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app required"}
    item, err = _resolve_secret_args(args)
    if err or not item:
        return {"ok": False, "error": err or "not found"}
    name = _display_name(item)
    refused = GATE.check("secrets_inject", args, f"Paste {name} into {app}?{_nav_note(args.get('navigate_steps'))}")
    if refused is not None:
        return refused
    island_publish("secrets", title="Injecting", app=app, detail=f"{name} → {app}", step="paste")
    out = _run_inject(item, app, args.get("navigate_steps"))
    if out.get("ok"):
        out["label"] = item.get("label") or ""
        out["id"] = item.get("catalog_id") or args.get("id")
        island_publish("done", title="Injected", app=app, detail=f"{name} → {app}")
    return out


def _secrets_provide(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action not in {"clipboard", "inject"}:
        return {"ok": False, "error": "action must be clipboard or inject"}
    item, err = _resolve_secret_args(args)
    if err or not item:
        return {"ok": False, "error": err or "not found"}
    name = _display_name(item)
    if action == "clipboard":
        seconds = float(args.get("seconds") or 30.0)
        refused = GATE.check("secrets_provide", args, f"Copy {name} to the clipboard for {seconds:.0f}s?")
        if refused is not None:
            return refused
        secret = fetch_value(item)
        try:
            set_clipboard(secret)
            island_publish("secrets", title="Clipboard", detail=f"{name} · {seconds:.0f}s")
            time.sleep(max(0.0, seconds))
        finally:
            secret = ""
            clear_clipboard()
        island_publish("done", title="Cleared", detail="clipboard cleared")
        return {"ok": True, "action": "clipboard", "display": name, "seconds": seconds, "cleared": True}
    app = str(args.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app required for inject"}
    refused = GATE.check("secrets_provide", args, f"Fill {name} into {app}?{_nav_note(args.get('navigate_steps'))}")
    if refused is not None:
        return refused
    out = _run_inject(item, app, args.get("navigate_steps"))
    out["action"] = "inject"
    return out


def _secrets_put(args: dict[str, Any]) -> dict[str, Any]:
    return capture_credential(args)


def _workflow_tools() -> list[dict[str, Any]]:
    return [
        {"type": "function", "name": "workflow_read",
         "description": "Read Samantha workflow trigger index (no slug), authoring procedure (slug authoring), or one matched workflow reference. Read before use or repair.",
         "parameters": {"type": "object", "properties": {"slug": {"type": "string"}}, "additionalProperties": False}},
        {"type": "function", "name": "workflow_save",
         "description": "Create/update one workflow reference and its trigger. Repair the faulty owner step immediately before retry; expected_revision comes from workflow_read, empty for create. No credentials in content.",
         "parameters": {"type": "object", "properties": {key: {"type": "string"} for key in
             ("slug", "title", "trigger", "content", "expected_revision")},
             "required": ["slug", "title", "trigger", "content", "expected_revision"], "additionalProperties": False}},
    ]


def _system_volume(args: dict[str, Any]) -> dict[str, Any]:
    direction = args.get("direction")
    if direction not in {"up", "down", "read"}:
        return {"ok": False, "error": "Use up, down or read."}
    result = get_backend().volume(direction)
    if direction != "read":
        island_publish("done" if result.get("ok") else "error",
                       title="Volume confirmed" if result.get("ok") else str(result.get("error_type") or "volume_unverified"))
    return result
