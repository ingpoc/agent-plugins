#!/usr/bin/env python3
"""Context Ledger loop: gated, never-blocking find/save plus the closeout check (Guru 2026-10-17).

Gate first. Use the ledger only for critical, recurring, research or debugging tasks, or to
record a Guru override. Simple or one-shot tasks skip it: `close <task_id> --gated-out <word>`.

  ledger_loop.py [--data DIR] find  <task_id> --bot NAME --query "<problem now>" [--audit]
  ledger_loop.py [--data DIR] save  <task_id> --bot NAME --type user_decision|agent_conclusion
                 --decision "<=15 words" --why "<=20 words" --entity E [--entity E] [--crit 1-5]
                 [--rule R] [--exception X] [--state S] [--override-by guru] [--approver A] [--outcome success|failed|partial|pending]
                 [--precedent ID] [--input I] [--audit]
  ledger_loop.py [--data DIR] start <task_id> --bot NAME --query "<q>" [--found id,id | --lookup-failed CODE]
  ledger_loop.py [--data DIR] close <task_id> [--bot NAME] [--decision id ...] [--gated-out WORD]
                 [--repeat-of "<id or topic>: <reason>"] [--guru-correction "<reason>"] [--audit]

A/B (Guru 2026-10-17): from AB_START to AB_END (IST), find is ON on odd calendar days and OFF on
even days; saves and outcomes stay on. The MCP server applies the same rule, so connector finds
follow it too. LEDGER_AB_DATE=YYYY-MM-DD overrides today for dry runs. Every close appends one
task row to DATA/ledger-loop/tasks.jsonl (LEDGER_TASK_LOG); scripts/ledger_stats.py reads it.

Never block: find and save use a LEDGER_TIMEOUT deadline (default 2 s). A slow or unreachable
ledger prints one `WARN ...; continuing without the ledger` line and exits 0, so the task goes on.
A lookup that failed is logged as `lookup: failed:<code>`, never as an empty found list. Auth
errors (AUTH_MISSING / AUTH_INVALID) are named in that line. `--audit` (or LEDGER_AUDIT=1) turns
every warning into a loud FAIL (exit 2) for audits.

`close` prints one PASS line (exit 0) or one FAIL line with the fix (exit 1; exit 2 = ledger
unreadable in audit mode, or bad auth). PASS needs either --gated-out WORD, or:
  1. a find/start lookup recorded for a real task_id (UUID, not a placeholder), and
  2. for every decision id in that task's `applied` events, a latest task outcome (outcome
     event with this task_id) whose status is final and that cites evidence.

Backends: DATA/binding.json -> the bound SQLite ledger via context_ledger.store.Store;
else CONTEXT_LEDGER_MCP_TOKEN and CONTEXT_LEDGER_MCP_URL -> MCP over HTTP.
A token with no URL is not sent. Otherwise the ledger is unavailable (WARN, or FAIL in audit mode).
DATA is --data, else CONTEXT_LEDGER_DATA, else ~/.context-ledger. Lookups are logged to
DATA/ledger-loop/starts.jsonl (override LEDGER_LOOP_LOG).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import uuid as uuidlib
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FINAL = {"success", "failed", "inconclusive", "abandoned"}
USABLE_EVIDENCE = {"unverified", "reported_verified"}
OUTCOME_FIX = (
    "append_event outcome on {ids} with task_id {tid}, status success|failed|inconclusive, "
    "an evidence ref, applied_event_id and helpful true|false (false: effect not_helped|misled + reason), then rerun close"
)


PLACEHOLDER_IDS = {
    "00000000-0000-0000-0000-000000000000",
    "12345678-1234-1234-1234-123456789abc",
    "12345678-1234-5678-1234-567812345678",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
}
WORD_RE = re.compile(r"^[a-z][a-z-]{1,23}$")
IST = timezone(timedelta(hours=5, minutes=30))
AB_START, AB_END = date(2026, 10, 18), date(2026, 10, 31)
FLAG_WORDS = 20


def ab_today() -> date:
    raw = os.environ.get("LEDGER_AB_DATE", "").strip()
    return date.fromisoformat(raw) if raw else datetime.now(IST).date()


def ab_arm(day: date) -> str:
    """on|off inside the A/B window (odd IST day = on), none outside it (find stays on)."""
    if not AB_START <= day <= AB_END:
        return "none"
    return "on" if day.day % 2 else "off"


def require_mcp_url() -> str:
    """URL is required before a bearer token is sent. There is no default host."""
    url = (os.environ.get("CONTEXT_LEDGER_MCP_URL") or "").strip()
    if not url:
        raise Unavailable(
            "UNBOUND",
            "CONTEXT_LEDGER_MCP_TOKEN is set but CONTEXT_LEDGER_MCP_URL is empty; the token is not sent",
        )
    return url


def timeout_s() -> float:
    raw = os.environ.get("LEDGER_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else 2.0
    except ValueError:
        value = 2.0
    return value if value > 0 else 2.0


def audit_mode(o: argparse.Namespace) -> bool:
    return bool(getattr(o, "audit", False)) or os.environ.get("LEDGER_AUDIT", "") == "1"


class Unavailable(Exception):
    """The ledger was slow, unreachable or unbound. Fail open unless in audit mode."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class Fail(Exception):
    def __init__(self, line: str, code: int = 1) -> None:
        super().__init__(line)
        self.line = line
        self.code = code


