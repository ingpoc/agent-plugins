#!/usr/bin/env python3
"""Press Success/Failure in a Razorpay Bank popup that lives in ANOTHER Comet window.

The Buyer lease is tab-scoped: its page_context, locators, focus_tab and its
Browser Use bridge (BU_CDP_WS) only see the leased tab, never the popup that
Razorpay opens with window.open(). This helper binds the popup by Comet window
identity (exact window name + active-tab URL prefix, Baroda/mock excluded) and
presses the button through the first surface that actually works:

  ax              AXPress the AXButton in that AX window (needs a healthy Comet
                  AX tree; fails closed with comet_ax_degenerate otherwise).
  applescript-js  Comet AppleScript `execute javascript` on that exact tab
                  (works with the screen locked / display asleep / AX empty;
                  needs Comet > View > Developer > Allow JavaScript from Apple
                  Events, which preflight checks read-only).
  auto            preflight decides: ax when unlocked and Comet AX is healthy,
                  else applescript-js. An ax miss before any press falls back.

Never prints full URLs (query strings carry key ids) or lease tokens. Never
touches the Buyer lease; parent proof stays with the lease owner
(page_context / locator on the same lease: iframe text leaves "Sending OTP").

Usage (runtime root ~/.agents/plugins/comet-control):
  python3 skills/comet-control/scripts/razorpay_bank_popup.py preflight
  python3 skills/comet-control/scripts/razorpay_bank_popup.py probe
  python3 skills/comet-control/scripts/razorpay_bank_popup.py press --action success
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit

DEFAULT_TITLE = "Razorpay Bank"
DEFAULT_EXCLUDES = ("Baroda", "Mock Bank")
DEFAULT_URL_PREFIXES = ("https://api.razorpay.com/v1/gateway/",)
LABELS = {"success": "Success", "failure": "Failure"}
APP = os.environ.get("COMET_APP_NAME", "Comet")

# ---------------------------------------------------------------- AppleScript

_JXA_LIST = r"""
function run(){
  var app = Application(%s);
  var out = [];
  app.windows().forEach(function(w){
    var t = null; try { t = w.activeTab(); } catch(e) {}
    out.push({id: String(w.id()), name: String(w.name()),
              url: t ? String(t.url()) : "", tabs: w.tabs().length});
  });
  return JSON.stringify(out);
}
"""

_JXA_EXEC = r"""
function run(){
  var env = $.NSProcessInfo.processInfo.environment;
  var wid = env.objectForKey('RZP_WINDOW_ID').js;
  var code = env.objectForKey('RZP_JS').js;
  var app = Application(%s);
  var w = app.windows.byId(Number(wid));
  var r = w.activeTab().execute({javascript: code});
  return (r === undefined || r === null) ? "null" : String(r);
}
"""

_JS_PROBE = r"""
(function(){
  var label = %s;
  var nodes = Array.prototype.slice.call(document.querySelectorAll(
    'button,input[type=submit],input[type=button],a[role=button],[role=button]'));
  function txt(n){ return ((n.innerText||n.value||n.textContent||'')+'').trim(); }
  function vis(n){ var r=n.getBoundingClientRect(); return r.width>0 && r.height>0; }
  var names = nodes.filter(vis).map(txt).filter(Boolean).slice(0,12);
  var hits = nodes.filter(function(n){ return vis(n) && txt(n).toLowerCase() === label.toLowerCase(); });
  return JSON.stringify({title: document.title, ready: document.readyState,
    visibility: document.visibilityState, buttons: names, matches: hits.length,
    otp_text: /sending otp/i.test(document.body ? document.body.innerText : '')});
})()
"""

_JS_PRESS = r"""
(function(){
  var label = %s;
  var nodes = Array.prototype.slice.call(document.querySelectorAll(
    'button,input[type=submit],input[type=button],a[role=button],[role=button]'));
  function txt(n){ return ((n.innerText||n.value||n.textContent||'')+'').trim(); }
  function vis(n){ var r=n.getBoundingClientRect(); return r.width>0 && r.height>0; }
  var hits = nodes.filter(function(n){ return vis(n) && txt(n).toLowerCase() === label.toLowerCase(); });
  if (hits.length !== 1) return JSON.stringify({pressed:false, matches:hits.length});
  hits[0].click();
  return JSON.stringify({pressed:true, matches:1, tag:hits[0].tagName});
})()
"""


def _osa_jxa(script: str, env: dict[str, str] | None = None, timeout: float = 20) -> str:
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:400])
    return proc.stdout.strip()


def _safe_url(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme else url[:60]
    except Exception:
        return url[:60]


def comet_windows() -> list[dict[str, Any]]:
    return json.loads(_osa_jxa(_JXA_LIST % json.dumps(APP)))


def exec_js(window_id: str, code: str) -> Any:
    raw = _osa_jxa(_JXA_EXEC % json.dumps(APP), env={"RZP_WINDOW_ID": str(window_id), "RZP_JS": code})
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def bind(title: str, excludes: list[str], prefixes: list[str]) -> tuple[list[dict], list[dict]]:
    wins = comet_windows()
    hits, skipped = [], []
    for w in wins:
        name = w["name"]
        rec = {"window_id": w["id"], "name": name, "url": _safe_url(w["url"]), "tabs": w["tabs"]}
        if name != title and not name.startswith(title + " \u2014"):
            continue
        if any(x.lower() in name.lower() for x in excludes):
            skipped.append({**rec, "why": "excluded_title"})
            continue
        if prefixes and not any(w["url"].startswith(p) for p in prefixes):
            skipped.append({**rec, "why": "url_prefix_mismatch"})
            continue
        # Test mode only: a razorpay.com popup must carry a rzp_test_ key id.
        host = urlsplit(w["url"]).netloc.lower()
        if host.endswith("razorpay.com") and "key_id=rzp_test_" not in w["url"]:
            skipped.append({**rec, "why": "not_test_mode"})
            continue
        hits.append(rec)
    return hits, skipped


# ------------------------------------------------------------------- AX path

def ax_health(pid: int | None) -> dict[str, Any]:
    try:
        from ApplicationServices import (AXIsProcessTrusted, AXUIElementCreateApplication,
                                         AXUIElementCopyAttributeValue)
    except Exception as exc:  # pragma: no cover - pyobjc missing
        return {"trusted": False, "error": f"pyobjc: {exc}"}
    out: dict[str, Any] = {"trusted": bool(AXIsProcessTrusted())}
    if not pid:
        return out
    app = AXUIElementCreateApplication(pid)
    _, wins = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    roles, titles = [], []
    for w in list(wins or [])[:12]:
        _, r = AXUIElementCopyAttributeValue(w, "AXRole", None)
        _, t = AXUIElementCopyAttributeValue(w, "AXTitle", None)
        roles.append(str(r)); titles.append(str(t))
    out.update(window_count=len(roles), window_roles=sorted(set(roles)))
    # Degenerate Chromium AX: AXWindows returns the app element itself.
    out["degenerate"] = (not roles) or all(r != "AXWindow" for r in roles)
    out["titles"] = titles[:12]
    return out


def ax_press(pid: int, title: str, label: str) -> dict[str, Any]:
    from ApplicationServices import (AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                                     AXUIElementPerformAction)
    app = AXUIElementCreateApplication(pid)
    _, wins = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    target = None
    for w in wins or []:
        _, r = AXUIElementCopyAttributeValue(w, "AXRole", None)
        _, t = AXUIElementCopyAttributeValue(w, "AXTitle", None)
        if str(r) == "AXWindow" and str(t) == title:
            target = w
            break
    if target is None:
        return {"pressed": False, "error": "ax_window_not_found"}
    stack, seen, hits = [target], 0, []
    while stack and seen < 5000:
        node = stack.pop(); seen += 1
        _, role = AXUIElementCopyAttributeValue(node, "AXRole", None)
        if str(role) == "AXButton":
            _, t = AXUIElementCopyAttributeValue(node, "AXTitle", None)
            _, d = AXUIElementCopyAttributeValue(node, "AXDescription", None)
            if label.lower() in (str(t or "").strip().lower(), str(d or "").strip().lower()):
                hits.append(node)
        _, kids = AXUIElementCopyAttributeValue(node, "AXChildren", None)
        stack.extend(list(kids or []))
    if len(hits) != 1:
        return {"pressed": False, "error": "ax_button_matches", "matches": len(hits), "walked": seen}
    AXUIElementPerformAction(target, "AXRaise")
    err = AXUIElementPerformAction(hits[0], "AXPress")
    return {"pressed": err == 0, "ax_err": int(err), "walked": seen}


# ------------------------------------------------------------------ preflight

def comet_pid() -> int | None:
    proc = subprocess.run(["pgrep", "-x", APP], capture_output=True, text=True)
    pids = [int(x) for x in proc.stdout.split()]
    return min(pids) if pids else None


def preflight(title: str, js_window_id: str | None = None) -> dict[str, Any]:
    """Read-only. Executes JS only in js_window_id (the bound popup), never in
    other Comet windows such as the Buyer lease tab."""
    out: dict[str, Any] = {}
    try:
        import Quartz
        sess = Quartz.CGSessionCopyCurrentDictionary() or {}
        out["screen_locked"] = bool(sess.get("CGSSessionScreenIsLocked", False))
        _, _, n_active = Quartz.CGGetActiveDisplayList(8, None, None)
        out["active_displays"] = int(n_active)
        out["screen_capture_access"] = bool(Quartz.CGPreflightScreenCaptureAccess())
        ghosts = []
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID):
            if w.get("kCGWindowOwnerName") != APP or w.get("kCGWindowLayer", 0) != 0:
                continue
            name = str(w.get("kCGWindowName") or "")
            if title.lower() in name.lower() or "razorpay" in name.lower():
                ghosts.append({"cg_id": int(w["kCGWindowNumber"]), "name": name,
                               "onscreen": bool(w.get("kCGWindowIsOnscreen"))})
        out["cg_windows"] = ghosts
    except Exception as exc:
        out["quartz_error"] = str(exc)[:200]
    pid = comet_pid()
    out["comet_pid"] = pid
    out["ax"] = ax_health(pid)
    try:
        wins = comet_windows()
        out["applescript_windows"] = len(wins)
        out["applescript_js"] = None  # unchecked: probe/press test it on the popup only
        if js_window_id:
            try:
                exec_js(js_window_id, "'1'")
                out["applescript_js"] = True
            except RuntimeError as exc:
                out["applescript_js"] = False
                out["applescript_js_error"] = str(exc)[:200]
    except Exception as exc:
        out["applescript_error"] = str(exc)[:200]
    blocked_pixels = out.get("screen_locked") or out.get("active_displays") == 0
    if not (out["ax"].get("degenerate") or blocked_pixels):
        out["recommended_method"] = "ax"
    elif out.get("applescript_js") is not False:
        out["recommended_method"] = "applescript-js"
    else:
        out["recommended_method"] = "none"
    out["notes"] = []
    if blocked_pixels:
        out["notes"].append("screen locked / displays asleep: captures black, CG onscreen unreliable, no HID/cua pixel clicks")
    if out["ax"].get("degenerate"):
        out["notes"].append("Comet AX degenerate (AXWindows -> AXApplication): AXPress/cua_slice cannot see web buttons")
    return out


# ----------------------------------------------------------------------- main

def _probe_all(hits: list[dict], label: str) -> list[dict]:
    out = []
    for h in hits:
        rec = dict(h)
        try:
            rec["page"] = exec_js(h["window_id"], _JS_PROBE % json.dumps(label))
        except RuntimeError as exc:
            rec["page_error"] = str(exc)[:200]
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["preflight", "probe", "press"])
    ap.add_argument("--action", choices=sorted(LABELS), default="success")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="exact Comet window name (default: Razorpay Bank)")
    ap.add_argument("--exclude", action="append", help="title substrings to reject (default: Baroda, Mock Bank)")
    ap.add_argument("--url-prefix", action="append", help="allowed active-tab URL prefix (default: Razorpay gateway)")
    ap.add_argument("--window-id", help="Comet AppleScript window id from probe (disambiguates)")
    ap.add_argument("--pick", choices=["only", "newest"], default="only",
                    help="several actionable windows: fail (only) or take the highest window id (newest)")
    ap.add_argument("--method", choices=["auto", "ax", "applescript-js"], default="auto")
    ap.add_argument("--verify-seconds", type=float, default=8.0)
    a = ap.parse_args()
    started = time.time()
    label = LABELS[a.action]
    excludes = a.exclude or list(DEFAULT_EXCLUDES)
    prefixes = a.url_prefix or list(DEFAULT_URL_PREFIXES)

    def emit(payload: dict[str, Any], code: int) -> int:
        payload["duration_ms"] = int((time.time() - started) * 1000)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return code

    if a.command == "preflight":
        pf = preflight(a.title)
        hits, skipped = bind(a.title, excludes, prefixes)
        return emit({"ok": True, "preflight": pf, "bound": hits, "skipped": skipped}, 0)

    hits, skipped = bind(a.title, excludes, prefixes)
    if a.window_id:
        hits = [h for h in hits if h["window_id"] == str(a.window_id)]
    if not hits:
        return emit({"ok": False, "error": "no_bound_window", "title": a.title, "skipped": skipped}, 2)
    probed = _probe_all(hits, label)
    if a.command == "probe":
        return emit({"ok": True, "bound": probed, "skipped": skipped}, 0)

    actionable = [p for p in probed if isinstance(p.get("page"), dict) and p["page"].get("matches") == 1]
    if not actionable:
        return emit({"ok": False, "error": "button_not_found", "label": label, "bound": probed}, 3)
    if len(actionable) > 1:
        if a.pick != "newest":
            return emit({"ok": False, "error": "ambiguous_windows", "bound": actionable,
                         "hint": "rerun with --window-id <id> or --pick newest"}, 4)
        actionable.sort(key=lambda p: int(p["window_id"]), reverse=True)
    target = actionable[0]

    pf = preflight(a.title, js_window_id=target["window_id"])
    method = a.method if a.method != "auto" else pf.get("recommended_method", "none")
    attempts: list[dict[str, Any]] = []
    pressed = False
    if method == "ax":
        res = ax_press(pf["comet_pid"], target["name"], label) if pf.get("comet_pid") else {"pressed": False, "error": "no_pid"}
        attempts.append({"method": "ax", **res})
        pressed = bool(res.get("pressed"))
        if not pressed and a.method == "auto" and pf.get("applescript_js"):
            method = "applescript-js"
    if not pressed and method == "applescript-js":
        try:
            res = exec_js(target["window_id"], _JS_PRESS % json.dumps(label))
        except RuntimeError as exc:
            res = {"pressed": False, "error": str(exc)[:200]}
        res = res if isinstance(res, dict) else {"pressed": False, "raw": str(res)[:200]}
        attempts.append({"method": "applescript-js", **res})
        pressed = bool(res.get("pressed"))
    if not pressed:
        return emit({"ok": False, "error": "press_failed", "target": target, "attempts": attempts,
                     "preflight": {k: pf.get(k) for k in ("screen_locked", "active_displays", "recommended_method")}}, 5)

    popup_state, after = "unchanged", None
    deadline = time.time() + a.verify_seconds
    while time.time() < deadline:
        time.sleep(0.5)
        ids = {w["id"] for w in comet_windows()}
        if target["window_id"] not in ids:
            popup_state = "closed"
            break
        try:
            after = exec_js(target["window_id"], _JS_PROBE % json.dumps(label))
        except RuntimeError:
            continue
        if isinstance(after, dict) and (after.get("matches") != 1 or after.get("title") != target["page"].get("title")):
            popup_state = "advanced"
            break
    return emit({"ok": popup_state != "unchanged", "action": a.action, "method": attempts[-1]["method"],
                 "target": {k: target[k] for k in ("window_id", "name", "url")},
                 "attempts": attempts, "popup_state": popup_state, "popup_after": after,
                 "parent_proof": "lease owner: same-lease page_context/locator; iframe text must leave 'Sending OTP'"},
                0 if popup_state != "unchanged" else 6)


if __name__ == "__main__":
    raise SystemExit(main())
