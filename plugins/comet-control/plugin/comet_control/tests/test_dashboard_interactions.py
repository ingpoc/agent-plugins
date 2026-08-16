#!/usr/bin/env python3
"""Live regression suite for comet-control click/interaction reliability.

END-TO-END tests. They drive the real browser through the Comet Control Bridge
and assert real outcomes (URL / DOM state / cursor position), never screenshots.
This is the suite that guards the three trust-breaking defects fixed on
2026-05-28 — see references/optimize.md "Click-reliability invariants".

Prerequisites:
  1. Broker running and extension loaded from plugin/comet_control/extension (Comet open,
     extension loaded)
  2. A compatible dashboard reachable at COMET_CONTROL_TEST_URL (default
     http://localhost:9876 — the Autonomous Agent Builder dashboard, whose nav
     has Board / Metrics / Memory / Settings and a memory-search input).

Exit codes:
  0  all assertions passed
  1  one or more assertions FAILED (a real regression)
  2  environment unavailable or incompatible dashboard — suite SKIPPED

Every browser command runs in one isolated leased window. The lease is closed
on success, failure, skip, or interruption.

Run:  python3 plugin/comet_control/tests/test_dashboard_interactions.py
"""

from __future__ import annotations

import os
import socket
import time
import uuid

from test_multi_agent_isolation import (
    SOCK,
    Session,
    TestFailure,
    bridge,
    close_session,
    preflight,
    session_inventory,
)


BASE = os.environ.get("COMET_CONTROL_TEST_URL", "http://localhost:9876").rstrip("/")
SESSION: Session | None = None


class SkipSuite(RuntimeError):
    """The optional external dashboard or bridge runtime is unavailable."""


# ── Bridge harness ─────────────────────────────────────────────────────────────

def seq(actions, timeout=30):
    if SESSION is None:
        raise TestFailure("dashboard command attempted without an active lease")
    return bridge(
        {
            "type": "run",
            "sessionId": SESSION.session_id,
            "leaseToken": SESSION.lease_token,
            "timeoutSeconds": timeout,
            "actions": actions,
        },
        timeout=timeout + 5,
    )


def url():
    return ((seq([{"type": "page_context"}]).get("results") or [{}])[0]).get("url", "")


def ev(expr):
    return (seq([{"type": "evaluate", "expression": expr}]).get("results") or [{}])[0].get("result")


def goto(path):
    seq([{"type": "goto", "url": BASE + path}])
    time.sleep(0.9)


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")


# ── Preflight: skip cleanly if env unavailable / dashboard incompatible ─────────

def bridge_or_skip() -> None:
    if not os.path.exists(SOCK):
        raise SkipSuite(
            f"bridge socket not found at {SOCK} — start the broker with Comet open"
        )
    try:
        st = bridge({"type": "status", "timeoutSeconds": 5}, timeout=7)
    except (OSError, socket.timeout) as exc:
        raise SkipSuite(f"bridge not reachable ({exc})") from exc
    if not st.get("success"):
        raise SkipSuite(f"bridge status failed ({st.get('error')})")


def dashboard_or_skip() -> None:
    goto("/memory")
    pc = (seq([{"type": "page_context"}]).get("results") or [{}])[0]
    nav = {n.get("text", "").strip() for n in pc.get("nav", [])}
    needed = {"Board", "Metrics", "Memory", "Settings"}
    if not needed.issubset(nav):
        raise SkipSuite(
            f"dashboard at {BASE} is unavailable or incompatible "
            f"(nav missing {needed - nav}); set COMET_CONTROL_TEST_URL"
        )


# ── Round 1 — click resolution + negative cases ────────────────────────────────
# Guards: findPointByText/findPointBySelector must pick the EXACT, VISIBLE element
# (not a hidden skip-link or 0×0 duplicate), and miss cleanly when nothing matches.