def _uuid(value: str, what: str) -> str:
    if not UUID_RE.fullmatch(value):
        raise Fail(f"FAIL bad {what} {value!r}; fix: pass the lowercase UUID the ledger uses")
    return value


def _task_id(value: str) -> str:
    """A real task id: the task's own UUID, not a placeholder or a hand-typed pattern."""
    _uuid(value, "task_id")
    hexes = value.replace("-", "")
    if value in PLACEHOLDER_IDS or len(set(hexes)) <= 2:
        raise Fail(f"FAIL placeholder task_id {value}; fix: pass the real task UUID (the task's own id)")
    return value


def data_dir(arg: str | None) -> Path:
    raw = arg or os.environ.get("CONTEXT_LEDGER_DATA") or str(Path.home() / ".context-ledger")
    return Path(raw).expanduser()


def start_log(data: Path) -> Path:
    env = os.environ.get("LEDGER_LOOP_LOG")
    return Path(env) if env else data / "ledger-loop" / "starts.jsonl"


def task_log(data: Path) -> Path:
    env = os.environ.get("LEDGER_TASK_LOG")
    return Path(env) if env else data / "ledger-loop" / "tasks.jsonl"


def read_starts(log: Path, task_id: str, evs: tuple[str, ...] = ("start",)) -> list[dict]:
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
            if row.get("task_id") == task_id and row.get("ev") in evs:
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
        for row in conn.execute("select src, dst, kind from fix_links where task_id=? order by src", (task_id,)):
            events.append({"event_type": "fix", "decision_id": row["dst"], "revision": 0,
                           "payload": {"task_id": task_id, "fix_id": row["src"], "kind": row["kind"]}})
    except LedgerError as exc:
        detail = exc.code + (f": {exc.message}" if exc.message else "")
        if exc.code == "UNAVAILABLE":
            raise Unavailable(exc.code, f"ledger unreadable at {where} ({detail})")
        raise Fail(f"FAIL {task_id}: ledger unreadable at {where} ({detail}); fix: run context_ledger.py doctor", 2)
    except sqlite3.Error as exc:
        raise Fail(f"FAIL {task_id}: ledger unreadable at {where} (sqlite: {exc}); fix: run context_ledger.py doctor", 2)
    finally:
        if store is not None:
            store.close()
    keep = ("event_id", "event_type", "decision_id", "revision", "payload")
    return [{k: e.get(k) for k in keep} for e in events if e["event_type"] in {"applied", "outcome", "fix"}], f"store {where}"


