"""Access log metadata regressions for the public lookup entry points."""
from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from context_ledger import __version__
from context_ledger.store import Store, init_ledger


class AccessLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir(mode=0o700)
        init_ledger(self.data, None, "example-project", "test-agent")
        self.store = Store(self.data, actor_channel="agent")
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr.__enter__()

    def tearDown(self) -> None:
        self.stderr.__exit__(None, None, None)
        self.store.close()
        self.tmp.cleanup()

    def save(self, project: str = "example-project") -> str:
        result = self.store.save(
            {
                "request_id": "8a4c3dbe-195c-4311-98f0-86aeac8f845d",
                "type": "user_decision",
                "decision": "Use the project default workflow",
                "why": "It matches the project policy",
                "entities": ["example-project"],
                "scope": f"project:{project}",
                "actor": "test-agent",
            },
            mcp=True,
        )
        return result["decision_id"]

    def rows(self) -> list[sqlite3.Row]:
        return self.store.open().execute(
            "select tool, version, tokens, project from access_log order by seq"
        ).fetchall()

    def test_lookup_rows_record_version_tokens_and_project(self) -> None:
        decision_id = self.save()
        self.store.find(
            {"query": "project default workflow", "caller_scope": {"project": "example-project"}},
            mcp=True,
        )
        self.store.get({"decision_id": decision_id}, mcp=True)

        self.assertEqual([row["tool"] for row in self.rows()], ["save", "find", "get"])
        for row in self.rows():
            self.assertEqual(row["version"], __version__)
            self.assertEqual(row["tokens"], "not_reported")
            self.assertEqual(row["project"], "example-project")

    def test_old_access_log_is_migrated_additively(self) -> None:
        conn = self.store.open()
        conn.execute("insert into access_log(ts,tool,caller,n,bytes,ids,ms) values ('old','find','old-agent',0,0,'[]',0)")
        # Simulate the historical schema by removing only the newly added columns.
        conn.close()
        self.store.conn = None
        legacy_db = sqlite3.connect(self.store.db_path)
        legacy_db.execute("alter table access_log rename to access_log_current")
        legacy_db.execute(
            "create table access_log (seq integer primary key autoincrement, ts text not null, tool text not null, "
            "caller text not null, task_id text, query text, n integer not null, bytes integer not null, "
            "ids text not null check(json_valid(ids)), ms integer not null)"
        )
        legacy_db.execute(
            "insert into access_log(seq,ts,tool,caller,task_id,query,n,bytes,ids,ms) "
            "select seq,ts,tool,caller,task_id,query,n,bytes,ids,ms from access_log_current"
        )
        legacy_db.execute("drop table access_log_current")
        legacy_db.commit()
        legacy_db.close()

        self.store.open()
        old = self.store.open().execute("select version,tokens,project from access_log where ts='old'").fetchone()
        self.assertEqual(tuple(old), (None, None, None))
        decision_id = self.save()
        newest = self.store.open().execute(
            "select version,tokens,project from access_log where tool='save' order by seq desc limit 1"
        ).fetchone()
        self.assertEqual(tuple(newest), (__version__, "not_reported", "example-project"))

    def test_find_reply_unchanged(self) -> None:
        self.save()
        result = self.store.find({"query": "project default workflow"}, mcp=True)
        self.assertEqual(set(result), {"matches", "has_more"})


    def test_get_row_records_the_caller_project_not_the_record_project(self) -> None:
        decision_id = self.save(project="other-project")
        self.store.get({"decision_id": decision_id}, mcp=True)
        row = self.store.open().execute(
            "select project from access_log where tool='get' order by seq desc limit 1"
        ).fetchone()
        self.assertEqual(row["project"], "example-project")

    def test_save_rows_do_not_count_as_lookups(self) -> None:
        self.save()
        report = self.store.access_report()
        self.assertEqual(report["by_caller"], {})
        from context_ledger.viewer import LedgerView
        self.store.close()
        kinds = [e["tool"] for e in LedgerView(self.data).activity({})["items"] if e["k"] == "lookup"]
        self.assertNotIn("save", kinds)

if __name__ == "__main__":
    unittest.main()