def round1():
    print("=== ROUND 1: click resolution + negatives ===")

    # T1: 'Board' is a substring of the hidden skip-link 'Skip to dashboard'.
    goto("/memory")
    r = seq([{"type": "click_text", "text": "Board"}, {"type": "wait", "ms": 800}])
    pt = (r.get("results") or [{}])[0].get("point", {})
    check("T1 click_text 'Board' → /board (not skip-link)", url().endswith("/board"),
          f"matched={pt.get('text')!r}")

    # T2-T5: nav resolution, incl. lowercase + multi-word.
    for text, suffix in [("Metrics", "/metrics"), ("knowledge", "/knowledge"),
                         ("Observability", "/observability"), ("Backlog", "/backlog")]:
        goto("/memory")
        seq([{"type": "click_text", "text": text}, {"type": "wait", "ms": 800}])
        check(f"T click_text {text!r} → {suffix}", url().endswith(suffix), f"url={url()}")

    # T6 NEG: missing text → error, no spurious navigation.
    goto("/memory")
    before = url()
    r = seq([{"type": "click_text", "text": "ZZQ_NoSuchButton_42"}])
    check("T6 NEG missing text → error + no nav", r.get("success") is False and url() == before,
          f"success={r.get('success')}")

    # T7 NEG: text only present in the invisible 1×1 skip-link → must not match it.
    goto("/memory")
    before = url()
    seq([{"type": "click_text", "text": "Skip to dashboard"}])
    check("T7 NEG invisible skip-link → no spurious nav", url() == before, f"url={url()}")

    # T8: click_selector with duplicate matches (visible + 0×0) → visible one.
    goto("/memory")
    seq([{"type": "click_selector", "selector": "a[href$='/settings']"}, {"type": "wait", "ms": 800}])
    check("T8 click_selector dup matches → visible → /settings", url().endswith("/settings"), f"url={url()}")

    # T9: fill then verify value.
    goto("/memory")
    r = seq([{"type": "fill_selector", "selector": "input[name='memory-search']", "value": "decision"},
             {"type": "wait", "ms": 300},
             {"type": "evaluate", "expression": "document.querySelector(\"input[name='memory-search']\").value"}])
    check("T9 fill_selector → value set", (r.get("results") or [{}])[-1].get("result") == "decision")


# ── Round 2 — hard edge cases ───────────────────────────────────────────────────
# Guards: scroll-to-click, occlusion, ambiguous suffix, fill replace/append,
# cursor visibly AT target (motion reliability), ordered batch, SPA continuity.

def round2():
    print("=== ROUND 2: hard edge cases ===")

    # R1: off-screen element must be scrolled to; cursor must land on it.
    goto("/observability")
    low = ev("""(()=>{const els=[...document.querySelectorAll('button,a')].filter(e=>(e.innerText||'').trim());
      let best=null;for(const e of els){const r=e.getBoundingClientRect();const ay=r.top+scrollY;
      if(!best||ay>best.ay)best={ay:Math.round(ay),text:(e.innerText||'').trim().slice(0,30)};}return best;})()""")
    if low and low.get("text"):
        r = seq([{"type": "click_text", "text": low["text"]}, {"type": "wait", "ms": 300}, {"type": "cursor_status"}])
        res = r.get("results")
        if res:
            pt, cs = res[0].get("point", {}), res[-1]
            landed = pt.get("onTarget") and abs(cs.get("y", -999) - pt.get("y", 999)) < 5
            check("R1 off-screen elem: scrolled + cursor landed", bool(landed),
                  f"text={low['text']!r} onTarget={pt.get('onTarget')} cursorY={cs.get('y')} pointY={pt.get('y')}")
        else:
            check("R1 off-screen elem", False, f"click failed: {r.get('error')}")
    else:
        check("R1 off-screen elem", False, "no off-screen element discovered")

    # R2: occlusion — background nav click must NOT navigate through an open modal.
    goto("/memory")
    seq([{"type": "click_text", "text": "Open command palette"}, {"type": "wait", "ms": 500}])
    modal = ev("!!document.querySelector('[role=dialog],[cmdk-root],[aria-modal=\"true\"]')")
    before = url()
    seq([{"type": "click_selector", "selector": "a[href$='/board']"}, {"type": "wait", "ms": 500}])
    blocked = url() == before
    seq([{"type": "cursor_key", "key": "Escape"}])
    time.sleep(0.3)
    check("R2 occlusion: bg nav click blocked while modal open", bool(modal) and blocked,
          f"modal={modal} stayed={blocked}")

    # R3: ambiguous text with count suffix ('Decisions\n0') matched by 'Decisions'.
    goto("/memory")
    r = seq([{"type": "click_text", "text": "Decisions"}, {"type": "wait", "ms": 300}])
    pt = (r.get("results") or [{}])[0].get("point", {})
    check("R3 'Decisions' matches 'Decisions 0' pill", "decision" in pt.get("text", "").lower(),
          f"matched={pt.get('text')!r}")

    # R4: fill replace then append.
    goto("/memory")
    seq([{"type": "fill_selector", "selector": "input[name='memory-search']", "value": "abc"}])
    seq([{"type": "fill_selector", "selector": "input[name='memory-search']", "value": "xyz"}])
    seq([{"type": "fill_selector", "selector": "input[name='memory-search']", "value": " more", "append": True}])
    val = ev("document.querySelector(\"input[name='memory-search']\").value")
    check("R4 fill replace+append → 'xyz more'", val == "xyz more", f"value={val!r}")

    # R5: cursor motion reliability — cursor must end exactly at target, visible.
    goto("/memory")
    r = seq([{"type": "cursor_move", "x": 716, "y": 34}, {"type": "wait", "ms": 400}, {"type": "cursor_status"}])
    cs = (r.get("results") or [{}])[-1]
    near = abs(cs.get("x", -999) - 716) < 5 and abs(cs.get("y", -999) - 34) < 5 and cs.get("visible")
    check("R5 cursor_move → cursor AT (716,34) visible", bool(near),
          f"cursor=({cs.get('x')},{cs.get('y')}) vis={cs.get('visible')}")

    # R6: ordered multi-target in one batch.
    goto("/memory")
    r = seq([{"type": "click_text", "text": "Patterns"}, {"type": "wait", "ms": 250},
             {"type": "click_text", "text": "Corrections"}, {"type": "wait", "ms": 250}])
    res = r.get("results") or [{}, {}, {}]
    p1 = res[0].get("point", {}).get("text", "")
    p2 = res[2].get("point", {}).get("text", "") if len(res) > 2 else ""
    check("R6 ordered batch: Patterns then Corrections",
          "pattern" in p1.lower() and "correction" in p2.lower(), f"first={p1!r} second={p2!r}")

    # R7: SPA nav via click, then immediate interaction on the new page.
    goto("/memory")
    seq([{"type": "click_text", "text": "Settings"}, {"type": "wait", "ms": 900}])
    pc = (seq([{"type": "page_context"}]).get("results") or [{}])[0]
    check("R7 nav→/settings then immediate page_context", pc.get("url", "").endswith("/settings"),
          f"url={url()} ctx_buttons={len(pc.get('buttons', []))}")


