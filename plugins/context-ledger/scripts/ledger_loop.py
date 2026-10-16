#!/usr/bin/env python3
"""Fail-loud Context Ledger closeout check (Guru 2026-10-11).

  ledger_loop.py [--data DIR] start <task_id> --bot NAME --query "<problem now>" [--found id,id]
  ledger_loop.py [--data DIR] close <task_id> [--decision id ...]

`close` prints one PASS line (exit 0) or one FAIL line with the fix (exit 1; exit 2 when the
ledger cannot be read). PASS needs:
  1. a `start` lookup recorded for task_id, and
  2. for every decision id in that task's `applied` events, a latest task outcome
     (outcome event with this task_id) whose status is final
     (success=worked, failed, inconclusive=partial, abandoned) and that cites evidence.

Applied and outcome events are read from the ledger, never from a local log:
  store   DATA/binding.json exists: open the bound SQLite ledger through
          context_ledger.store.Store (same binding, path, integrity and schema checks).
  remote  no binding and CONTEXT_LEDGER_MCP_TOKEN set: MCP `get` on the Mini serve-http
          (CONTEXT_LEDGER_MCP_URL, default below) for the decision ids named by
          `start --found` and `--decision`, following the decision_ids of applied events.
          MCP has no read by task_id, so this only sees applied events on those decisions.
  else    FAIL, exit 2. No fallback.

DATA is --data, else CONTEXT_LEDGER_DATA, else ~/.context-ledger (same as the launcher).
The ledger records no lookup event, so `start` appends to DATA/ledger-loop/starts.jsonl
(override LEDGER_LOOP_LOG).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://gurusharan-mac-codex.taild2e98e.ts.net/mcp"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FINAL = {"success", "failed", "inconclusive", "abandoned"}
USABLE_EVIDENCE = {"unverified", "reported_verified"}
OUTCOME_FIX = (
    "append_event outcome on {ids} with task_id {tid}, status success|failed|inconclusive, "
    "an evidence ref and applied_event_id, then rerun close"
)


class Fail(Exception):
    def __init__(self, line: str, code: int = 1) -> None:
        super().__init__(line)
        self.line = line
        self.code = code


def _uuid(value: str, what: str) -> str:
    if not UUID_RE.fullmatch(value):
        raise Fail(f"FAIL bad {what} {value!r}; fix: pass the lowercase UUID the ledger uses")
    return value


def data_dir(arg: str | None) -> Path:
    raw = arg or os.environ.get("CONTEXT_LEDGER_DATA") or str(Path.home() / ".context-ledger")
    return Path(raw).expanduser()


def start_log(data: Path) -> Path:
    env = os.environ.get("LEDGER_LOOP_LOG")
    return Path(env) if env else data / "ledger-loop" / "starts.jsonl"


def read_starts(log: Path, task_id: str) -> list[dict]:
    if not log.exists():
        return []
    rows = []
    with log.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise Fail(f"FAIL {task_id}: {log}:{n} is not JSON ({exc.msg}); fix: repair or move that line", 2)
            if row.get("task_id") == task_id and row.get("ev") == "start":
                rows.append(row)
    return rows


# ---- store backend: the bound SQLite ledger via the plugin's own Store -------------------

def plugin_root() -> Path:
    env = os.environ.get("CONTEXT_LEDGER_PLUGIN_ROOT")
    return Path(env) if env else Path(__file__).resolve().parent.parent


def store_events(data: Path, task_id: str) -> tuple[list[dict], str]:
    root = plugin_root()
    if not (root / "context_ledger" / "store.py").is_file():
        raise Fail(
            f"FAIL {task_id}: {data / 'binding.json'} exists but context_ledger is not at {root}; "
            "fix: run this script from plugins/context-ledger/scripts or set CONTEXT_LEDGER_PLUGIN_ROOT",
            2,
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import sqlite3

    from context_ledger.contracts import LedgerError, parse_json
    from context_ledger.store import Store

    where = str(data / "binding.json")
    store = None
    try:
        store = Store(data, actor_channel="owner_cli")
        where = str(store.db_path)
        conn = store.open()
        store._guard(conn)  # schema version, ledger_id, maintenance: same gate as every tool call
        rows = conn.execute(
            "select event_json from events "
            "where json_extract(event_json, '$.payload.task_id') = ? order by seq",
            (task_id,),
        ).fetchall()
        events = [parse_json(row["event_json"]) for row in rows]
    except LedgerError as exc:
        detail = exc.code + (f": {exc.message}" if exc.message else "")
        raise Fail(f"FAIL {task_id}: ledger unreadable at {where} ({detail}); fix: run context_ledger.py doctor", 2)
    except sqlite3.Error as exc:
        raise Fail(f"FAIL {task_id}: ledger unreadable at {where} (sqlite: {exc}); fix: run context_ledger.py doctor", 2)
    finally:
        if store is not None:
            store.close()
    keep = ("event_id", "event_type", "decision_id", "revision", "payload")
    return [{k: e[k] for k in keep} for e in events if e["event_type"] in {"applied", "outcome"}], f"store {where}"


# ---- remote backend: MCP over HTTP to the Mini serve-http --------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class Mcp:
    def __init__(self, url: str, token: str, task_id: str) -> None:
        self.url, self.token, self.task_id = url, token, task_id
        self.session: dict[str, str] = {}
        self.next_id = 0
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _fail(self, why: str) -> Fail:
        return Fail(f"FAIL {self.task_id}: ledger unreadable at {self.url} ({why}); fix: check the Mini serve-http and CONTEXT_LEDGER_MCP_TOKEN", 2)

    def _post(self, msg: dict) -> dict | None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.session,
        }
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=float(os.environ.get("CONTEXT_LEDGER_MCP_TIMEOUT") or 30)) as resp:
                status, hdrs, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, hdrs, body = exc.code, exc.headers, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise self._fail(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")
        for name in ("Mcp-Session-Id", "Mcp-Protocol-Version"):
            if hdrs is not None and hdrs.get(name):
                self.session[name] = hdrs.get(name)
        if status == 401:
            raise self._fail("UNAUTHORIZED")
        if not 200 <= status < 300:
            raise self._fail(f"HTTP {status}")
        if "id" not in msg or status == 202 or not body.strip():
            return None
        text = body.decode("utf-8")
        if "text/event-stream" in (hdrs.get("Content-Type", "") if hdrs else ""):
            msgs = [json.loads(line[5:].strip()) for line in text.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
        else:
            parsed = json.loads(text)
            msgs = parsed if isinstance(parsed, list) else [parsed]
        for item in msgs:
            if isinstance(item, dict) and item.get("id") == msg["id"]:
                if "error" in item:
                    raise self._fail(f"JSON-RPC error {item['error']}")
                return item["result"]
        raise self._fail("no JSON-RPC response")

    def _req(self, method: str, params: dict) -> dict:
        self.next_id += 1
        result = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        assert result is not None
        return result

    def open(self) -> None:
        self._req("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ledger_loop", "version": "1"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def get(self, decision_id: str) -> dict:
        result = self._req("tools/call", {"name": "get", "arguments": {"decision_id": decision_id}})
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads(result["content"][0]["text"])
        if not payload.get("ok"):
            raise self._fail(f"get {decision_id}: {payload.get('error')}")
        record = payload["data"]["record"]
        if "events" not in record:
            raise self._fail(f"get {decision_id} returned no events; server is older than context-ledger 0.1.18")
        return record


def remote_events(task_id: str, candidates: list[str], token: str) -> tuple[list[dict], str]:
    url = (os.environ.get("CONTEXT_LEDGER_MCP_URL") or "").strip() or DEFAULT_URL
    client = Mcp(url, token, task_id)
    client.open()
    events: list[dict] = []
    seen: set[str] = set()
    queue = list(candidates)
    while queue:
        decision_id = queue.pop(0)
        if decision_id in seen:
            continue
        seen.add(decision_id)
        for ev in client.get(decision_id)["events"]:
            if ev["payload"].get("task_id") != task_id:
                continue
            events.append({**ev, "decision_id": decision_id})
            if ev["event_type"] == "applied":
                queue.extend(ev["payload"]["decision_ids"])
    return events, f"remote {url}, {len(seen)} decision(s) read; MCP has no by-task read"


# ---- check --------------------------------------------------------------------------------

def check(task_id: str, events: list[dict]) -> tuple[int, int]:
    applied: list[str] = []
    for ev in events:
        if ev["event_type"] == "applied":
            applied += [d for d in ev["payload"]["decision_ids"] if d not in applied]
    latest: dict[str, dict] = {}
    for ev in sorted((e for e in events if e["event_type"] == "outcome"), key=lambda e: e["revision"]):
        latest[ev["decision_id"]] = ev["payload"]
    missing = [d for d in applied if d not in latest]
    if missing:
        ids = ",".join(missing)
        raise Fail(f"FAIL {task_id}: no outcome for applied {ids}; fix: " + OUTCOME_FIX.format(ids=ids, tid=task_id))
    pending = [d for d in applied if latest[d]["status"] not in FINAL]
    if pending:
        ids = ",".join(f"{d}={latest[d]['status']}" for d in pending)
        raise Fail(f"FAIL {task_id}: outcome not final for {ids}; fix: " + OUTCOME_FIX.format(ids=",".join(pending), tid=task_id))
    bare = [d for d in applied if not any(e["state"] in USABLE_EVIDENCE for e in latest[d]["evidence"])]
    if bare:
        ids = ",".join(bare)
        raise Fail(f"FAIL {task_id}: outcome without evidence for {ids}; fix: " + OUTCOME_FIX.format(ids=ids, tid=task_id))
    linked = sum(1 for d in applied if latest[d].get("applied_event_id"))
    return len(applied), linked


def cmd_start(o: argparse.Namespace, data: Path) -> str:
    task_id = _uuid(o.task_id, "task_id")
    if not o.query.strip():
        raise Fail(f"FAIL {task_id}: empty query; fix: name the problem happening now")
    if not o.bot.strip():
        raise Fail(f"FAIL {task_id}: empty --bot; fix: name the bot doing the task")
    found = [_uuid(i.strip(), "--found id") for i in o.found.split(",") if i.strip()]
    log = start_log(data)
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    row = {"task_id": task_id, "ev": "start", "bot": o.bot, "query": o.query, "found": found,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return f"start logged {task_id} -> {log}"


def cmd_close(o: argparse.Namespace, data: Path) -> str:
    task_id = _uuid(o.task_id, "task_id")
    extra = [_uuid(d, "--decision id") for d in o.decision]
    log = start_log(data)
    starts = read_starts(log, task_id)
    if not starts:
        raise Fail(
            f"FAIL {task_id}: no start lookup in {log}; fix: run Context-ledger find for the problem now, "
            f"then `ledger_loop.py start {task_id} --bot NAME --query \"...\" --found ids`"
        )
    token = os.environ.get("CONTEXT_LEDGER_MCP_TOKEN", "").strip()
    if (data / "binding.json").exists():
        events, source = store_events(data, task_id)
    elif token:
        found = [d for row in starts for d in row.get("found", [])]
        events, source = remote_events(task_id, list(dict.fromkeys(found + extra)), token)
    else:
        raise Fail(
            f"FAIL {task_id}: no ledger to read: {data / 'binding.json'} missing and CONTEXT_LEDGER_MCP_TOKEN empty; "
            "fix: run on the Mini (bound ledger) or export CONTEXT_LEDGER_MCP_TOKEN",
            2,
        )
    n, linked = check(task_id, events)
    return f"PASS {task_id}: lookup done, {n} applied, all with final outcome + evidence ({linked} linked by applied_event_id) [{source}]"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", help="ledger data dir (default CONTEXT_LEDGER_DATA or ~/.context-ledger)")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("start", help="record the task-start ledger lookup")
    x.add_argument("task_id")
    x.add_argument("--bot", required=True)
    x.add_argument("--query", required=True)
    x.add_argument("--found", default="", help="comma-separated decision ids the lookup returned")
    x = sub.add_parser("close", help="fail unless lookup done and every applied id has an outcome")
    x.add_argument("task_id")
    x.add_argument("--decision", action="append", default=[], help="extra decision id to read (remote mode)")
    o = p.parse_args(argv)
    try:
        print(cmd_start(o, data_dir(o.data)) if o.cmd == "start" else cmd_close(o, data_dir(o.data)))
    except Fail as exc:
        print(exc.line, file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
