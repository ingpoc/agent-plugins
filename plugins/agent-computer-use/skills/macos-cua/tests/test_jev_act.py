#!/usr/bin/env python3
"""Unit tests for jev_act candidate menu + fail-closed validation."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "jev_act.py"
BENCH = SKILL / "scripts" / "bench_jev_act.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jev_act_cases.json"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class JevActTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_script("macos_cua_jev_act", SCRIPT)
        cls.bench = load_script("macos_cua_jev_act_bench_test", BENCH)
        cls.cases = json.loads(FIXTURES.read_text())["cases"]

    def test_act_id_stable_and_collision_free(self):
        act = {
            "app": "Calculator",
            "steps": [{"label": "8"}],
            "expect": "8",
        }
        a = self.mod.act_id(act)
        b = self.mod.act_id(dict(act))
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("act_"))
        self.assertEqual(len(a), 16)

    def test_duplicate_candidates_rejected(self):
        twin = {
            "description": "same",
            "act": {"app": "Calculator", "steps": [{"label": "1"}], "expect": "1"},
        }
        with self.assertRaises(ValueError):
            self.mod.build_menu([twin, dict(twin)])

    def test_mock_picks_oracle_act_and_auto_acts(self):
        case = self.cases[0]
        menu = self.mod.build_menu(case["candidates"])
        oracle = next(
            cid
            for cid, entry in menu.items()
            if entry.get("act") and entry["act"].get("expect") == case["expect"]
        )
        result = self.mod.decide(
            goal=case["goal"],
            candidates=case["candidates"],
            state=case["state"],
            mock_choice=oracle,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["auto_act"])
        self.assertEqual(result["choice"], oracle)
        self.assertEqual(result["act"]["expect"], "64")

    def test_invented_id_never_auto_acts(self):
        case = self.cases[0]
        result = self.mod.decide(
            goal=case["goal"],
            candidates=case["candidates"],
            state=case["state"],
            mock_choice="act_deadbeefdead",
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["auto_act"])
        self.assertEqual(result["error"], "invented_or_unknown_id")

    def test_abstain_and_reobserve_never_auto_act(self):
        case = self.cases[0]
        for control in (self.mod.ABSTAIN, self.mod.REOBSERVE):
            result = self.mod.decide(
                goal=case["goal"],
                candidates=case["candidates"],
                state=case["state"],
                mock_choice=control,
            )
            self.assertTrue(result["ok"], control)
            self.assertFalse(result["auto_act"], control)
            self.assertIsNone(result.get("act"), control)

    def test_low_confidence_asks_user(self):
        case = self.cases[0]
        menu = self.mod.build_menu(case["candidates"])
        oracle = next(cid for cid, e in menu.items() if e.get("act") and e["act"].get("expect") == "64")

        def fake_evaluate(**kwargs):
            return {
                "ok": True,
                "answers": {
                    "next": {
                        "type": "choice",
                        "choice": oracle,
                        "confidence": 0.2,
                        "probabilities": {oracle: 0.4},
                    }
                },
                "recommend": {"ask_user": True, "next": oracle},
                "usage": {},
                "latency_ms": 1,
            }

        original = self.mod.run_evaluate
        self.mod.run_evaluate = fake_evaluate  # type: ignore[method-assign]
        try:
            result = self.mod.decide(
                goal=case["goal"],
                candidates=case["candidates"],
                state=case["state"],
                min_confidence=0.7,
            )
        finally:
            self.mod.run_evaluate = original  # type: ignore[method-assign]
        self.assertTrue(result["ok"])
        self.assertFalse(result["auto_act"])
        self.assertTrue(result["ask_user"])

    def test_fixture_reobserve_oracle(self):
        case = next(c for c in self.cases if c["id"] == "stale-state-reobserve")
        result = self.mod.decide(
            goal=case["goal"],
            candidates=case["candidates"],
            state=case["state"],
            mock_choice=case["oracle_id"],
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["auto_act"])
        self.assertTrue(result["reobserve"])

    def test_cost_gate_prefers_usd_over_raw_tokens_and_jev_output_is_free(self):
        jev_usd = self.bench.usd_per_decision("jev", 1000, 10000)
        llm_usd = self.bench.usd_per_decision("llm", 10, 50)
        self.assertEqual(jev_usd, 1000 * self.bench.JEV_INPUT_USD_PER_1M / 1_000_000)
        self.assertLess(jev_usd, llm_usd)

        summary = {
            "jev": {
                "exact_match_rate": 1.0,
                "invalid_id_rate": 0.0,
                "p50_latency_ms": 105.0,
                "mean_tokens": 11000,
                "estimated_usd_per_decision": jev_usd,
            },
            "llm": {
                "exact_match_rate": 1.0,
                "invalid_id_rate": 0.0,
                "p50_latency_ms": 100.0,
                "mean_tokens": 60,
                "estimated_usd_per_decision": llm_usd,
            },
        }
        gate = self.bench.pass_rule(summary)
        self.assertGreater(summary["jev"]["mean_tokens"], summary["llm"]["mean_tokens"])
        self.assertTrue(gate["checks"]["usd_improved_20pct"])
        self.assertTrue(gate["ok"])


if __name__ == "__main__":
    unittest.main()
