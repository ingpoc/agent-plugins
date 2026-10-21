"""Island publish/confirm — local bus in gateway, HTTP remote in realtime."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import re

from voice_cua.island_state import ISLAND, IslandKind

DEFAULT_GATEWAY = "http://127.0.0.1:8765"
# Apps Samantha has used this session, oldest → newest (island shows newest nearest the pill).
_ACTIVE_APPS: list[str] = []


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    host = host.removeprefix("www.")
    if len(host) > 20:  # keep the registrable part whole rather than ellipsize it
        host = ".".join(host.split(".")[-2:])
    return host


def _comet_title(name: str, args: dict) -> tuple[str, str]:
    """Verb + target for a Comet call; detail is expanded-only."""
    if name == "comet_begin":
        return f"Opening {_host(args.get('url')) or 'website'}", "Comet"
    if name == "comet_end":
        return "Closing Comet", ""
    actions = args.get("actions") if isinstance(args.get("actions"), list) else []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    kind = str(first.get("type") or "")
    if kind == "goto":
        return f"Opening {_host(first.get('url')) or 'page'}", "Comet"
    if kind.startswith("click"):
        text = _short(str(first.get("text") or ""), 18)
        return (f"Clicking {text}" if text else "Clicking"), "Comet"
    if kind in {"fill_selector", "cursor_type"}:
        return "Typing in Comet", ""
    if kind in {"page_context", "text", "snapshot", "locator", "screenshot"}:
        return "Reading page", "Comet"
    if kind.startswith("wait"):
        return "Waiting for page", "Comet"
    return "Using Comet", ""


def describe_tool(name: str, args: dict | None) -> tuple[str, str, str, str]:
    """Pure: map a tool call to island (kind, title, detail, app).

    ``title`` follows the copy contract (verb + target, ≤ 28); detail is
    expanded-only context.
    """
    args = args or {}
    app = str(args.get("app") or "").strip()
    step = str(args.get("step") or args.get("step_label") or "").strip()
    if name == "cua_state":
        return ("acting", f"Reading {app}" if app else "Reading screen", "", app)
    if name == "cua_act":
        op = str(args.get("op") or "").strip()
        verb = {"open": "Opening", "focus": "Opening", "scroll": "Scrolling", "drag": "Dragging",
                "select_text": "Selecting"}.get(op, "Using")
        title = f"{verb} {app}" if app else (step or "Working")
        return ("acting", title, _short(step or op, 60), app)
    if name.startswith("comet_"):
        title, detail = _comet_title(name, args)
        return ("acting", title, detail, "Comet")
    if name.startswith("secrets_"):
        return ("secrets", "Using Keychain", _short(name[len("secrets_"):], 60), "")
    return ("acting", "Working", _short(name.replace("_", " "), 60), app)


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def summarize_result(name: str, result: object, *, fallback: str = "") -> tuple[str, str, str]:
    """Pure: map a tool result to island (kind, title, detail).

    ``fallback`` (usually the action detail, e.g. "open Calculator") is shown
    when an ok result carries no summary/message of its own.
    """
    if not isinstance(result, dict):
        return ("error", "Failed", "no result")
    if result.get("ok"):
        detail = _first_non_empty(
            str(result.get("summary") or ""),
            str(result.get("message") or ""),
            fallback,
            f"{name.replace('_', ' ')} ok",
        )
        return ("done", "Done", _short(detail, 60))
    first_line = str(result.get("error") or result.get("error_type") or "error").splitlines()
    return ("error", "Failed", _short(first_line[0] if first_line else "error", 60))


def use_remote() -> bool:
    return os.environ.get("VOICE_CUA_REMOTE_ISLAND", "").strip().lower() in {"1", "true", "yes"}


def _gateway_url() -> str:
    return os.environ.get("VOICE_CUA_GATEWAY", DEFAULT_GATEWAY).rstrip("/")


def _post(path: str, body: dict, *, timeout: float) -> dict | None:
    req = urllib.request.Request(
        f"{_gateway_url()}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def island_voice_meter(
    side: str,
    level: float,
    levels: list[float] | None = None,
    *,
    min_interval: float = 0.045,
) -> None:
    """Push live waveform bars to island SSE (throttled)."""
    import time

    key = "_meter_ts"
    now = time.monotonic()
    last = getattr(island_voice_meter, key, 0.0)
    if now - last < min_interval and side == getattr(island_voice_meter, "_meter_side", ""):
        return
    setattr(island_voice_meter, key, now)
    setattr(island_voice_meter, "_meter_side", side)
    if use_remote():
        # Never block the caller (mic sender thread) on gateway IPC.
        import threading

        threading.Thread(
            target=_post,
            args=("/api/island/voice", {"voice_side": side, "voice_level": level, "voice_levels": levels or []}),
            kwargs={"timeout": 1.5},
            name="island-meter",
            daemon=True,
        ).start()
        return
    ISLAND.set_voice_meter(side, level, levels)


def island_publish(
    kind: IslandKind,
    *,
    title: str = "",
    detail: str = "",
    app: str = "",
    step: str = "",
    confirm_id: str = "",
    confirm_prompt: str = "",
    active_apps: list[str] | None = None,
    expanded: bool = False,
) -> None:
    if kind == "idle":
        _ACTIVE_APPS.clear()
    elif app.strip():
        name = app.strip()
        if name in _ACTIVE_APPS:
            _ACTIVE_APPS.remove(name)
        _ACTIVE_APPS.append(name)  # most recent last
    apps = active_apps if active_apps is not None else list(_ACTIVE_APPS)
    body = {
        "kind": kind,
        "title": title,
        "detail": detail,
        "app": app,
        "step": step,
        "confirm_id": confirm_id,
        "confirm_prompt": confirm_prompt,
        "active_apps": apps,
        "expanded": bool(expanded),
    }
    if use_remote():
        _post("/api/island/publish", body, timeout=3)
        return
    ISLAND.publish(
        kind,
        title=title,
        detail=detail,
        app=app,
        step=step,
        confirm_id=confirm_id,
        confirm_prompt=confirm_prompt,
        active_apps=apps,
        expanded=expanded,
    )


def island_confirm(confirm_id: str, prompt: str, *, timeout: float = 120.0) -> bool:
    if use_remote():
        data = _post(
            "/api/confirm/request",
            {"confirm_id": confirm_id, "prompt": prompt, "timeout": timeout},
            timeout=timeout + 5,
        )
        return bool(data and data.get("approved"))
    return ISLAND.request_confirm(confirm_id, prompt, timeout=timeout)
