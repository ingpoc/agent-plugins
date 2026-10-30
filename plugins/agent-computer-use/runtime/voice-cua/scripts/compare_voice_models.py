#!/usr/bin/env python3
"""10-run live comparison of GPT-Live-1 task tiers (Bulk/Intelligence) [vs Grok Voice 3.0].

The Realtime arm was removed with the Realtime backend (GPT-Live-1 is the only OpenAI voice path).

PAID: every run opens a real voice session and makes billed model calls. It refuses
to run without --i-have-approval. Default (no flag) prints the plan and exits.

Each arm runs in its own process with a throwaway settings.json + activity log, in
text mode (no mic), sending the same harmless, read-only tasks N times. Per arm:
- speed: wall time per task (send → final reply), median / p90;
- cost: tokens from `usage` rows x price table, plus voice duration for GPT-Live
  (`live_usage.voice_seconds`) and Grok Voice (session wall time x per-minute rate);
- success: reply arrived (ok) AND the expected tool ran (tool_result row, ok=true).

Usage:
  python3 scripts/compare_voice_models.py                      # plan only, no calls
  python3 scripts/compare_voice_models.py --i-have-approval    # 10 runs per arm
  python3 scripts/compare_voice_models.py --i-have-approval --arms gpt-live-1 --runs 10
Prices (USD per 1M tokens / per minute) are from developers.openai.com and x.ai (Oct 2026);
override with --prices prices.json. Keys come from the Keychain inside the worker; never pass them as args.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYDIR = HERE.parent / "python"

TASKS = [  # read-only; expected tool in brackets
    ("Which app is frontmost right now? Check the screen, answer in five words.", "cua_state"),
    ("Is Finder running? Check, then answer yes or no.", "cua_state"),
    ("Look at the frontmost window and tell me its title only.", "cua_state"),
]

PRICES = {
    "tokens": {  # USD per 1M: input, cached input, output (audio rates for realtime when audio tokens present)
        "gpt-6-luna": {"input": 0.1, "cached": 0.01, "output": 0.5},
        "gpt-6-sol": {"input": 2.0, "cached": 0.2, "output": 10.0},
        "gpt-6.1-sol": {"input": 2.0, "cached": 0.1, "output": 10.0},
        "gpt-6-astra": {"input": 10.0, "cached": 1.0, "output": 50.0},
    },
    "per_minute": {"gpt-live-1": 0.05, "grok-voice-latest": 0.08},
}

ARMS = {
    "gpt-live-1": {"realtime_model": "gpt-live-1", "live_bulk_model": "gpt-6-luna",
                   "live_intelligence_model": "gpt-6.1-sol"},
    "grok-voice-latest": {"realtime_model": "grok-voice-latest"},
}


def worker(arm: str, runs: int, out: Path) -> int:
    sys.path.insert(0, str(PYDIR))
    from voice_cua.realtime_session import RealtimeSession

    results = []
    session = RealtimeSession(enable_audio=False)
    import threading

    threading.Thread(target=session.run, daemon=True).start()
    opened = time.monotonic()
    if not session._session_ready.wait(timeout=30.0):
        out.write_text(json.dumps({"arm": arm, "error": "session not ready"}))
        session.shutdown()
        return 1
    for i in range(runs):
        text, tool = TASKS[i % len(TASKS)]
        t0, w0 = time.monotonic(), time.time()
        res = session.send_text_and_wait(text, timeout=120.0)
        results.append({"i": i, "tool": tool, "ok": bool(res.get("ok")), "secs": round(time.monotonic() - t0, 3),
                        "w0": w0, "w1": time.time(), "error": str(res.get("error") or "")[:80]})
    session_secs = time.monotonic() - opened
    session.shutdown()
    time.sleep(2.0)  # let session.closed / final usage land
    out.write_text(json.dumps({"arm": arm, "results": results, "session_secs": session_secs}))
    return 0


def _cost(rows: list[dict], arm: str, session_secs: float, prices: dict) -> dict:
    tok = 0.0
    for r in rows:
        if r.get("kind") != "usage":
            continue
        p = prices["tokens"].get(r.get("model") or arm)
        if not p:
            continue
        cached = r.get("cached_tokens", 0)
        a_in, a_out = r.get("input_audio_tokens", 0), r.get("output_audio_tokens", 0)
        t_in = max(0, r.get("input_tokens", 0) - cached - a_in)
        t_out = max(0, r.get("output_tokens", 0) - a_out)
        tok += (t_in * p["input"] + cached * p["cached"] + t_out * p["output"]
                + a_in * p.get("audio_input", p["input"]) + a_out * p.get("audio_output", p["output"])) / 1e6
    minutes = 0.0
    if arm == "gpt-live-1":
        secs = [r.get("voice_seconds", 0) for r in rows if r.get("kind") == "live_usage"]
        minutes = (max(secs) if secs else session_secs) / 60
    elif arm == "grok-voice-latest":
        minutes = session_secs / 60
    voice = minutes * prices["per_minute"].get(arm, 0.0)
    return {"token_usd": round(tok, 5), "voice_usd": round(voice, 5), "total_usd": round(tok + voice, 5)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--i-have-approval", action="store_true", help="run PAID live calls (owner approved)")
    ap.add_argument("--arms", default="gpt-live-1")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--prices", type=Path)
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        return worker(a.worker, a.runs, a.out)
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    unknown = [x for x in arms if x not in ARMS]
    if unknown:
        ap.error(f"unknown arm(s): {unknown}; choose from {sorted(ARMS)}")
    prices = json.loads(a.prices.read_text()) if a.prices else PRICES
    print(f"plan: {len(arms)} arms x {a.runs} runs, tasks: {[t for t, _ in TASKS]}")
    if not a.i_have_approval:
        print("dry run: no calls made. Re-run with --i-have-approval once the owner approves paid live calls.")
        return 0
    summary = {}
    for arm in arms:
        with tempfile.TemporaryDirectory(prefix=f"vcmp-{arm}-") as td:
            tdp = Path(td)
            settings = {"realtime_voice": "alloy", "eagerness": "balanced", "mic_profile": "near_field", **ARMS[arm]}
            (tdp / "settings.json").write_text(json.dumps(settings))
            env = dict(os.environ, VOICE_CUA_SETTINGS=str(tdp / "settings.json"),
                       VOICE_CUA_ACTIVITY_LOG=str(tdp / "activity.jsonl"), VOICE_CUA_NO_AUDIO="1")
            env.pop("VOICE_CUA_REALTIME_MODEL", None)
            out = tdp / "out.json"
            proc = subprocess.run([sys.executable, __file__, "--worker", arm, "--runs", str(a.runs), "--out", str(out)],
                                  env=env, check=False)
            data = json.loads(out.read_text()) if out.exists() else {"error": "worker crashed"}
            if proc.returncode != 0:
                data = {"arm": arm, "error": f"worker exit {proc.returncode}", **({"partial": data} if data else {})}
            rows = [json.loads(x) for x in (tdp / "activity.jsonl").read_text().splitlines()] \
                if (tdp / "activity.jsonl").exists() else []
        if "results" not in data:
            summary[arm] = data
            continue
        res = data["results"]
        from datetime import datetime

        tool_ok = [(t.get("tool"), datetime.fromisoformat(t["ts"]).timestamp()) for t in rows
                   if t.get("kind") == "tool_result" and t.get("ok") is not False and t.get("ts")]
        success = sum(1 for r in res if r["ok"] and any(
            name == r["tool"] and r["w0"] <= ts <= r["w1"] for name, ts in tool_ok))
        secs = sorted(r["secs"] for r in res)
        summary[arm] = {
            "runs": len(res), "ok_replies": sum(r["ok"] for r in res), "success": success,
            "median_s": round(statistics.median(secs), 3), "p90_s": secs[max(0, int(len(secs) * 0.9) - 1)],
            "tiers": sorted({r.get("tier") for r in rows if r.get("kind") == "usage" and r.get("tier")}),
            **_cost(rows, arm, data["session_secs"], prices),
        }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
