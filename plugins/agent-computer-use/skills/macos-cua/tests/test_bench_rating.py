import importlib.util
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest


SKILL = Path(__file__).resolve().parents[1]
RATING = SKILL / "scripts" / "bench_rating.py"
RUNNER = SKILL / "scripts" / "run_benchmarks.py"
FACADE = SKILL / "scripts" / "macos-cua.py"
COMPACT = SKILL / "scripts" / "compact_mcp.py"
CONTRACT = SKILL / "references" / "entry-contract.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repeat(measured, ok=True):
    criteria = {
        "accuracy": True,
        "visibility": True,
        "speed": True,
        "context_efficiency": True,
        "robustness": True,
    }
    if not ok:
        criteria = {key: False for key in criteria}
    return {"ok": ok, "criteria": criteria, "measured": measured}


class ClampAndPercentileTests(unittest.TestCase):
    def setUp(self):
        self.rating = load_module(f"rating_math_{id(self)}", RATING)

    def test_clamp01_bounds(self):
        self.assertEqual(self.rating.clamp01(-1), 0.0)
        self.assertEqual(self.rating.clamp01(0), 0.0)
        self.assertEqual(self.rating.clamp01(0.25), 0.25)
        self.assertEqual(self.rating.clamp01(1), 1.0)
        self.assertEqual(self.rating.clamp01(2), 1.0)

    def test_percentile_type7_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(self.rating.percentile(values, 0), 1.0)
        self.assertEqual(self.rating.percentile(values, 100), 4.0)
        self.assertEqual(self.rating.percentile(values, 50), 2.5)
        self.assertEqual(self.rating.percentile([7.0], 95), 7.0)
        self.assertAlmostEqual(self.rating.percentile([1.0, 2.0, 3.0], 95), 2.9)
        with self.assertRaises(ValueError):
            self.rating.percentile([], 50)


class FloorScoreTests(unittest.TestCase):
    def setUp(self):
        self.rating = load_module(f"rating_floor_{id(self)}", RATING)

    def test_floor_score_at_floor_is_ten(self):
        self.assertEqual(self.rating._floor_score(1.582, 1.582), 10.0)

    def test_floor_score_double_floor_is_five(self):
        self.assertEqual(self.rating._floor_score(2.0, 1.0), 5.0)

    def test_floor_score_ten_times_floor_is_one(self):
        self.assertEqual(self.rating._floor_score(10.0, 1.0), 1.0)

    def test_floor_score_none_floor_is_unrated(self):
        self.assertIsNone(self.rating._floor_score(1.0, None))

    def test_floor_score_zero_p50_is_ten(self):
        self.assertEqual(self.rating._floor_score(0.0, 1.0), 10.0)
        self.assertEqual(self.rating._floor_score(0.0, 0.0), 10.0)

    def test_floor_score_zero_floor_is_zero(self):
        self.assertEqual(self.rating._floor_score(1.0, 0.0), 0.0)


