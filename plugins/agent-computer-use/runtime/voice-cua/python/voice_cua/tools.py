"""Realtime-facing tools — CUA + confirm + secrets. No named-app helpers."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from voice_cua.catalog import (
    keychain_put,
    load_catalog,
    upsert_key,
)
from voice_cua.cua_bridge import get_backend, get_tool_schemas, redact_for_model
from voice_cua.inject import clipboard_paste_secret, clear_clipboard, set_clipboard
from voice_cua.labels_tracker import labels_tracker_path, refresh_labels_tracker
from voice_cua.keychain_access import describe_ref, enrich_items, fetch_value, list_keychain, resolve_ref
from voice_cua.activity_log import log_event, log_exception, log_tool_result, log_tool_start
from voice_cua.island_facade import island_confirm, island_publish

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


def _island_act_error(app: str, err: str) -> tuple[str, str]:
    """Short pill copy — never dump RPC traces in the menubar island."""
    low = (err or "").lower()
    if "app not found" in low:
        return "Can't open app", app[:40] or "App"
    if "no window" in low:
        return "No window", app[:40] or "App"
    if "label not found" in low:
        return "Control not found", app[:40] or "App"
    if "user denied" in low:
        return "Denied", app[:40] or "Action"
    if "verification" in low or "unverified" in low:
        return "Not verified", app[:40] or "Action"
    return "Miss", app[:40] or "Action"


def tool_definitions() -> list[dict[str, Any]]:
    cua = get_tool_schemas()
    act_parameters = {
        **cua["act"]["inputSchema"],
        "properties": {
            **cua["act"]["inputSchema"]["properties"],
            "step_label": {
                "type": "string",
                "description": "Short human step for island UI",
            },
            "risky": {
                "type": "boolean",
                "description": "Force confirm_risky before acting",
            },
        },
    }
    return [
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
            "description": cua["act"]["description"],
            "parameters": act_parameters,
        },
        {
            "type": "function",
            "name": "confirm_risky",
            "description": "Ask user to confirm irreversible UI or Keychain write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                },
                "required": ["prompt"],
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
                "Store/update Keychain item + catalog row. Requires confirm. "
                "Pass value only when user just dictated it; prefer interactive CLI otherwise."
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
                    "value": {"type": "string"},
                },
                "required": ["id", "label", "service", "account"],
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


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "cua_state": _cua_state,
        "cua_act": _cua_act,
        "confirm_risky": _confirm_risky,
        "secrets_list": _secrets_list,
        "secrets_label": _secrets_label,
        "secrets_get": _secrets_get,
        "secrets_put": _secrets_put,
        "secrets_inject": _secrets_inject,
        "secrets_provide": _secrets_provide,
    }
    fn = handlers.get(name)
    if not fn:
        log_event("tool_error", tool=name, ok=False, error=f"unknown tool {name}")
        return {"ok": False, "error": f"unknown tool {name}"}
    args = arguments or {}
    app = str(args.get("app") or "")
    log_tool_start(name, args)
    try:
        result = fn(args)
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
    if isinstance(steps, list):
        for s in steps:
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
    if _looks_risky(args):
        cid = str(uuid.uuid4())
        approved = island_confirm(cid, f"Allow action in {app}: {step}?")
        if not approved:
            island_publish("error", title="Denied", app=app, detail=step)
            return {"ok": False, "error": "user denied", "confirmed": False}
    island_publish("driving", title="Driving", app=app, step=step, detail=f"{app} · {step}")
    backend = get_backend()
    result = backend.act(app, args)
    out = redact_for_model(result if isinstance(result, dict) else {"ok": False})
    if out.get("ok"):
        island_publish("done", title="Done", app=app, step=step, detail=f"{app} · {step}")
    else:
        err = str(out.get("error") or "")
        title, detail = _island_act_error(app, err)
        island_publish("error", title=title, app=app, step=step, detail=detail)
        _schedule_listening_reset()
    return out


def _confirm_risky(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "Confirm?")
    cid = str(uuid.uuid4())
    approved = island_confirm(cid, prompt)
    island_publish(
        "done" if approved else "error",
        title="Confirmed" if approved else "Denied",
        detail=prompt[:120],
    )
    return {"ok": True, "approved": approved}


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
        island_publish("error", title="Secret", detail=(err or "miss")[:40])
        return {"ok": False, "error": err or f"Keychain miss for {label}"}
    island_publish("secrets", title="Secret", detail=_display_name(item))
    return describe_ref(item)


def _secrets_get(args: dict[str, Any]) -> dict[str, Any]:
    item, err = _resolve_secret_args(args)
    if err or not item:
        island_publish("error", title="Secret", detail=(err or "miss")[:120])
        return {"ok": False, "error": err or "not found"}
    name = _display_name(item)
    island_publish("secrets", title="Secret", detail=f"{name} ready")
    return describe_ref(item)


def _run_inject(item: dict[str, str], app: str, navigate: Any) -> dict[str, Any]:
    name = _display_name(item)
    secret = fetch_value(item)
    backend = get_backend()
    if isinstance(navigate, list) and navigate:
        nav = backend.act(app, {"app": app, "steps": navigate})
        if not isinstance(nav, dict) or nav.get("ok") is not True:
            clear_clipboard()
            return {"ok": False, "error": "navigate failed", "display": name}
    try:
        clipboard_paste_secret(app, secret, backend=backend)
    finally:
        secret = ""
        clear_clipboard()
        time.sleep(0.05)
        clear_clipboard()
    return {"ok": True, "display": name, "app": app, "injected": True}


def _secrets_inject(args: dict[str, Any]) -> dict[str, Any]:
    app = str(args.get("app") or "").strip()
    if not app:
        return {"ok": False, "error": "app required"}
    item, err = _resolve_secret_args(args)
    if err or not item:
        return {"ok": False, "error": err or "not found"}
    name = _display_name(item)
    cid = str(uuid.uuid4())
    if not island_confirm(cid, f"Inject {name} into {app} via clipboard paste?"):
        return {"ok": False, "error": "user denied", "confirmed": False}
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
    cid = str(uuid.uuid4())
    if action == "clipboard":
        seconds = float(args.get("seconds") or 30.0)
        if not island_confirm(cid, f"Copy {name} to clipboard for {seconds:.0f}s?"):
            return {"ok": False, "error": "user denied", "confirmed": False}
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
    if not island_confirm(cid, f"Provide {name} to {app}?"):
        return {"ok": False, "error": "user denied", "confirmed": False}
    out = _run_inject(item, app, args.get("navigate_steps"))
    out["action"] = "inject"
    return out


def _secrets_put(args: dict[str, Any]) -> dict[str, Any]:
    cid = str(uuid.uuid4())
    prompt = f"Store Keychain secret id={args.get('id')} label={args.get('label')}?"
    if not island_confirm(cid, prompt):
        return {"ok": False, "error": "user denied", "confirmed": False}
    cat = load_catalog()
    row = upsert_key(cat, args)
    value = args.get("value")
    if value is not None and str(value) == "":
        value = None
    keychain_put(
        service=row["service"],
        account=row["account"],
        label=row["label"],
        value=str(value) if value is not None else None,
    )
    island_publish("secrets", title="Stored", detail=f"label={row['label']}")
    return {"ok": True, "id": row["id"], "label": row["label"], "stored": True}