# ---- remote backend: MCP over HTTP to the Mini serve-http --------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class Mcp:
    def __init__(self, url: str, token: str, task_id: str, timeout: float | None = None) -> None:
        self.url, self.token, self.task_id = url, token, task_id
        self.session: dict[str, str] = {}
        self.next_id = 0
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.deadline = None if timeout is None else time.monotonic() + timeout

    def _fail(self, why: str) -> Fail:
        return Fail(f"FAIL {self.task_id}: ledger unreadable at {self.url} ({why}); fix: check the Mini serve-http and CONTEXT_LEDGER_MCP_TOKEN", 2)

    def _left(self) -> float:
        if self.deadline is None:
            return float(os.environ.get("CONTEXT_LEDGER_MCP_TIMEOUT") or 30)
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Unavailable("TIMEOUT", f"no answer from {self.url} within {timeout_s():g}s")
        return left

    def _post(self, msg: dict) -> dict | None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.session,
        }
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=self._left()) as resp:
                status, hdrs, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, hdrs, body = exc.code, exc.headers, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            code = "TIMEOUT" if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError) or "timed out" in str(reason) else "UNREACHABLE"
            raise Unavailable(code, f"{self.url} {code.lower()} ({type(exc).__name__}: {reason})")
        for name in ("Mcp-Session-Id", "Mcp-Protocol-Version"):
            if hdrs is not None and hdrs.get(name):
                self.session[name] = hdrs.get(name)
        if status == 401:
            try:
                err = json.loads(body.decode("utf-8")).get("error") or {}
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                err = {}
            raise self._fail(f"UNAUTHORIZED {err.get('code', 'AUTH_INVALID')}: {err.get('message', 'check CONTEXT_LEDGER_MCP_TOKEN')}")
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

    def tool(self, name: str, arguments: dict) -> dict:
        result = self._req("tools/call", {"name": name, "arguments": arguments})
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads(result["content"][0]["text"])
        if not payload.get("ok"):
            err = payload.get("error") or {}
            raise Fail(f"FAIL {self.task_id}: {name} rejected: {err.get('code')} {err.get('message', '')}".rstrip()
                       + "; fix: correct the arguments named above")
        return payload["data"]

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
    url = require_mcp_url()
    client = Mcp(url, token, task_id, timeout=timeout_s() * 3)
    client.open()
    events: list[dict] = []
    seen: set[str] = set()
    queue = list(candidates)
    while queue:
        decision_id = queue.pop(0)
        if decision_id in seen:
            continue
        seen.add(decision_id)
        record = client.get(decision_id)
        for fix in record.get("fixed_by") or []:
            if fix.get("task_id") == task_id:
                events.append({"event_type": "fix", "decision_id": decision_id, "revision": 0,
                               "payload": {"task_id": task_id, "fix_id": fix["id"], "kind": fix["kind"]}})
        for ev in record["events"]:
            if ev["payload"].get("task_id") != task_id:
                continue
            events.append({**ev, "decision_id": decision_id})
            if ev["event_type"] == "applied":
                queue.extend(ev["payload"]["decision_ids"])
    return events, f"remote {url}, {len(seen)} decision(s) read; MCP has no by-task read"


# ---- check --------------------------------------------------------------------------------

def check(task_id: str, events: list[dict]) -> dict:
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
    unmarked = [d for d in applied if type(latest[d].get("helpful")) is not bool]
    if unmarked:
        ids = ",".join(unmarked)
        raise Fail(f"FAIL {task_id}: outcome without helpful true|false for {ids}; fix: " + OUTCOME_FIX.format(ids=ids, tid=task_id))
    vague = [d for d in applied if latest[d].get("helpful") is False
             and (latest[d].get("effect") not in ("not_helped", "misled") or not latest[d].get("reason"))]
    if vague:
        ids = ",".join(vague)
        raise Fail(f"FAIL {task_id}: helpful false without effect not_helped|misled and a reason for {ids}; "
                   "fix: append the outcome again with effect and a reason (<=20 words)")
    fixed = {e["decision_id"] for e in events if e["event_type"] == "fix"}
    unfixed = [d for d in applied if latest[d].get("helpful") is False and latest[d]["status"] == "success" and d not in fixed]
    return {
        "applied": len(applied),
        "linked": sum(1 for d in applied if latest[d].get("applied_event_id")),
        "helpful": sum(1 for d in applied if latest[d].get("helpful") is True),
        "misled": sum(1 for d in applied if latest[d].get("effect") == "misled"),
        "not_helped": sum(1 for d in applied if latest[d].get("effect") == "not_helped"),
        "unfixed": unfixed,
        "applied_ids": applied,
        "helpful_ids": [d for d in applied if latest[d].get("helpful") is True],
    }