class RateRowTests(unittest.TestCase):
    def setUp(self):
        self.rating = load_module(f"rating_row_{id(self)}", RATING)
        self.full = {
            "budget_seconds": 10,
            "max_step_ms": 1000,
            "bytes_budget": 1000,
            "pointer_required": True,
            "floor_seconds": 1.0,
            "floor_max_step_ms": 100,
            "floor_bytes": 50,
            "floor_driver_calls": 2,
        }

    def test_saturating_row_scores_ten(self):
        row = dict(self.full)
        row["floor_ax_snapshots"] = 2
        scores = self.rating.rate_row(
            row,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "ax_snapshots": 2,
                        "robust": True,
                    }
                )
            ],
        )
        for key in self.rating.SCORE_KEYS:
            self.assertEqual(scores[key], 10.0, key)
        self.assertEqual(scores["overall"], 10.0)
        self.assertEqual(scores["trust_gate_zeros"], [])
        self.assertEqual(scores["unrated"], [])
        self.assertFalse(scores["gated"])

    def test_double_floor_scores_five_on_cost_dimensions(self):
        row = dict(self.full)
        row["floor_ax_snapshots"] = 2
        scores = self.rating.rate_row(
            row,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 2.0,
                        "max_step_ms": 200,
                        "output_bytes": 100,
                        "driver_calls": 4,
                        "ax_snapshots": 4,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["speed"], 5.0)
        self.assertEqual(scores["efficiency"], 5.0)
        self.assertEqual(scores["token_efficiency"], 5.0)

    def test_trust_gate_zero_is_flagged(self):
        scores = self.rating.rate_row(
            self.full,
            [
                _repeat(
                    {
                        "readback": False,
                        "cursor_visible": True,
                        "duration_s": 1,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "robust": True,
                    },
                    ok=False,
                )
            ],
        )
        self.assertEqual(scores["accuracy"], 0.0)
        self.assertIn("accuracy", scores["trust_gate_zeros"])
        self.assertTrue(scores["gated"])
        self.assertGreater(scores["overall"], 0.0)

    def test_accuracy_and_visibility_excluded_from_overall(self):
        row = dict(self.full)
        row["floor_ax_snapshots"] = 2
        measured = {
            "readback": True,
            "cursor_visible": True,
            "duration_s": 2.0,
            "max_step_ms": 200,
            "output_bytes": 50,
            "driver_calls": 4,
            "ax_snapshots": 4,
            "robust": True,
        }
        scores = self.rating.rate_row(row, [_repeat(measured)])
        graded = [scores[key] for key in self.rating.GRADED_KEYS]
        self.assertEqual(scores["accuracy"], 10.0)
        self.assertEqual(scores["visibility"], 10.0)
        self.assertEqual(scores["overall"], round(sum(graded) / len(graded), 1))
        self.assertEqual(scores["overall"], 7.0)
        self.assertNotEqual(scores["overall"], 7.9)
        failed = self.rating.rate_row(
            row, [_repeat({**measured, "readback": False, "cursor_visible": False})]
        )
        self.assertEqual(failed["accuracy"], 0.0)
        self.assertEqual(failed["visibility"], 0.0)
        self.assertEqual(failed["overall"], scores["overall"])
        self.assertTrue(failed["gated"])

    def test_efficiency_axis_from_floor_ax_snapshots(self):
        row = dict(self.full)
        row["floor_ax_snapshots"] = 3
        scores = self.rating.rate_row(
            row,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "ax_snapshots": 6,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["efficiency"], 5.0)
        self.assertNotIn("efficiency", scores["unrated"])
        absent = dict(self.full)
        unrated = self.rating.rate_row(
            absent,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "ax_snapshots": 6,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertIsNone(unrated["efficiency"])
        self.assertIn("efficiency", unrated["unrated"])
        null_floor = dict(self.full)
        null_floor["floor_ax_snapshots"] = None
        self.assertIsNone(
            self.rating.rate_row(
                null_floor,
                [
                    _repeat(
                        {
                            "readback": True,
                            "cursor_visible": True,
                            "duration_s": 1.0,
                            "max_step_ms": 100,
                            "driver_calls": 2,
                            "ax_snapshots": 6,
                            "robust": True,
                        }
                    )
                ],
            )["efficiency"]
        )

    def test_zero_driver_calls_scores_token_efficiency_ten(self):
        scores = self.rating.rate_row(
            self.full,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 0,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["token_efficiency"], 10.0)

    def test_visibility_and_token_efficiency_none_excluded_from_mean(self):
        row = {
            "budget_seconds": 10,
            "bytes_budget": 100,
            "pointer_required": False,
            "floor_seconds": 1.0,
        }
        scores = self.rating.rate_row(
            row,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": False,
                        "duration_s": 1.0,
                        "output_bytes": 0,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertIsNone(scores["visibility"])
        self.assertIsNone(scores["token_efficiency"])
        self.assertIsNone(scores["efficiency"])
        self.assertNotIn("visibility", scores["trust_gate_zeros"])
        self.assertFalse(scores["gated"])
        present = [scores[key] for key in self.rating.GRADED_KEYS if scores[key] is not None]
        self.assertEqual(scores["overall"], round(sum(present) / len(present), 1))
        self.assertEqual(len(present), 3)
        self.assertEqual(scores["unrated"], ["efficiency", "token_efficiency"])
        self.assertNotIn(0.0, (scores["visibility"], scores["token_efficiency"], scores["efficiency"]))

    def test_spread_penalty_fires_and_does_not_fire(self):
        stable = [
            _repeat(
                {
                    "readback": True,
                    "cursor_visible": True,
                    "duration_s": 1.0,
                    "max_step_ms": 100,
                    "output_bytes": 10,
                    "driver_calls": 1,
                    "robust": True,
                }
            )
            for _ in range(3)
        ]
        self.assertEqual(self.rating.rate_row(self.full, stable)["reliability"], 10.0)
        spread = [
            _repeat(
                {
                    "readback": True,
                    "cursor_visible": True,
                    "duration_s": duration,
                    "max_step_ms": 100,
                    "output_bytes": 10,
                    "driver_calls": 1,
                    "robust": True,
                }
            )
            for duration in (1.0, 1.0, 1.0, 1.0, 10.0)
        ]
        self.assertEqual(self.rating.rate_row(self.full, spread)["reliability"], 8.0)

    def test_speed_blends_when_floor_max_step_and_measured_exist(self):
        scores = self.rating.rate_row(
            self.full,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 200,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["speed"], 7.5)

    def test_speed_skips_step_blend_without_floor_max_step_ms(self):
        row = dict(self.full)
        row["floor_max_step_ms"] = None
        scores = self.rating.rate_row(
            row,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 200,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["speed"], 10.0)

    def test_speed_skips_step_blend_without_measured_max_step(self):
        scores = self.rating.rate_row(
            self.full,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "output_bytes": 50,
                        "driver_calls": 2,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["speed"], 10.0)

    def test_missing_floor_ax_snapshots_leaves_efficiency_unrated(self):
        scores = self.rating.rate_row(
            self.full,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 9999,
                        "driver_calls": 2,
                        "ax_snapshots": 99,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertIsNone(scores["efficiency"])
        self.assertEqual(scores["unrated"], ["efficiency"])
        present = [scores[key] for key in self.rating.GRADED_KEYS if scores[key] is not None]
        self.assertEqual(len(present), 4)
        self.assertNotIn(0.0, present)
        self.assertEqual(scores["overall"], round(sum(present) / len(present), 1))
        self.assertNotEqual(scores["overall"], round((sum(present) + 0.0) / 5, 1))

    def test_token_efficiency_from_floor_driver_calls(self):
        scores = self.rating.rate_row(
            self.full,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 4,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertEqual(scores["token_efficiency"], 5.0)
        missing = dict(self.full)
        del missing["floor_driver_calls"]
        unrated = self.rating.rate_row(
            missing,
            [
                _repeat(
                    {
                        "readback": True,
                        "cursor_visible": True,
                        "duration_s": 1.0,
                        "max_step_ms": 100,
                        "output_bytes": 50,
                        "driver_calls": 4,
                        "robust": True,
                    }
                )
            ],
        )
        self.assertIsNone(unrated["token_efficiency"])

    def test_regression_anchor_speed_ratings(self):
        contract = {
            row["name"]: row for row in json.loads(CONTRACT.read_text())["suite"]
        }
        passing = {
            "readback": True,
            "cursor_visible": True,
            "output_bytes": 1,
            "driver_calls": 1,
            "robust": True,
        }
        calc_duration = self.rating._round1(self.rating._floor_score(5.828, 1.582))
        calc_step = self.rating._round1(self.rating._floor_score(1108, 106))
        calc_blend = self.rating._round1(
            (
                self.rating._floor_score(5.828, 1.582)
                + self.rating._floor_score(1108, 106)
            )
            / 2.0
        )
        self.assertEqual(calc_duration, 2.7)
        self.assertEqual(calc_step, 1.0)
        self.assertEqual(calc_blend, 1.8)
        calculator = self.rating.rate_row(
            contract["calculator-8x8"],
            [_repeat({**passing, "duration_s": 5.828, "max_step_ms": 1108})],
        )
        self.assertEqual(calculator["speed"], 1.8)
        folder = self.rating.rate_row(
            contract["folder-downloads"],
            [_repeat({**passing, "duration_s": 0.986})],
        )
        self.assertEqual(folder["speed"], 3.6)
        textedit = self.rating.rate_row(
            contract["textedit-right-click"],
            [_repeat({**passing, "duration_s": 7.528})],
        )
        self.assertEqual(textedit["speed"], 1.7)
        whatsapp = self.rating.rate_row(
            contract["whatsapp-new-chat"],
            [_repeat({**passing, "duration_s": 2.583})],
        )
        self.assertEqual(whatsapp["speed"], 0.5)

    def test_score_measured_still_uses_budgets(self):
        row = {
            "budget_seconds": 25,
            "max_step_ms": 2500,
            "bytes_budget": 12000,
            "pointer_required": True,
            "floor_seconds": 1.582,
            "floor_max_step_ms": 106,
            "floor_bytes": None,
        }
        under = {
            "readback": True,
            "cursor_visible": True,
            "robust": True,
            "output_bytes": 400,
            "duration_s": 5.828,
            "max_step_ms": 1108,
        }
        self.assertTrue(all(self.rating.score_measured(row, under).values()))
        over_time = dict(under)
        over_time["duration_s"] = 26
        self.assertFalse(self.rating.score_measured(row, over_time)["speed"])
        over_step = dict(under)
        over_step["max_step_ms"] = 2501
        self.assertFalse(self.rating.score_measured(row, over_step)["speed"])
        over_bytes = dict(under)
        over_bytes["output_bytes"] = 12001
        self.assertFalse(self.rating.score_measured(row, over_bytes)["context_efficiency"])
        exact_budget = dict(under)
        exact_budget["duration_s"] = 25
        exact_budget["max_step_ms"] = 2500
        exact_budget["output_bytes"] = 12000
        self.assertTrue(self.rating.score_measured(row, exact_budget)["speed"])
        self.assertTrue(self.rating.score_measured(row, exact_budget)["context_efficiency"])


class CompareRowTests(unittest.TestCase):
    def setUp(self):
        self.rating = load_module(f"rating_compare_{id(self)}", RATING)

    def test_compare_detects_regression_and_improvement(self):
        current = [_repeat({"duration_s": 2.0, "max_step_ms": 200, "output_bytes": 20, "driver_calls": 4})]
        baseline = [_repeat({"duration_s": 1.0, "max_step_ms": 200, "output_bytes": 20, "driver_calls": 4})]
        worse = self.rating.compare_row(current, baseline)
        self.assertEqual(worse["duration_s"], 1.0)
        self.assertTrue(worse["regressed"])
        better = self.rating.compare_row(baseline, current)
        self.assertEqual(better["duration_s"], -1.0)
        self.assertFalse(better["regressed"])

    def test_compare_skips_missing_metric(self):
        payload = self.rating.compare_row(
            [_repeat({"duration_s": 1.0, "output_bytes": 10})],
            [_repeat({"duration_s": 1.0, "output_bytes": 10})],
        )
        self.assertIsNone(payload["max_step_ms"])
        self.assertIsNone(payload["driver_calls"])
        self.assertFalse(payload["regressed"])


class HarnessAndFacadeTests(unittest.TestCase):
    def test_repeat_defaults_to_one(self):
        runner = load_module("macos_cua_run_benchmarks_defaults", RUNNER)
        self.assertEqual(inspect.signature(runner.run_suite).parameters["repeat"].default, 1)

    def test_existing_budgets_unchanged(self):
        data = json.loads(CONTRACT.read_text())
        expected = {
            "calculator-8x8": (25, 2500, 12000, 1.582, 106, 52, 2, 1),
            "folder-downloads": (20, None, 4000, 0.358, None, 40, 1, 0),
            "textedit-right-click": (30, 5000, 8000, 1.284, 85, 51, 3, 1),
            "whatsapp-new-chat": (15, 4000, 800, 0.125, 115, 49, 2, 1),
        }
        removed = ("round_trip_budget", "driver_bytes_budget", "budgets_pending")
        for row in data["suite"]:
            budget, step, nbytes, floor_s, floor_step, floor_b, floor_ax, floor_calls = expected[
                row["name"]
            ]
            self.assertEqual(row["budget_seconds"], budget)
            self.assertEqual(row.get("max_step_ms"), step)
            self.assertEqual(row["bytes_budget"], nbytes)
            self.assertEqual(row["floor_seconds"], floor_s)
            self.assertEqual(row.get("floor_max_step_ms"), floor_step)
            self.assertEqual(row.get("floor_bytes"), floor_b)
            self.assertEqual(row["floor_ax_snapshots"], floor_ax)
            self.assertEqual(row["floor_driver_calls"], floor_calls)
            self.assertTrue(row["floor_derivation"])
            for key in removed:
                self.assertNotIn(key, row)
        model = data["rating_model"]
        self.assertNotIn("budgets_pending", model)
        self.assertIn("never a rating denominator", model["budget_gate"])
        self.assertIn("subtract 2.0", model["reliability"])
        self.assertNotIn("multiply by 0.8", model["reliability"])
        self.assertIn("floor_ax_snapshots", model["efficiency"])
        self.assertNotIn("asserted_batch", model["efficiency"])
        self.assertIn("GRADED_KEYS", model["overall"])
        self.assertIn("irreducible", model["floor_ax_snapshots"])

    def test_score_booleans_unchanged_by_ratings(self):
        runner = load_module("macos_cua_run_benchmarks_score", RUNNER)
        criteria = runner.score(
            {"budget_seconds": 25, "bytes_budget": 12000, "pointer_required": True, "max_step_ms": 2500},
            {
                "readback": True,
                "cursor_visible": True,
                "robust": True,
                "asserted_batch": True,
                "output_bytes": 400,
                "duration_s": 6.1,
                "max_step_ms": 1346,
                "driver_calls": 9,
            },
        )
        self.assertTrue(all(criteria.values()))
        self.assertEqual(set(criteria), set(runner.CRITERIA))

    def test_compact_bytes_ignore_telemetry_keys(self):
        runner = load_module("macos_cua_run_benchmarks_bytes", RUNNER)
        compact = {"ok": True, "verified": True, "final": "64", "steps": [{"action": "click"}]}
        before = runner._compact_bytes(compact)
        self.assertEqual(runner._compact_bytes(compact), before)
        self.assertNotIn("driver_calls", compact)

    def test_facade_reset_and_read(self):
        cua = load_module("macos_cua_facade_telemetry", FACADE)
        cua.telemetry_reset()
        self.assertEqual(
            cua.telemetry_read(),
            {
                "driver_calls": 0,
                "driver_seconds": 0.0,
                "ax_snapshots": 0,
                "cli_invocations": 0,
                "cli_response_bytes": 0,
            },
        )

    def test_timeout_then_socket_recovery_counts_one_driver_call(self):
        cua = load_module("macos_cua_facade_telemetry_retry", FACADE)
        cua.telemetry_reset()
        timed_out = SimpleNamespace(
            timeout=None,
            sent=[],
            closed=False,
            recv_error=TimeoutError("timed out"),
        )
        timed_out.settimeout = lambda _timeout: None
        timed_out.sendall = lambda _data: None
        timed_out.close = lambda: None

        def _timeout_recv(_n):
            raise TimeoutError("timed out")

        timed_out.recv = _timeout_recv
        recovered_line = (
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "content": [{"text": "x", "type": "text"}],
                        "structuredContent": {"ok": True},
                    },
                }
            ).encode()
            + b"\n"
        )
        recovered = SimpleNamespace(
            timeout=None,
            sent=[],
            closed=False,
            _chunks=[recovered_line],
        )
        recovered.settimeout = lambda _timeout: None
        recovered.sendall = lambda data: recovered.sent.append(data)
        recovered.close = lambda: None
        recovered.recv = lambda _n: recovered._chunks.pop(0)
        with mock.patch.dict(
            os.environ, {"MACOS_CUA_DRIVER_SOCKET": "/tmp/macos-cua-bench-test.sock"}
        ):
            with mock.patch.object(
                cua, "_connect_driver_socket", side_effect=[timed_out, recovered]
            ):
                with mock.patch.object(
                    cua.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run:
                    result = cua.call_driver("get_window_state", timeout=5)
        self.assertTrue(result["ok"])
        self.assertEqual(cua.telemetry_read()["driver_calls"], 1)
        self.assertNotIn("call", run.call_args.args[0])

    def test_ax_snapshot_counts_without_changing_error_payload_keys(self):
        cua = load_module("macos_cua_facade_telemetry_ax", FACADE)
        cua.telemetry_reset()
        with mock.patch.dict("sys.modules", {"ApplicationServices": None}):
            result = cua._native_ax_snapshot(1, max_elements=10)
        self.assertIn("error", result)
        self.assertNotIn("ax_snapshots", result)
        self.assertEqual(cua.telemetry_read()["ax_snapshots"], 1)

    def test_compact_mcp_run_cli_does_not_add_payload_keys(self):
        mcp = load_module("compact_mcp_telemetry", COMPACT)
        mcp.telemetry_reset()
        body = json.dumps({"ok": True, "text": "64"}, separators=(",", ":"))
        fake = SimpleNamespace(returncode=0, stdout=body, stderr="")
        with mock.patch.object(mcp.subprocess, "run", return_value=fake):
            payload = mcp.run_cli(["python3", "macos-cua.py", "state", "Calculator"], 1)
        self.assertEqual(payload, {"ok": True, "text": "64"})
        self.assertNotIn("cli_invocations", payload)
        self.assertEqual(mcp.telemetry_read()["cli_invocations"], 1)


if __name__ == "__main__":
    unittest.main()