# ── Round 3 — navigation robustness ─────────────────────────────────────────────
# Guards two patterns that historically HUNG the playwright/webwright lane but must
# NOT hang comet-control (CDP load events, bounded timeouts — not networkidle).
# Sourced from 30d session analysis 2026-05-28: B4 networkidle-on-SPA timeout,
# B5 net::ERR_CONNECTION_REFUSED. See references/optimize.md.

def round3():
    print("=== ROUND 3: navigation robustness ===")

    # N1 (B4): SSE/long-poll SPA never goes network-idle. goto must settle fast on
    # the CDP load event, not wait for idle. Bound generously (< 15s) but the real
    # signal is "seconds, not the 30s timeout".
    t0 = time.time()
    seq([{"type": "goto", "url": BASE + "/observability"}])
    elapsed = time.time() - t0
    pc = (seq([{"type": "page_context"}]).get("results") or [{}])[0]
    check("N1 SSE-SPA goto settles (not networkidle-hang)",
          elapsed < 15 and pc.get("url", "").endswith("/observability"),
          f"elapsed={elapsed:.1f}s url={pc.get('url')}")

    # N2 (B5): goto to a refused/dead target must NOT hang, must return bounded, and
    # the failed load must be DETECTABLE via page state (page_context.url falsy /
    # nav empty) — so an agent asserting real outcomes catches it.
    t0 = time.time()
    seq([{"type": "goto", "url": "http://127.0.0.1:59999/"}])
    elapsed = time.time() - t0
    time.sleep(0.4)
    pc = (seq([{"type": "page_context"}]).get("results") or [{}])[0]
    failed_load_detectable = not (pc.get("url") or "").startswith("http://127.0.0.1:59999")
    check("N2 dead-target goto bounded + failed load detectable",
          elapsed < 15 and failed_load_detectable,
          f"elapsed={elapsed:.1f}s post_url={pc.get('url')!r}")

    # N3 (B5 recovery): after a dead-target nav the bridge must stay usable — a real
    # nav back to the dashboard must succeed and report the right URL.
    seq([{"type": "goto", "url": BASE + "/memory"}])
    time.sleep(0.6)
    pc = (seq([{"type": "page_context"}]).get("results") or [{}])[0]
    check("N3 bridge recovers after dead-target nav",
          pc.get("url", "").endswith("/memory"), f"url={pc.get('url')}")


def main():
    global SESSION

    print(f"comet-control dashboard regression suite → {BASE}\n")
    session: Session | None = None
    exit_code = 1
    cleanup_failures: list[str] = []

    try:
        bridge_or_skip()
        nonce = uuid.uuid4().hex[:10]
        session = preflight(
            f"comet-control-dashboard-{nonce}",
            f"Dashboard click regression {nonce}",
            f"{BASE}/memory",
        )
        SESSION = session
        dashboard_or_skip()
        round1()
        round2()
        round3()
        passed = sum(1 for _, ok in RESULTS if ok)
        total = len(RESULTS)
        print(f"\n=== SUMMARY: {passed}/{total} PASS ===")
        exit_code = 0 if passed == total else 1
    except SkipSuite as exc:
        print(f"SKIP: {exc}.")
        exit_code = 2
    except (OSError, socket.timeout, TestFailure) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        if session is not None:
            try:
                close_session(session)
            except Exception as exc:  # cleanup failure must override a skip/pass
                cleanup_failures.append(f"closeout: {type(exc).__name__}: {exc}")
            try:
                if session.session_id in session_inventory():
                    cleanup_failures.append(f"lease remains after closeout: {session.session_id}")
            except Exception as exc:
                cleanup_failures.append(f"inventory: {type(exc).__name__}: {exc}")
        SESSION = None

    for failure in cleanup_failures:
        print(f"FAIL: cleanup {failure}")
    return 1 if cleanup_failures else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