def _log_row(data: Path, row: dict) -> Path:
    log = start_log(data)
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    row = {**row, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return log


def _bot(o: argparse.Namespace, task_id: str) -> str:
    if not o.bot.strip():
        raise Fail(f"FAIL {task_id}: empty --bot; fix: name the bot doing the task")
    return o.bot.strip().lower()


def cmd_start(o: argparse.Namespace, data: Path) -> str:
    task_id = _task_id(o.task_id)
    if not o.query.strip():
        raise Fail(f"FAIL {task_id}: empty query; fix: name the problem happening now")
    bot = _bot(o, task_id)
    found = [_uuid(i.strip(), "--found id") for i in o.found.split(",") if i.strip()]
    row = {"task_id": task_id, "ev": "start", "bot": bot, "query": o.query, "found": found,
           "arm": ab_arm(ab_today()), "chars": o.chars or 0,
           "trusted": [_uuid(i.strip(), "--trusted id") for i in (o.trusted or "").split(",") if i.strip()]}
    if o.lookup_failed:
        if found:
            raise Fail(f"FAIL {task_id}: --found and --lookup-failed together; fix: pass one")
        row = {**row, "lookup": f"failed:{o.lookup_failed}"}
        del row["found"]
    log = _log_row(data, row)
    return f"start logged {task_id} -> {log}"


def _call(data: Path, task_id: str, name: str, arguments: dict) -> tuple[dict, str]:
    """One ledger tool call under the never-block deadline."""
    if (data / "binding.json").exists():
        root = plugin_root()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from context_ledger.contracts import LedgerError
        from context_ledger.store import Store
        store = None
        try:
            store = Store(data, actor_channel="agent")
            fn = {"find": store.find, "save": store.save}[name]
            return fn(arguments, mcp=True), f"store {store.db_path}"
        except LedgerError as exc:
            if exc.code in {"UNAVAILABLE", "BUSY", "MAINTENANCE", "CORRUPT_LEDGER"}:
                raise Unavailable(exc.code, exc.message or exc.code)
            raise Fail(f"FAIL {task_id}: {name} rejected: {exc.code} {exc.message or ''}".rstrip()
                       + "; fix: correct the arguments named above")
        finally:
            if store is not None:
                store.close()
    token = os.environ.get("CONTEXT_LEDGER_MCP_TOKEN", "").strip()
    if not token:
        raise Unavailable("UNBOUND", f"no {data / 'binding.json'} and CONTEXT_LEDGER_MCP_TOKEN is empty")
    url = require_mcp_url()
    client = Mcp(url, token, task_id, timeout=timeout_s())
    try:
        # serve-http answers a stateless tools/call: one round trip keeps the box under the deadline.
        return client.tool(name, arguments), f"remote {url}"
    except Fail as exc:
        text = str(exc)
        if "UNAUTHORIZED" in text or " rejected: " in text or not ("HTTP 4" in text or "JSON-RPC error" in text):
            raise
    client.open()
    return client.tool(name, arguments), f"remote {url}"


def _warn(o: argparse.Namespace, task_id: str, what: str, exc: Unavailable) -> str:
    line = f"{exc.code} {exc.detail}"
    if audit_mode(o):
        raise Fail(f"FAIL {task_id}: ledger {what} failed ({line}); fix: audit mode needs a reachable ledger", 2)
    print(f"WARN ledger {what} skipped ({line}); continuing without the ledger", file=sys.stderr)
    return exc.code


def _binding_caller_scope(data: Path) -> dict | None:
    """Project and tools from binding.json. Absent binding means the find stays fleet-only."""
    path = data / "binding.json"
    if not path.is_file():
        return None
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(binding, dict):
        return None
    root = plugin_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from context_ledger.contracts import caller_scope_from_binding, read_project_registry
    except ImportError:
        return None
    return caller_scope_from_binding(binding, read_project_registry(data))


def cmd_find(o: argparse.Namespace, data: Path) -> str:
    task_id = _task_id(o.task_id)
    if not o.query.strip():
        raise Fail(f"FAIL {task_id}: empty query; fix: name the problem happening now")
    bot = _bot(o, task_id)
    arm = ab_arm(ab_today())
    if arm == "off":
        _log_row(data, {"task_id": task_id, "ev": "start", "bot": bot, "query": o.query, "lookup": "arm_off", "arm": arm, "ms": 0})
        return json.dumps({"matches": [], "arm": "off", "note": "A/B OFF day: no lookup; saves and outcomes stay on"})
    started = time.monotonic()
    find_args: dict = {"query": o.query, "actor": bot, "task_id": task_id}
    caller_scope = _binding_caller_scope(data)
    if caller_scope is not None:
        find_args["caller_scope"] = caller_scope
    try:
        result, _source = _call(data, task_id, "find", find_args)
    except Unavailable as exc:
        code = _warn(o, task_id, "find", exc)
        _log_row(data, {"task_id": task_id, "ev": "start", "bot": bot, "query": o.query, "lookup": f"failed:{code}",
                        "arm": arm, "ms": int((time.monotonic() - started) * 1000)})
        return json.dumps({"matches": [], "lookup": f"failed:{code}"})
    ids = [m["decision_id"] for m in result["matches"]]
    ids += [r["id"] for m in result["matches"] for r in m.get("related", [])]
    out = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    trusted = [m["decision_id"] for m in result["matches"] if m.get("tier") == "trusted"]
    _log_row(data, {"task_id": task_id, "ev": "start", "bot": bot, "query": o.query, "found": ids, "arm": arm,
                    "trusted": trusted,
                    "chars": len(out), "ms": int((time.monotonic() - started) * 1000)})
    return out


def cmd_save(o: argparse.Namespace, data: Path) -> str:
    task_id = _task_id(o.task_id)
    bot = _bot(o, task_id)
    args: dict = {
        "request_id": str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"ledger-loop:{task_id}:{o.decision}")),
        "type": o.type, "decision": o.decision, "why": o.why, "entities": o.entity,
        "scope": o.scope, "task_id": task_id, "actor": bot,
    }
    for key in ("rule", "exception", "state", "outcome", "approver", "override_by"):
        if getattr(o, key):
            args[key] = getattr(o, key)
    if o.crit is not None:
        args["criticality"] = o.crit
    if o.precedent:
        args["precedent_ids"] = o.precedent
    if o.input:
        args["inputs"] = o.input
    for key in ("supersedes", "context"):
        if getattr(o, key):
            args[key] = getattr(o, key)
    started = time.monotonic()
    chars = len(json.dumps(args, ensure_ascii=False))
    try:
        receipt, _source = _call(data, task_id, "save", args)
    except Unavailable as exc:
        code = _warn(o, task_id, "save", exc)
        _log_row(data, {"task_id": task_id, "ev": "save", "bot": bot, "lookup": f"failed:{code}", "chars": chars,
                        "ms": int((time.monotonic() - started) * 1000)})
        return json.dumps({"saved": False, "reason": f"failed:{code}"})
    _log_row(data, {"task_id": task_id, "ev": "save", "bot": bot, "decision_id": receipt["decision_id"], "chars": chars,
                    "ms": int((time.monotonic() - started) * 1000)})
    return json.dumps({"saved": True, **receipt})


