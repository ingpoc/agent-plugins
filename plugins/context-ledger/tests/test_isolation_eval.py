"""isolation_eval.py: metrics on a tiny synthetic ledger. No HOME: the data dir is an explicit temp
--data root (the plugin's override; CONTEXT_LEDGER_DATA is the env form), never ~/.context-ledger."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from context_ledger.store import Store, init_ledger  # noqa: E402

_spec = importlib.util.spec_from_file_location("isolation_eval", ROOT / "scripts" / "isolation_eval.py")
iso = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iso)  # type: ignore[union-attr]
FIXTURE = ROOT / "tests" / "fixtures" / "isolation_30.json"


def _body(summary: str, entities: list[str], scope: str) -> dict:
    return {"kind": "decision", "summary": summary, "situation": summary, "action": summary,
            "rationale": "fixture reason", "constraints": [], "applicability": "any",
            "entities": entities, "scope": scope, "evidence": [], "sensitivity": "model_safe",
            "approval": {"state": "none", "authority_ref": None}}


class IsolationEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home = os.environ.get("HOME")
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.data = base / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "iso-test", "tester")
        store = Store(self.data, actor_channel="agent")
        ids = {}
        with contextlib.redirect_stderr(io.StringIO()):
            for key, summary, ents, scope in (
                ("lease", "Comet browser lease is held by one agent at a time", [], "fleet"),
                ("proj", "Project alpha deploy uses blue green switch", ["alpha"], "project:alpha"),
                ("other", "Project beta comet browser lease is held nightly", ["beta"], "project:beta"),
            ):
                ids[key] = store.record({"request_id": str(uuid.uuid4()), "occurred_at": None,
                                         "body": _body(summary, ents, scope),
                                         "outcome": {"status": "pending", "note": "", "evidence": []}},
                                        mcp=True)["decision_id"]
        store.close()
        self.db = self.data / "ledger" / "ledger.sqlite3"
        p = {k: v[:8] for k, v in ids.items()}
        self.fixture = base / "fx.json"
        self.fixture.write_text(json.dumps({
            "ledger": {"scope_id": "iso-test"},
            "finds": [{"id": "A1", "query": "comet browser lease held", "entities": ["alpha"],
                       "actor": "test.iso", "binding": {"project": "alpha", "tools": ["comet"]},
                       "relevant_ids": [p["lease"]], "fleet_ids": [p["lease"]],
                       "forbidden_ids": [p["other"]], "zero_hits_ok": False},
                      {"id": "T1", "query": "zzz nothing", "entities": [], "actor": "test.iso",
                       "binding": {"project": None, "tools": []}, "relevant_ids": [], "fleet_ids": [],
                       "forbidden_ids": [p["proj"], p["other"]], "zero_hits_ok": True}],
        }))
        self.p = p

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self.assertEqual(os.environ.get("HOME"), self._home)

    def test_metrics_and_source_untouched(self) -> None:
        before = self.db.read_bytes()
        rep = iso.run(self.db, ROOT, self.fixture, None)
        self.assertEqual(self.db.read_bytes(), before, "source DB must not change")
        a1 = rep["finds"][0]
        # Text match returns both lease records. The alpha entity no longer adds the off-topic project row.
        self.assertNotIn(self.p["proj"], a1["hits"])
        self.assertEqual(a1["fillers"], 0)
        self.assertEqual(a1["fleet_found"], 1)
        self.assertEqual(a1["leaks"], 0)  # beta lease is project:beta; unscoped find is fleet only
        t1 = rep["finds"][1]
        self.assertEqual(t1["hits"], [])
        m = rep["metrics"]["all"]
        self.assertEqual((m["zero_hit"], m["zero_hit_violations"], m["errors"]), (1, 0, 0))
        self.assertEqual(rep["metrics"]["audit_entity_runs"]["finds"], 1)

    def test_deterministic_and_scope_arg_fallback(self) -> None:
        r1 = iso.run(self.db, ROOT, self.fixture, None)
        r2 = iso.run(self.db, ROOT, self.fixture, "scope")
        self.assertFalse(r2["finds"][0]["scope_passed"])  # main rejects the unknown key; retried without
        for r in (r1, r2):
            for f in r["finds"]:
                f.pop("scope_passed")
        self.assertEqual(r1["finds"], r2["finds"])
        self.assertEqual(r1["metrics"], r2["metrics"])

    def test_cli_exits_zero_on_bad_input(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = iso.main(["--db", str(Path(self._tmp.name) / "missing.sqlite3"), "--plugin", str(ROOT),
                             "--fixture", str(self.fixture)])
        self.assertEqual(code, 0)
        self.assertIn("error", json.loads(out.getvalue().splitlines()[0]))

    def test_frozen_fixture_shape(self) -> None:
        fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(fx["finds"]), 20)
        keys = {"id", "query", "entities", "actor", "binding", "relevant_ids", "fleet_ids", "forbidden_ids", "zero_hits_ok"}
        for f in fx["finds"]:
            self.assertTrue(keys <= set(f), f["id"])
            self.assertTrue(set(f["fleet_ids"]) <= set(f["relevant_ids"]), f["id"])
            self.assertFalse(set(f["relevant_ids"]) & set(f["forbidden_ids"]), f["id"])


if __name__ == "__main__":
    unittest.main()
