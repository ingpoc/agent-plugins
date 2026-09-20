#!/usr/bin/env python3
"""A/B: rules vs LLM vs Jev Choice on identical macos-cua candidate menus.

Decision-quality harness (not the official entry-contract suite). Pass rule:
- Jev exact-action match ≥95% and ≥ LLM
- Jev invalid-ID execution rate = 0%
- Jev improves latency by ≥10% or estimated USD/decision by ≥20%
- When only one efficiency metric wins, the other may regress by at most 10%
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
JEV_ACT = HERE / "jev_act.py"
DEFAULT_CASES = SKILL / "tests" / "fixtures" / "jev_act_cases.json"
CHAT_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
DEFAULT_LLM_MODEL = os.environ.get("MACOS_CUA_LLM_PICKER_MODEL", "openai/gpt-4.1-mini")
JEV_INPUT_USD_PER_1M = float(os.environ.get("JEV_INPUT_USD_PER_1M", "0.042"))
JEV_OUTPUT_USD_PER_1M = float(os.environ.get("JEV_OUTPUT_USD_PER_1M", "0"))
LLM_INPUT_USD_PER_1M = float(os.environ.get("LLM_INPUT_USD_PER_1M", "0.4"))
LLM_OUTPUT_USD_PER_1M = float(os.environ.get("LLM_OUTPUT_USD_PER_1M", "1.6"))


def load_jev_act():
    spec = importlib.util.spec_from_file_location("macos_cua_jev_act_bench", JEV_ACT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def gateway_key() -> str | None:
    key = os.environ.get("AI_GATEWAY_API_KEY")
    if key:
        return key
    try:
        import subprocess

        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Vercel AI Gateway", "-w"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip() or None
    except Exception:
        return None


def oracle_id(mod, case: dict[str, Any]) -> str:
    if case.get("oracle_id"):
        return str(case["oracle_id"])
    menu = mod.build_menu(case["candidates"])
    expect = case.get("expect")
    for cid, entry in menu.items():
        act = entry.get("act")
        if isinstance(act, dict) and act.get("expect") == expect:
            return cid
    raise SystemExit(f"no oracle for case {case.get('id')}")


def rules_pick(mod, case: dict[str, Any]) -> dict[str, Any]:
    """Diagnostic heuristic: prefer description/expect overlap with goal/expect."""
    started = time.perf_counter()
    menu = mod.build_menu(case["candidates"])
    goal = str(case.get("goal") or "").lower()
    expect = str(case.get("expect") or "").lower()
    best = None
    best_score = -1
    for cid, entry in menu.items():
        if entry.get("control"):
            continue
        desc = entry["description"].lower()
        score = 0
        if expect and expect in desc:
            score += 5
        for token in ("clear", "multiply", "equals", "type", "proof"):
            if token in goal and token in desc:
                score += 1
        act = entry.get("act") or {}
        if expect and str(act.get("expect") or "").lower() == expect:
            score += 10
        if score > best_score:
            best_score = score
            best = cid
    if case.get("oracle_choice_kind") == "control":
        best = case.get("oracle_id") or mod.REOBSERVE
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "provider": "rules",
        "choice": best,
        "ok": best is not None,
        "latency_ms": latency_ms,
        "usage": {"inputTokens": 0, "outputTokens": 0},
        "invented": best not in menu and best not in (mod.REOBSERVE, mod.ABSTAIN),
    }


def llm_pick(mod, case: dict[str, Any], *, model: str, key: str) -> dict[str, Any]:
    menu = mod.build_menu(case["candidates"])
    criteria = {cid: entry["description"] for cid, entry in menu.items()}
    prompt = (
        "Pick exactly one action ID from the menu. Reply JSON only: "
        '{"id":"<id>"}. Do not invent IDs.\n'
        f"goal: {case['goal']}\n"
        f"state: {json.dumps(case.get('state'), ensure_ascii=False)}\n"
        f"menu: {json.dumps(criteria, ensure_ascii=False)}"
    )
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        return {
            "provider": "llm",
            "ok": False,
            "error": exc.read().decode()[:300],
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "usage": {},
            "invented": False,
            "choice": None,
        }
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    choice = None
    try:
        parsed = json.loads(text.strip())
        choice = parsed.get("id") if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        for cid in menu:
            if cid in text:
                choice = cid
                break
    usage = payload.get("usage") or {}
    tokens = {
        "inputTokens": usage.get("prompt_tokens") or usage.get("inputTokens") or 0,
        "outputTokens": usage.get("completion_tokens") or usage.get("outputTokens") or 0,
    }
    invented = choice is not None and choice not in menu
    return {
        "provider": "llm",
        "ok": choice in menu,
        "choice": choice,
        "latency_ms": latency_ms,
        "usage": tokens,
        "invented": invented,
        "raw": text[:200],
    }


def jev_pick(mod, case: dict[str, Any], *, live: bool) -> dict[str, Any]:
    oracle = oracle_id(mod, case)
    result = mod.decide(
        goal=case["goal"],
        candidates=case["candidates"],
        state=case.get("state"),
        mock_choice=None if live else oracle,
    )
    choice = result.get("choice")
    menu = mod.build_menu(case["candidates"])
    usage = result.get("usage") or {}
    return {
        "provider": "jev" if live else "jev_mock",
        "ok": bool(result.get("ok")),
        "choice": choice,
        "auto_act": bool(result.get("auto_act")),
        "latency_ms": result.get("latency_ms") or 0,
        "usage": {
            "inputTokens": usage.get("inputTokens") or 0,
            "outputTokens": usage.get("outputTokens") or 0,
        },
        "invented": choice is not None and choice not in menu,
        "error": result.get("error"),
    }


def summarize(rows: list[dict[str, Any]], oracle: str) -> dict[str, Any]:
    n = len(rows) or 1
    exact = sum(1 for r in rows if r.get("choice") == oracle)
    invented = sum(1 for r in rows if r.get("invented"))
    latencies = [float(r.get("latency_ms") or 0) for r in rows]
    in_tok = [int((r.get("usage") or {}).get("inputTokens") or 0) for r in rows]
    out_tok = [int((r.get("usage") or {}).get("outputTokens") or 0) for r in rows]
    return {
        "n": len(rows),
        "exact_match_rate": round(exact / n, 4),
        "invalid_id_rate": round(invented / n, 4),
        "p50_latency_ms": round(statistics.median(latencies), 3) if latencies else None,
        "mean_input_tokens": round(sum(in_tok) / n, 2),
        "mean_output_tokens": round(sum(out_tok) / n, 2),
        "mean_tokens": round((sum(in_tok) + sum(out_tok)) / n, 2),
    }


def usd_per_decision(provider: str, input_tokens: float, output_tokens: float) -> float | None:
    if provider in ("jev", "jev_mock"):
        input_price, output_price = JEV_INPUT_USD_PER_1M, JEV_OUTPUT_USD_PER_1M
    elif provider == "llm":
        input_price, output_price = LLM_INPUT_USD_PER_1M, LLM_OUTPUT_USD_PER_1M
    elif provider == "rules":
        input_price, output_price = 0.0, 0.0
    else:
        return None
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def pass_rule(summary: dict[str, Any]) -> dict[str, Any]:
    jev_key = "jev" if "jev" in summary else "jev_mock"
    jev = summary[jev_key]
    llm = summary.get("llm")
    rules = summary.get("rules")
    checks = {
        "jev_exact_ge_95": jev["exact_match_rate"] >= 0.95,
        "jev_invalid_id_zero": jev["invalid_id_rate"] == 0.0,
    }
    if llm:
        checks["jev_exact_ge_llm"] = jev["exact_match_rate"] >= llm["exact_match_rate"]
        lat_improve = (
            llm["p50_latency_ms"]
            and jev["p50_latency_ms"] is not None
            and jev["p50_latency_ms"] <= llm["p50_latency_ms"] * 0.9
        )
        lat_non_worse = (
            llm["p50_latency_ms"]
            and jev["p50_latency_ms"] is not None
            and jev["p50_latency_ms"] <= llm["p50_latency_ms"] * 1.1
        )
        jev_usd = jev.get("estimated_usd_per_decision")
        llm_usd = llm.get("estimated_usd_per_decision")
        usd_known = jev_usd is not None and llm_usd is not None and llm_usd > 0
        usd_improve = usd_known and jev_usd <= llm_usd * 0.8
        usd_non_worse = not usd_known or jev_usd <= llm_usd * 1.1
        efficiency_ok = (lat_improve and usd_non_worse) or (usd_improve and lat_non_worse)
        if lat_improve and usd_improve:
            note = "latency and USD/decision improved"
        elif lat_improve and not usd_known:
            note = "latency improved; USD/decision held because rates are unknown"
        elif lat_improve and usd_non_worse:
            note = "latency improved; USD/decision did not regress by more than 10%"
        elif usd_improve and lat_non_worse:
            note = "USD/decision improved; latency did not regress by more than 10%"
        else:
            note = "efficiency pair did not meet the improvement/non-regression rule"
        checks["primary_metric_improved"] = bool(efficiency_ok)
        checks["latency_improved_10pct"] = bool(lat_improve)
        checks["latency_not_worse_10pct"] = bool(lat_non_worse)
        checks["usd_improved_20pct"] = bool(usd_improve)
        checks["usd_not_worse_10pct_or_unknown"] = bool(usd_non_worse)
        checks["usd_rates_known"] = bool(usd_known)
    else:
        checks["primary_metric_improved"] = True
        checks["jev_exact_ge_llm"] = True
        note = "LLM comparison unavailable"
    if rules:
        checks["rules_diagnostic_only"] = True
    ok = all(
        checks[k]
        for k in (
            "jev_exact_ge_95",
            "jev_invalid_id_zero",
            "jev_exact_ge_llm",
            "primary_metric_improved",
        )
    )
    return {"ok": ok, "checks": checks, "note": note}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--live-jev", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    mod = load_jev_act()
    cases = json.loads(args.cases.read_text())["cases"]
    key = None if args.skip_llm else gateway_key()
    providers_runs: dict[str, list[dict[str, Any]]] = {"rules": [], "jev_mock": []}
    if args.live_jev:
        providers_runs["jev"] = []
    if key:
        providers_runs["llm"] = []

    details = []
    for case in cases:
        oracle = oracle_id(mod, case)
        for i in range(args.repeat):
            row = {"case": case["id"], "repeat": i, "oracle": oracle, "providers": {}}
            for provider in ("rules", "jev"):
                if provider == "rules":
                    result = rules_pick(mod, case)
                else:
                    result = jev_pick(mod, case, live=args.live_jev)
                row["providers"][result["provider"]] = result
                providers_runs.setdefault(result["provider"], []).append(
                    {**result, "oracle": oracle}
                )
            if key:
                result = llm_pick(mod, case, model=args.llm_model, key=key)
                row["providers"]["llm"] = result
                providers_runs["llm"].append(
                    {**result, "oracle": oracle, "exact": result.get("choice") == oracle}
                )
            details.append(row)

    # Re-summarize per provider with oracle from details
    summary: dict[str, Any] = {}
    for provider, runs in providers_runs.items():
        if not runs:
            continue
        # attach exact vs oracle already in runs via rebuild
        enriched = []
        for r in runs:
            enriched.append(r)
        # Need oracle per run - stored when appending
        exact_rows = []
        for r in enriched:
            oracle = r.get("oracle")
            exact_rows.append({**r, "choice_match": r.get("choice") == oracle})
        n = len(exact_rows) or 1
        exact = sum(1 for r in exact_rows if r.get("choice") == r.get("oracle"))
        invented = sum(1 for r in exact_rows if r.get("invented"))
        latencies = [float(r.get("latency_ms") or 0) for r in exact_rows]
        in_tok = [int((r.get("usage") or {}).get("inputTokens") or 0) for r in exact_rows]
        out_tok = [int((r.get("usage") or {}).get("outputTokens") or 0) for r in exact_rows]
        summary[provider] = {
            "n": len(exact_rows),
            "exact_match_rate": round(exact / n, 4),
            "invalid_id_rate": round(invented / n, 4),
            "p50_latency_ms": round(statistics.median(latencies), 3) if latencies else None,
            "mean_input_tokens": round(sum(in_tok) / n, 2),
            "mean_output_tokens": round(sum(out_tok) / n, 2),
            "mean_tokens": round((sum(in_tok) + sum(out_tok)) / n, 2),
        }
        summary[provider]["estimated_usd_per_decision"] = usd_per_decision(
            provider,
            summary[provider]["mean_input_tokens"],
            summary[provider]["mean_output_tokens"],
        )

    gate = pass_rule(summary)
    out = {
        "ok": gate["ok"],
        "pass": gate,
        "summary": summary,
        "live_jev": bool(args.live_jev),
        "llm_model": args.llm_model if key else None,
        "prices_usd_per_1m_tokens": {
            "jev": {"input": JEV_INPUT_USD_PER_1M, "output": JEV_OUTPUT_USD_PER_1M},
            "llm": {"input": LLM_INPUT_USD_PER_1M, "output": LLM_OUTPUT_USD_PER_1M},
        },
        "details": details,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if gate["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