def _flag(task_id: str, name: str, value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or "\n" in text or len(text.split()) > FLAG_WORDS:
        raise Fail(f"FAIL {task_id}: --{name} needs one line of {FLAG_WORDS} words or fewer; fix: shorten it")
    return text


def _task_row(data: Path, task_id: str, o: argparse.Namespace, gate: str, stats: dict | None, fail_open: bool) -> None:
    """One mechanical A/B row per closed task; ledger_stats.py reads these."""
    rows = read_starts(start_log(data), task_id, ("start", "save"))
    starts = [r for r in rows if r["ev"] == "start"]
    saves = [r for r in rows if r["ev"] == "save"]
    actor = (o.bot or "").strip().lower() or next((r["bot"] for r in starts + saves if r.get("bot")), "unknown")
    found = {d for r in starts for d in r.get("found", [])}
    trusted = {d for r in starts for d in r.get("trusted", [])}
    chars = sum(int(r.get("chars") or 0) for r in rows)
    failed = any(str(r.get("lookup", "")).startswith("failed:") for r in rows)
    stats = stats or {}
    _row = {
        "ev": "task", "task_id": task_id, "date": ab_today().isoformat(), "arm": ab_arm(ab_today()),
        "actor": actor, "gate": gate, "gated": gate == "in",
        "find_ran": any("found" in r for r in starts), "hits": len(found),
        "helpful": stats.get("helpful", 0), "misled": stats.get("misled", 0),
        "not_helped": stats.get("not_helped", 0), "unfixed": len(stats.get("unfixed", [])),
        "trusted_applied": sum(1 for d in stats.get("applied_ids", []) if d in trusted),
        "trusted_helpful": sum(1 for d in stats.get("helpful_ids", []) if d in trusted),
        "tokens_est": chars // 4, "tokens_unit": "est chars/4 of find replies + save requests",
        "latency_ms": sum(int(r.get("ms") or 0) for r in rows),
        "fail_open": failed or fail_open,
        "writes": sum(1 for r in saves if r.get("decision_id")),
        "repeat_of": _flag(task_id, "repeat-of", o.repeat_of),
        "guru_correction": _flag(task_id, "guru-correction", o.guru_correction),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    log = task_log(data)
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_row) + "\n")


def cmd_close(o: argparse.Namespace, data: Path) -> str:
    task_id = _task_id(o.task_id)
    _flag(task_id, "repeat-of", o.repeat_of)
    _flag(task_id, "guru-correction", o.guru_correction)
    if o.gated_out:
        if not WORD_RE.fullmatch(o.gated_out):
            raise Fail(f"FAIL {task_id}: --gated-out needs one lowercase word (simple, one-shot, routine); fix: pass one word")
        _log_row(data, {"task_id": task_id, "ev": "gated_out", "reason": o.gated_out})
        _task_row(data, task_id, o, o.gated_out, None, False)
        return f"PASS {task_id}: gated out ({o.gated_out}); no ledger lookup needed"
    extra = [_uuid(d, "--decision id") for d in o.decision]
    log = start_log(data)
    starts = read_starts(log, task_id)
    arm = ab_arm(ab_today())
    if not starts and arm != "off":
        raise Fail(
            f"FAIL {task_id}: no start lookup in {log}; fix: run Context-ledger find for the problem now, "
            f"then `ledger_loop.py start {task_id} --bot NAME --query \"...\" --found ids`"
        )
    token = os.environ.get("CONTEXT_LEDGER_MCP_TOKEN", "").strip()
    try:
        if (data / "binding.json").exists():
            events, source = store_events(data, task_id)
        elif token:
            found = [d for row in starts for d in row.get("found", [])]
            events, source = remote_events(task_id, list(dict.fromkeys(found + extra)), token)
        else:
            raise Unavailable(
                "UNBOUND",
                f"no ledger to read: {data / 'binding.json'} missing and CONTEXT_LEDGER_MCP_TOKEN empty",
            )
    except Unavailable as exc:
        if audit_mode(o):
            raise Fail(f"FAIL {task_id}: ledger unreadable ({exc.code} {exc.detail}); fix: run on the Mini (bound ledger) "
                       "or export CONTEXT_LEDGER_MCP_TOKEN", 2)
        _task_row(data, task_id, o, "in", None, True)
        return f"PASS {task_id}: closeout check skipped, ledger unavailable ({exc.code}); WARN fail-open, task continues"
    stats = check(task_id, events)
    _task_row(data, task_id, o, "in", stats, False)
    line = (f"PASS {task_id}: lookup {'off' if arm == 'off' and not starts else 'done'}, {stats['applied']} applied, "
            f"all with final outcome + evidence ({stats['linked']} linked by applied_event_id) [{source}] arm={arm}")
    if stats["unfixed"]:
        ids = ",".join(stats["unfixed"])
        line += (f"; WARN {len(stats['unfixed'])} not_helped without a fix ({ids}): "
                 f"`ledger_loop.py save {task_id} ... --supersedes {stats['unfixed'][0]} --context \"<where it failed>\"`")
    return line


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", help="ledger data dir (default CONTEXT_LEDGER_DATA or ~/.context-ledger)")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("find", help="gated lookup: one find, 2 s deadline, fail-open")
    x.add_argument("task_id")
    x.add_argument("--bot", required=True)
    x.add_argument("--query", required=True)
    x.add_argument("--audit", action="store_true")
    x = sub.add_parser("save", help="capture one compact trace, 2 s deadline, fail-open")
    x.add_argument("task_id")
    x.add_argument("--bot", required=True)
    x.add_argument("--type", required=True, choices=["user_decision", "agent_conclusion"])
    x.add_argument("--decision", required=True)
    x.add_argument("--why", required=True)
    x.add_argument("--scope", required=True, help="fleet, project:<id>, or tool:<id>. Still true in another project?")
    x.add_argument("--entity", action="append", default=[], required=True)
    x.add_argument("--crit", type=int)
    for flag in ("--rule", "--exception", "--state", "--approver", "--override-by"):
        x.add_argument(flag)
    x.add_argument("--outcome", choices=["success", "failed", "partial", "pending"])
    x.add_argument("--precedent", action="append", default=[])
    x.add_argument("--input", action="append", default=[])
    x.add_argument("--supersedes", help="decision id this fix replaces in --context")
    x.add_argument("--context", help="<=10 words: where the old decision did not help")
    x.add_argument("--audit", action="store_true")
    x = sub.add_parser("start", help="record a lookup made with the connector find")
    x.add_argument("task_id")
    x.add_argument("--bot", required=True)
    x.add_argument("--query", required=True)
    x.add_argument("--found", default="", help="comma-separated decision ids the lookup returned")
    x.add_argument("--trusted", default="", help="comma-separated ids the lookup returned with tier trusted")
    x.add_argument("--chars", type=int, help="characters of the find reply (token estimate = chars/4)")
    x.add_argument("--lookup-failed", dest="lookup_failed", help="error code when the connector find failed")
    x = sub.add_parser("close", help="fail unless gated out, or lookup done and every applied id has an outcome")
    x.add_argument("task_id")
    x.add_argument("--decision", action="append", default=[], help="extra decision id to read (remote mode)")
    x.add_argument("--gated-out", dest="gated_out", help="one word: why this task skipped the ledger")
    x.add_argument("--bot", help="actor id when no find/save row names it (OFF days)")
    x.add_argument("--repeat-of", dest="repeat_of", help="<=20 words: the earlier task/decision this re-diagnosed, and why")
    x.add_argument("--guru-correction", dest="guru_correction", help="<=20 words: what Guru had to re-prompt or correct")
    x.add_argument("--audit", action="store_true")
    o = p.parse_args(argv)
    cmds = {"find": cmd_find, "save": cmd_save, "start": cmd_start, "close": cmd_close}
    try:
        print(cmds[o.cmd](o, data_dir(o.data)))
    except Fail as exc:
        print(exc.line, file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
