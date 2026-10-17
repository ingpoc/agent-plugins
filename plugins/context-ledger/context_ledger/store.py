"""SQLite ledger, replay, search, and owner administration."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from context_ledger.contracts import (
    APPEND_TYPES,
    CANDIDATE_CAP,
    EVENT_TYPE,
    MAX_COMBINED_ROWS,
    MAX_CONFLICTS,
    MAX_DB_BYTES,
    MAX_EVENT,
    MAX_EVENTS,
    BROWSE_OFFSET_MAX,
    MAX_FIND_BYTES,
    MAX_FIND_RESPONSE,
    RELATED_CAP,
    MAX_GET_RESPONSE,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_LINES,
    MAX_TOMBSTONE_BYTES,
    SCHEMA_VERSION,
    LedgerError,
    canonical_json,
    fail_dict,
    fts_tokens,
    nfc,
    new_uuid,
    now_ts,
    parse_json,
    request_hash,
    require_enum,
    require_keys,
    require_scope,
    require_str,
    require_ts,
    require_uuid,
    truncate_unicode,
    validate_append_input,
    validate_find_input,
    validate_get_input,
    validate_payload,
    validate_record_input,
    validate_save_input,
)
from context_ledger import ranking

DDL = """
CREATE TABLE ledger_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  ledger_id TEXT NOT NULL UNIQUE,
  maintenance TEXT CHECK(maintenance IN ('purge','rebuild','migrate')),
  maintenance_target TEXT,
  schema_version INTEGER NOT NULL CHECK(schema_version=1)
);
CREATE TABLE scopes (scope_id TEXT PRIMARY KEY);
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  decision_id TEXT NOT NULL,
  scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
  revision INTEGER NOT NULL CHECK(revision>0),
  event_json TEXT NOT NULL CHECK(json_valid(event_json)),
  ingest_kind TEXT NOT NULL CHECK(ingest_kind IN ('local','import')),
  received_at TEXT NOT NULL,
  UNIQUE(decision_id,revision)
);
CREATE INDEX events_scope ON events(scope_id,seq);
CREATE TABLE requests (
  scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
  request_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
  PRIMARY KEY(scope_id,request_id)
);
CREATE TABLE tombstones (
  kind TEXT NOT NULL CHECK(kind IN ('decision','event')),
  id TEXT NOT NULL,
  scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
  decision_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  purged_at TEXT NOT NULL,
  PRIMARY KEY(kind,id)
);
CREATE TABLE current_records (
  decision_id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
  revision INTEGER NOT NULL,
  sensitivity TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  projection_json TEXT NOT NULL CHECK(json_valid(projection_json))
);
CREATE INDEX current_scope ON current_records(scope_id,sensitivity,lifecycle,updated_at);
CREATE VIRTUAL TABLE search_fts USING fts5(decision_id UNINDEXED, text, tokenize='unicode61');
"""

# v2, additive: projections and logs only; events stay the source of truth.
V2_DDL = """
CREATE TABLE IF NOT EXISTS links (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('precedent')),
  PRIMARY KEY(src,dst,kind)
);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE TABLE IF NOT EXISTS fix_links (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('supersedes','exception_for')),
  context TEXT NOT NULL,
  task_id TEXT,
  PRIMARY KEY(src,dst)
);
CREATE INDEX IF NOT EXISTS fix_links_dst ON fix_links(dst);
CREATE TABLE IF NOT EXISTS access_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  tool TEXT NOT NULL,
  caller TEXT NOT NULL,
  task_id TEXT,
  query TEXT,
  n INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  ids TEXT NOT NULL CHECK(json_valid(ids)),
  ms INTEGER NOT NULL
);
"""

WINDOWS_ACL_SCRIPT = r"""
$path = $env:CL_PATH
if (-not $path) { Write-Output '{"ok":false,"error":"missing"}'; exit 2 }
if ($path -match '[\r\n]') { Write-Output '{"ok":false,"error":"invalid"}'; exit 2 }
$item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
  Write-Output '{"ok":false,"error":"reparse"}'; exit 2
}
$acl = Get-Acl -LiteralPath $path
$identities = @()
foreach ($ace in $acl.Access) {
  $identities += [ordered]@{
    sid = $ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    rights = [string]$ace.FileSystemRights
    type = [string]$ace.AccessControlType
  }
}
$payload = [ordered]@{ ok = $true; owner = $acl.Owner; aces = $identities }
$payload | ConvertTo-Json -Compress -Depth 5
"""


def plugin_root_from_here() -> Path:
    return Path(__file__).resolve().parent.parent


def binding_path(plugin_data: Path) -> Path:
    return plugin_data / "binding.json"


def load_binding(plugin_data: Path) -> dict[str, Any]:
    path = binding_path(plugin_data)
    _refuse_symlink(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError("UNAVAILABLE") from exc
    obj = parse_json(raw)
    data = require_keys(
        obj, {"schema_version", "ledger_dir", "ledger_id", "scope_id", "actor_id"}
    )
    if data["schema_version"] != 1:
        raise LedgerError("UPGRADE_REQUIRED")
    ledger_dir = Path(require_str(data["ledger_dir"], 4096))
    if not ledger_dir.is_absolute():
        raise LedgerError("INVALID_ARGUMENT")
    return {
        "schema_version": 1,
        "ledger_dir": str(ledger_dir),
        "ledger_id": require_uuid(data["ledger_id"]),
        "scope_id": require_scope(data["scope_id"]),
        "actor_id": require_scope(data["actor_id"]),
    }


def write_binding(plugin_data: Path, binding: dict[str, Any], *, replace: bool) -> None:
    path = binding_path(plugin_data)
    plugin_data.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise LedgerError("INVALID_TRANSITION")
    _posix_secure_dir(plugin_data)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(canonical_json(binding) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    _posix_secure_file(path)


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink() or (path.exists() and path.is_symlink()):
        raise LedgerError("UNAVAILABLE")
    if os.name == "nt":
        return
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise LedgerError("UNAVAILABLE")


def _posix_check_dir(path: Path) -> None:
    if os.name == "nt":
        _windows_acl_or_raise(path)
        return
    _refuse_symlink(path)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise LedgerError("UNAVAILABLE")


def _posix_check_file(path: Path) -> None:
    if os.name == "nt":
        _windows_acl_or_raise(path)
        return
    _refuse_symlink(path)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise LedgerError("UNAVAILABLE")


def _posix_secure_dir(path: Path) -> None:
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
        _windows_acl_or_raise(path)
        return
    _refuse_symlink(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    _posix_check_dir(path)


def _posix_secure_file(path: Path) -> None:
    if os.name == "nt":
        _windows_acl_or_raise(path)
        return
    os.chmod(path, 0o600)
    _posix_check_file(path)


def _windows_acl_or_raise(path: Path) -> None:
    if os.name != "nt":
        return
    env = os.environ.copy()
    env["CL_PATH"] = str(path)
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                WINDOWS_ACL_SCRIPT,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerError("UNAVAILABLE") from exc
    if proc.returncode != 0:
        raise LedgerError("UNAVAILABLE")
    try:
        payload = parse_json(proc.stdout.strip() or "{}")
    except LedgerError as exc:
        raise LedgerError("UNAVAILABLE") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise LedgerError("UNAVAILABLE")
    allowed_rights = {
        "Read",
        "Write",
        "ReadAndExecute",
        "Modify",
        "FullControl",
        "Synchronize",
        "ReadExtendedAttributes",
        "WriteExtendedAttributes",
        "ReadAttributes",
        "WriteAttributes",
        "ReadData",
        "WriteData",
        "AppendData",
        "Delete",
        "ReadPermissions",
        "ChangePermissions",
        "TakeOwnership",
        "ExecuteFile",
        "ListDirectory",
        "CreateFiles",
        "CreateDirectories",
        "DeleteSubdirectoriesAndFiles",
    }
    # Allowed principals: current user SID, SYSTEM (S-1-5-18), Administrators (S-1-5-32-544)
    try:
        whoami = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        current = whoami.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerError("UNAVAILABLE") from exc
    allowed_sids = {current, "S-1-5-18", "S-1-5-32-544"}
    aces = payload.get("aces")
    if not isinstance(aces, list):
        raise LedgerError("UNAVAILABLE")
    for ace in aces:
        if not isinstance(ace, dict):
            raise LedgerError("UNAVAILABLE")
        if ace.get("type") != "Allow":
            continue
        sid = ace.get("sid")
        if sid not in allowed_sids:
            raise LedgerError("UNAVAILABLE")


def _refuse_network(path: Path) -> Path:
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        raise LedgerError("UNAVAILABLE")
    resolved = path.resolve()
    return resolved


_CHECKIN_RE = re.compile(r"^\s*hourly\b", re.IGNORECASE)
_PLACEHOLDER_TASK_RE = re.compile(r"^(a1b2c3d4|00000000|11111111|12345678|deadbeef)-", re.IGNORECASE)


def _counts_as_use(payload: dict[str, Any]) -> bool:
    """An outcome is a use when it carries a real task id. Status check-ins are not uses:
    an outcome tied to an applied event always counts; an untied one whose note is a
    routine "Hourly ..." check-in (the pre-v2 pattern) does not; placeholder task ids never do."""
    task_id = payload.get("task_id")
    if not task_id or _PLACEHOLDER_TASK_RE.match(task_id):
        return False
    if payload.get("applied_event_id"):
        return True
    return not _CHECKIN_RE.match(payload.get("note") or "")


class Store:
    def __init__(self, plugin_data: Path, *, actor_channel: str = "owner_cli") -> None:
        self.plugin_data = Path(plugin_data)
        self.actor_channel = actor_channel
        self.binding = load_binding(self.plugin_data)
        self.ledger_dir = _refuse_network(Path(self.binding["ledger_dir"]))
        self.db_path = self.ledger_dir / "ledger.sqlite3"
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._reachable = False

    def _unreachable(self, exc: BaseException | None = None) -> LedgerError:
        err = LedgerError(
            "UNAVAILABLE",
            message=f"ledger unreachable at {self.db_path}",
        )
        if exc is not None:
            raise err from exc
        return err

    def probe(self) -> None:
        """Stat the ledger file and query a new connection, not the startup handle."""
        path = self.db_path
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise self._unreachable(exc)
        if not stat.S_ISREG(st.st_mode):
            raise self._unreachable()
        try:
            conn = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            text = str(exc).casefold()
            if "malformed" in text or "not a database" in text or "corrupt" in text:
                raise LedgerError("CORRUPT_LEDGER") from exc
            raise self._unreachable(exc)
        try:
            try:
                row = conn.execute("pragma quick_check").fetchone()
            except sqlite3.Error as exc:
                text = str(exc).casefold()
                if "malformed" in text or "not a database" in text or "corrupt" in text:
                    raise LedgerError("CORRUPT_LEDGER") from exc
                raise self._unreachable(exc)
        finally:
            conn.close()
        if row is None or str(row[0]) != "ok":
            raise LedgerError("CORRUPT_LEDGER")

    def _ensure_reachable(self) -> None:
        if self._reachable:
            return
        last: LedgerError | None = None
        for _attempt in range(2):
            try:
                self.probe()
            except LedgerError as exc:
                if exc.code != "UNAVAILABLE":
                    raise
                last = exc
                continue
            self._reachable = True
            return
        if last is not None and last.message:
            raise last
        raise self._unreachable()

    def open(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        _posix_check_dir(self.ledger_dir)
        _refuse_symlink(self.db_path)
        if not self.db_path.is_file():
            raise LedgerError(
                "UNAVAILABLE",
                message=f"ledger unreachable at {self.db_path}",
            )
        _posix_check_file(self.db_path)
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise LedgerError("CORRUPT_LEDGER") from exc
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        try:
            chk = conn.execute("pragma quick_check").fetchone()
            if chk is None or str(chk[0]) != "ok":
                raise LedgerError("CORRUPT_LEDGER")
        except sqlite3.Error as exc:
            raise LedgerError("CORRUPT_LEDGER") from exc
        self._ensure_v2(conn)
        self.conn = conn
        return conn

    def _ensure_v2(self, conn: sqlite3.Connection) -> None:
        """Additive v2 tables, then backfill links from current projections. Never drops data."""
        try:
            conn.executescript(V2_DDL)
            conn.execute("begin immediate")
            try:
                conn.execute("delete from links")
                conn.execute("delete from fix_links")
                for row in conn.execute("select decision_id, projection_json from current_records").fetchall():
                    self._write_links(conn, row["decision_id"], parse_json(row["projection_json"])["body"])
                conn.execute("commit")
            except BaseException:
                conn.execute("rollback")
                raise
        except sqlite3.Error as exc:
            raise LedgerError("UNAVAILABLE", message=f"v2 tables could not be created in {self.db_path}: {exc}") from exc

    @staticmethod
    def _write_links(conn: sqlite3.Connection, decision_id: str, body: dict[str, Any]) -> None:
        conn.execute("delete from links where src=?", (decision_id,))
        conn.execute("delete from fix_links where src=?", (decision_id,))
        trace = body.get("trace") or {}
        if trace.get("fix_of"):
            conn.execute(
                "insert or replace into fix_links(src,dst,kind,context,task_id) values (?,?,?,?,?)",
                (decision_id, trace["fix_of"], trace["fix_kind"], trace["context"], trace.get("task_id")),
            )
        for dst in (body.get("trace") or {}).get("precedent_ids") or []:
            conn.execute(
                "insert or ignore into links(src,dst,kind) values (?,?,'precedent')", (decision_id, dst)
            )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        conn.execute("pragma foreign_keys=ON")
        conn.execute("pragma journal_mode=DELETE")
        conn.execute("pragma synchronous=FULL")
        conn.execute("pragma secure_delete=ON")
        conn.execute("pragma busy_timeout=5000")

    def _begin(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("begin immediate")
        except sqlite3.OperationalError as exc:
            raise LedgerError("BUSY", retryable=True) from exc
        self._guard(conn)

    def _guard(self, conn: sqlite3.Connection) -> None:
        try:
            user_version = conn.execute("pragma user_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise LedgerError("CORRUPT_LEDGER") from exc
        if user_version != SCHEMA_VERSION:
            raise LedgerError("UPGRADE_REQUIRED")
        row = conn.execute(
            "select ledger_id, maintenance, schema_version from ledger_meta where singleton=1"
        ).fetchone()
        if row is None:
            raise LedgerError("CORRUPT_LEDGER")
        if row["schema_version"] != SCHEMA_VERSION:
            raise LedgerError("UPGRADE_REQUIRED")
        if row["ledger_id"] != self.binding["ledger_id"]:
            raise LedgerError("UNAVAILABLE")
        if row["maintenance"] is not None:
            raise LedgerError("MAINTENANCE", retryable=True)

    def _tx(self):
        conn = self.open()
        return _Txn(self, conn)

    def doctor(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "python": list(sys.version_info[:3]),
            "sqlite_ok": False,
            "binding": True,
            "identity": False,
            "permissions": False,
            "schema": False,
            "quick_check": False,
        }
        with self._lock:
            try:
                self.open()
                data["permissions"] = True
                conn = self.conn
                assert conn is not None
                data["sqlite_ok"] = True
                self._begin(conn)
                data["identity"] = True
                data["schema"] = True
                chk = conn.execute("pragma quick_check").fetchone()
                data["quick_check"] = chk is not None and str(chk[0]) == "ok"
                conn.execute("rollback")
            except LedgerError:
                if self.conn is not None:
                    try:
                        self.conn.execute("rollback")
                    except sqlite3.Error:
                        pass
                raise
        return data

    def scope_add(self, scope_id: str) -> dict[str, Any]:
        scope_id = require_scope(scope_id)
        with self._tx() as conn:
            conn.execute("insert into scopes(scope_id) values (?)", (scope_id,))
        return {"scope_id": scope_id}

    def record(
        self,
        arguments: dict[str, Any],
        *,
        mcp: bool,
        tool: str = "record",
    ) -> dict[str, Any]:
        allow_owner = not mcp
        parsed = validate_record_input(arguments, allow_owner_attested=allow_owner)
        if mcp and parsed["body"]["approval"]["state"] == "owner_attested":
            raise LedgerError("INVALID_ARGUMENT")
        req_hash = request_hash(tool, parsed)
        with self._tx() as conn:
            existing = conn.execute(
                "select request_hash, receipt_json, decision_id from requests where scope_id=? and request_id=?",
                (self.binding["scope_id"], parsed["request_id"]),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != req_hash:
                    raise LedgerError("IDEMPOTENCY_CONFLICT")
                receipt = parse_json(existing["receipt_json"])
                receipt["replayed"] = True
                return receipt
            decision_id = new_uuid()
            event_id = new_uuid()
            recorded = now_ts()
            event = {
                "schema_version": 1,
                "ledger_id": self.binding["ledger_id"],
                "event_id": event_id,
                "decision_id": decision_id,
                "scope_id": self.binding["scope_id"],
                "revision": 1,
                "event_type": "created",
                "recorded_at": recorded,
                "occurred_at": parsed["occurred_at"],
                "provenance": {
                    "actor_id": parsed.get("actor") or self.binding["actor_id"],
                    "channel": "agent" if mcp else self.actor_channel,
                },
                "request_id": parsed["request_id"],
                "expected_revision": 0,
                "payload": {"body": parsed["body"], "outcome": parsed["outcome"]},
            }
            receipt = self._append_event_row(
                conn,
                event,
                ingest_kind="local",
                request_hash=req_hash,
                mcp=mcp,
            )
            return receipt

    def append_event(self, arguments: dict[str, Any], *, mcp: bool, tool: str = "append_event") -> dict[str, Any]:
        parsed = validate_append_input(arguments, allow_owner_attested=not mcp)
        if mcp and parsed["event_type"] not in APPEND_TYPES:
            raise LedgerError("INVALID_ARGUMENT")
        if parsed["event_type"] == "outcome" and "task_id" in parsed["payload"]:
            return self._append_task_outcome(parsed, mcp=mcp, tool=tool)
        req_hash = request_hash(tool, parsed)
        with self._tx() as conn:
            existing = conn.execute(
                "select request_hash, receipt_json from requests where scope_id=? and request_id=?",
                (self.binding["scope_id"], parsed["request_id"]),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != req_hash:
                    raise LedgerError("IDEMPOTENCY_CONFLICT")
                receipt = parse_json(existing["receipt_json"])
                receipt["replayed"] = True
                return receipt
            row = conn.execute(
                "select revision, scope_id, sensitivity from current_records where decision_id=?",
                (parsed["decision_id"],),
            ).fetchone()
            if row is None:
                tomb = conn.execute(
                    "select 1 from tombstones where kind='decision' and id=?",
                    (parsed["decision_id"],),
                ).fetchone()
                raise LedgerError("UNAVAILABLE")
            if row["scope_id"] != self.binding["scope_id"]:
                raise LedgerError("UNAVAILABLE")
            if mcp and row["sensitivity"] != "model_safe":
                raise LedgerError("UNAVAILABLE")
            if row["revision"] != parsed["expected_revision"]:
                raise LedgerError("REVISION_CONFLICT")
            event_id = new_uuid()
            recorded = now_ts()
            event = {
                "schema_version": 1,
                "ledger_id": self.binding["ledger_id"],
                "event_id": event_id,
                "decision_id": parsed["decision_id"],
                "scope_id": self.binding["scope_id"],
                "revision": parsed["expected_revision"] + 1,
                "event_type": parsed["event_type"],
                "recorded_at": recorded,
                "occurred_at": parsed["occurred_at"],
                "provenance": {
                    "actor_id": parsed.get("actor") or self.binding["actor_id"],
                    "channel": "agent" if mcp else self.actor_channel,
                },
                "request_id": parsed["request_id"],
                "expected_revision": parsed["expected_revision"],
                "payload": parsed["payload"],
            }
            return self._append_event_row(
                conn, event, ingest_kind="local", request_hash=req_hash, mcp=mcp
            )

    def _append_task_outcome(
        self, parsed: dict[str, Any], *, mcp: bool, tool: str
    ) -> dict[str, Any]:
        req_hash = request_hash(tool, parsed)
        with self._tx() as conn:
            existing = conn.execute(
                "select request_hash, receipt_json from requests where scope_id=? and request_id=?",
                (self.binding["scope_id"], parsed["request_id"]),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != req_hash:
                    raise LedgerError("IDEMPOTENCY_CONFLICT")
                receipt = parse_json(existing["receipt_json"])
                receipt["replayed"] = True
                return receipt

            row = conn.execute(
                "select revision, scope_id, sensitivity from current_records where decision_id=?",
                (parsed["decision_id"],),
            ).fetchone()
            if row is None or row["scope_id"] != self.binding["scope_id"]:
                raise LedgerError("UNAVAILABLE")
            if mcp and row["sensitivity"] != "model_safe":
                raise LedgerError("UNAVAILABLE")

            task_id = parsed["payload"]["task_id"]
            prior = None
            for event_row in conn.execute(
                "select event_json from events where decision_id=? order by revision desc",
                (parsed["decision_id"],),
            ):
                event = parse_json(event_row["event_json"])
                if (
                    event["event_type"] == "outcome"
                    and event["payload"].get("task_id") == task_id
                ):
                    prior = event
                    break
            if prior is not None:
                prior_payload = prior["payload"]
                same = all(
                    prior_payload.get(key) == parsed["payload"].get(key)
                    for key in ("status", "note", "evidence", "applied_event_id")
                )
                if same:
                    receipt = {
                        "ledger_id": self.binding["ledger_id"],
                        "decision_id": parsed["decision_id"],
                        "event_id": prior["event_id"],
                        "revision": prior["revision"],
                        "replayed": True,
                    }
                    conn.execute(
                        "insert into requests(scope_id,request_id,request_hash,decision_id,receipt_json) "
                        "values (?,?,?,?,?)",
                        (
                            self.binding["scope_id"],
                            parsed["request_id"],
                            req_hash,
                            parsed["decision_id"],
                            canonical_json(receipt),
                        ),
                    )
                    return receipt

            expected_revision = row["revision"]
            event = {
                "schema_version": 1,
                "ledger_id": self.binding["ledger_id"],
                "event_id": new_uuid(),
                "decision_id": parsed["decision_id"],
                "scope_id": self.binding["scope_id"],
                "revision": expected_revision + 1,
                "event_type": "outcome",
                "recorded_at": now_ts(),
                "occurred_at": parsed["occurred_at"],
                "provenance": {
                    "actor_id": parsed.get("actor") or self.binding["actor_id"],
                    "channel": "agent" if mcp else self.actor_channel,
                },
                "request_id": parsed["request_id"],
                "expected_revision": expected_revision,
                "payload": parsed["payload"],
            }
            return self._append_event_row(
                conn, event, ingest_kind="local", request_hash=req_hash, mcp=mcp
            )

    def attest(
        self,
        decision_id: str,
        expected_revision: int,
        request_id: str,
        authority_ref: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.append_event(
            {
                "request_id": require_uuid(request_id),
                "decision_id": require_uuid(decision_id),
                "expected_revision": expected_revision,
                "occurred_at": None,
                "event_type": "attested",
                "payload": {
                    "approval": {"state": "owner_attested", "authority_ref": authority_ref},
                    "reason": reason,
                },
            },
            mcp=False,
            tool="attest",
        )

    def get(self, arguments: dict[str, Any], *, mcp: bool) -> dict[str, Any]:
        started = time.monotonic()
        parsed = validate_get_input(arguments)
        with self._tx() as conn:
            rec = self._get_record(conn, parsed["decision_id"], mcp=mcp)
            if rec is None:
                raise LedgerError("UNAVAILABLE")
            if "expected_revision" in parsed and rec["revision"] != parsed["expected_revision"]:
                raise LedgerError("REVISION_CONFLICT")
            rec = dict(rec)
            events = self._visible_trace(
                conn, self._trace_events(conn, parsed["decision_id"]), mcp=mcp
            )
            packed = {"record": self._pack_trace(rec, events)}
            fixed = self._fixed_by(conn, rec, events, mcp=mcp)
            if fixed:
                packed["record"]["fixed_by"] = fixed
            if mcp:
                self._log_access(
                    conn, tool="get", caller=parsed["actor"], task_id=None, query=None,
                    ids=[parsed["decision_id"]], nbytes=len(canonical_json(packed).encode("utf-8")),
                    started=started,
                )
            return packed

    def _fixed_by(
        self, conn: sqlite3.Connection, rec: dict[str, Any], events: list[dict[str, Any]], *, mcp: bool
    ) -> list[dict[str, Any]]:
        """Fixes that supersede this decision or are an exception to it, with the confidence
        this decision keeps in each fix's context (not_helped outcomes count against it there)."""
        out: list[dict[str, Any]] = []
        conf = rec.get("confidence") or {}
        succ, n = conf.get("successes") or 0, conf.get("sample_count") or 0
        for row in conn.execute(
            "select src, kind, context, task_id from fix_links where dst=? order by src", (rec["decision_id"],)
        ).fetchall():
            if self._get_record(conn, row["src"], mcp=mcp) is None:
                continue
            not_helped = sum(
                1
                for e in events
                if e["event_type"] == "outcome"
                and e["payload"].get("helpful") is False
                and (row["task_id"] is None or e["payload"].get("task_id") == row["task_id"])
            )
            out.append({
                "id": row["src"], "kind": row["kind"], "context": row["context"],
                **({"task_id": row["task_id"]} if row["task_id"] else {}),
                "not_helped": not_helped,
                "confidence_here": round(succ / (n + not_helped), 2) if (n + not_helped) else 0.0,
            })
        return out

    def _trace_events(self, conn: sqlite3.Connection, decision_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "select event_json from events where decision_id=? order by revision",
            (decision_id,),
        ).fetchall()
        traced: list[dict[str, Any]] = []
        for row in rows:
            event = parse_json(row["event_json"])
            if event["event_type"] not in {"applied", "outcome"}:
                continue
            traced.append(
                {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "revision": event["revision"],
                    "payload": event["payload"],
                }
            )
        return traced

    def _visible_trace(
        self,
        conn: sqlite3.Connection,
        events: list[dict[str, Any]],
        *,
        mcp: bool,
    ) -> list[dict[str, Any]]:
        if not mcp:
            return events
        scope = self.binding["scope_id"]
        visible: list[dict[str, Any]] = []
        for event in events:
            if event["event_type"] != "applied":
                visible.append(event)
                continue
            payload = dict(event["payload"])
            payload["decision_ids"] = [
                did
                for did in payload["decision_ids"]
                if self._trace_ref_visible(conn, did, required_scope=scope)
            ]
            copied = dict(event)
            copied["payload"] = payload
            visible.append(copied)
        return visible

    def _trace_ref_visible(
        self,
        conn: sqlite3.Connection,
        decision_id: str,
        *,
        required_scope: str,
    ) -> bool:
        row = conn.execute(
            "select sensitivity, scope_id from current_records where decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None or row["scope_id"] != required_scope:
            return False
        return row["sensitivity"] == "model_safe"

    def _pack_trace(self, rec: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """Keep the current record under the get cap. Drop oldest trace events first."""
        packed = dict(rec)
        packed["events"] = events
        if self._get_len(packed) <= MAX_GET_RESPONSE:
            return packed
        packed["trace_continued"] = True
        best = len(events)
        left, right = 0, len(events)
        while left <= right:
            mid = (left + right) // 2
            packed["events"] = events[mid:]
            if self._get_len(packed) <= MAX_GET_RESPONSE:
                best = mid
                right = mid - 1
            else:
                left = mid + 1
        packed["events"] = events[best:]
        if self._get_len(packed) > MAX_GET_RESPONSE:
            raise LedgerError("LIMIT_EXCEEDED")
        return packed

    @staticmethod
    def _get_len(rec: dict[str, Any]) -> int:
        return len(canonical_json({"record": rec}).encode("utf-8"))

    def find(self, arguments: dict[str, Any], *, mcp: bool) -> dict[str, Any]:
        """Compact, text-first find. One copy of each field; nothing below the relevance floor."""
        started = time.monotonic()
        self._reachable = False
        self._ensure_reachable()
        parsed = validate_find_input(arguments)
        qterms = ranking.query_terms(parsed["query"])
        if not qterms and not parsed["entities"] and not parsed["constraints"]:
            raise LedgerError(
                "INVALID_ARGUMENT",
                message="find needs query words (or entities); stop-words alone match nothing",
            )
        with self._tx() as conn:
            scope = self.binding["scope_id"]
            rows = conn.execute(
                "select decision_id, projection_json from current_records "
                "where scope_id=? and sensitivity='model_safe'",
                (scope,),
            ).fetchall()
            recs: dict[str, dict[str, Any]] = {}
            for row in rows:
                rec = self._get_record(conn, row["decision_id"], mcp=True)
                if rec is not None:
                    recs[row["decision_id"]] = rec
            corpus = ranking.Corpus((did, rec["body"], rec.get("revision")) for did, rec in recs.items())
            repeats = self._repeat_counts(recs)
            scored: list[tuple[tuple, dict[str, Any]]] = []
            text_of: dict[str, tuple[float, float, int]] = {}
            for did, rec in recs.items():
                text_of[did] = corpus.score(did, qterms) if qterms else (0.0, 0.0, 0)
            fix_pairs = self._fixes_in_context(recs, text_of, qterms)
            demoted = {old for _fix, old in fix_pairs}
            for did, rec in recs.items():
                if rec["lifecycle"] != "active" and not parsed["include_superseded"]:
                    continue
                body = rec["body"]
                ent_hits = sum(1 for e in parsed["entities"] if e in body["entities"])
                con_hits = sum(1 for c in parsed["constraints"] if c in body["constraints"])
                bm25, cov, matched = text_of[did]
                text_ok = bool(qterms) and ranking.relevant(cov, matched, len(qterms))
                if not (text_ok or ent_hits or con_hits):
                    continue
                rtype = ranking.record_type(body)
                crit = (body.get("trace") or {}).get("criticality", 0)
                boost = ranking.TYPE_BOOST[rtype] * (1 + 0.05 * crit)
                final = round(bm25 * boost, 1) if text_ok else 0.0
                if did in demoted:
                    final = round(final * ranking.NOT_HELPED_FACTOR, 1)
                ev = rec["evidence_state"]
                ev_rank = 0 if ev == "reported_verified" else 1 if ev in {"unverified", "none"} else 2
                conf = rec["confidence"]["score"] or 0.0
                key = (
                    -ent_hits,
                    -con_hits,
                    -final,
                    0 if rtype == "user_decision" else 1,
                    self._outcome_rank(rec),
                    ev_rank,
                    -conf,
                    -_ts_key(rec["updated_at"]),
                    did,
                )
                scored.append((key, rec))
            scored.sort(key=lambda item: item[0])
            ranked = [rec for _key, rec in scored]
            for fix, old in fix_pairs:
                ids = [r["decision_id"] for r in ranked]
                if old not in ids:
                    continue
                if fix in ids and ids.index(fix) < ids.index(old):
                    continue
                if fix in ids:
                    ranked.pop(ids.index(fix))
                    ids.remove(fix)
                ranked.insert(ids.index(old), recs[fix])
            offset = parsed["offset"]
            window = ranked[offset : offset + parsed["limit"]]
            shown = {rec["decision_id"] for rec in window}
            matches = []
            for rec in window:
                hit = self._compact(rec, repeats)
                if rec["decision_id"] in demoted:
                    hit["flag"] = "not_helped here"
                rel = self._related(conn, rec, recs, text_of, shown)
                if rel:
                    hit["related"] = rel
                    shown |= {r["id"] for r in rel}
                matches.append(hit)
            has_more = offset + len(window) < len(ranked)
            result: dict[str, Any] = {"matches": matches, "has_more": has_more}
            # Byte budget: drop related cases first, then trailing hits. Never truncate a field.
            while len(canonical_json(result).encode("utf-8")) > MAX_FIND_BYTES:
                with_rel = [m for m in matches if m.get("related")]
                if with_rel:
                    with_rel[-1]["related"].pop()
                    if not with_rel[-1]["related"]:
                        del with_rel[-1]["related"]
                elif matches:
                    matches.pop()
                    result["has_more"] = True
                else:
                    break
            nbytes = len(canonical_json(result).encode("utf-8"))
            self._log_access(
                conn,
                tool="find",
                caller=parsed["actor"],
                task_id=parsed["task_id"],
                query=parsed["query"],
                ids=[m["decision_id"] for m in matches] + [r["id"] for m in matches for r in m.get("related", [])],
                nbytes=nbytes,
                started=started,
            )
            return result

    @staticmethod
    def _fixes_in_context(
        recs: dict[str, dict[str, Any]], text_of: dict[str, tuple[float, float, int]], qterms: list[str]
    ) -> list[tuple[str, str]]:
        """(fix, old) pairs whose context this query is in: the query covers half the fix's context
        words, or the fix itself is a relevant hit. In that context the old decision drops and the
        fix ranks above it."""
        qset = set(qterms)
        pairs: list[tuple[str, str]] = []
        for did, rec in recs.items():
            trace = rec["body"].get("trace") or {}
            old = trace.get("fix_of")
            if not old or old not in recs or rec["lifecycle"] != "active":
                continue
            ctx = set(ranking.query_terms(trace.get("context") or ""))
            covered = len(ctx & qset)
            _bm, cov, matched = text_of.get(did, (0.0, 0.0, 0))
            fix_ok = bool(qterms) and ranking.relevant(cov, matched, len(qterms))
            if (ctx and covered * 2 >= len(ctx)) or fix_ok:
                pairs.append((did, old))
        return sorted(pairs)

    def _compact(self, rec: dict[str, Any], repeats: dict[tuple[str, str], int]) -> dict[str, Any]:
        body = rec["body"]
        trace = body.get("trace") or {}
        rtype = ranking.record_type(body)
        hit: dict[str, Any] = {
            "decision_id": rec["decision_id"],
            "type": rtype,
            "decision": ranking.words(body["summary"], 15) if rtype == "legacy" else body["summary"],
            "why": ranking.words(body["rationale"], 20) if rtype == "legacy" else body["rationale"],
        }
        if trace:
            hit["crit"] = trace["criticality"]
            for key in ("rule", "exception", "override_by"):
                if trace.get(key):
                    hit[key] = trace[key]
            if trace.get("fix_of"):
                hit[trace["fix_kind"]] = trace["fix_of"]
                hit["context"] = trace["context"]
            rk = self._repeat_key(trace)
            if rk and repeats.get(rk, 0) >= 2:
                hit["repeats"] = repeats[rk]
        if body["entities"]:
            hit["entities"] = body["entities"][:3]
        hit["outcome"] = rec["outcome"]["status"]
        flag = self._visible_flag(rec)
        if flag:
            hit["flag"] = flag
        return hit

    @staticmethod
    def _repeat_key(trace: dict[str, Any]) -> tuple[str, str] | None:
        if not trace.get("rule"):
            return None
        norm = lambda text: " ".join(ranking.terms(text or ""))  # noqa: E731
        return norm(trace.get("rule")), norm(trace.get("exception"))

    def _repeat_counts(self, recs: dict[str, dict[str, Any]]) -> dict[tuple[str, str], int]:
        """Same rule + same exception, counted across active traces: an automation-candidate signal."""
        counts: dict[tuple[str, str], int] = {}
        for rec in recs.values():
            if rec["lifecycle"] != "active":
                continue
            key = self._repeat_key(rec["body"].get("trace") or {})
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _related(
        self,
        conn: sqlite3.Connection,
        rec: dict[str, Any],
        recs: dict[str, dict[str, Any]],
        text_of: dict[str, tuple[float, float, int]],
        shown: set[str],
    ) -> list[dict[str, Any]]:
        """One hop over links (precedents it relied on, then cases that cite it), capped, on-topic."""
        did = rec["decision_id"]
        ids = [r[0] for r in conn.execute("select dst from links where src=? order by dst", (did,))]
        ids += [r[0] for r in conn.execute("select src from links where dst=? order by src", (did,))]
        ents = set(rec["body"]["entities"])
        out: list[dict[str, Any]] = []
        for other in ids:
            if other in shown or other not in recs or any(o["id"] == other for o in out):
                continue
            orec = recs[other]
            if orec["lifecycle"] != "active":
                continue
            on_topic = text_of.get(other, (0, 0, 0))[2] >= 1 or bool(ents & set(orec["body"]["entities"]))
            if not on_topic:
                continue
            out.append({"id": other, "decision": orec["body"]["summary"]})
            if len(out) >= RELATED_CAP:
                break
        return out

    def _log_access(
        self,
        conn: sqlite3.Connection,
        *,
        tool: str,
        caller: str | None,
        task_id: str | None,
        query: str | None,
        ids: list[str],
        nbytes: int,
        started: float,
    ) -> None:
        ms = int((time.monotonic() - started) * 1000)
        who = caller or "unnamed"
        conn.execute(
            "insert into access_log(ts,tool,caller,task_id,query,n,bytes,ids,ms) values (?,?,?,?,?,?,?,?,?)",
            (now_ts(), tool, who, task_id, (query or "")[:240], len(ids), nbytes, canonical_json(ids), ms),
        )
        line = {"event": tool, "caller": who, "n": len(ids), "bytes": nbytes, "ms": ms}
        if query:
            line["query"] = query[:120]
        if task_id:
            line["task_id"] = task_id
        sys.stderr.write(canonical_json(line) + "\n")
        sys.stderr.flush()

    def save(self, arguments: dict[str, Any], *, mcp: bool) -> dict[str, Any]:
        """Compact trace capture. Over-limit text is rejected loudly, never trimmed."""
        parsed = validate_save_input(arguments)
        trace = parsed["trace"]
        with self._tx() as conn:
            for pid in trace.get("precedent_ids", []):
                if self._get_record(conn, pid, mcp=mcp) is None:
                    raise LedgerError(
                        "INVALID_ARGUMENT",
                        message=f"precedent {pid} is not a visible record; nothing was saved",
                    )
            if trace.get("fix_of") and self._get_record(conn, trace["fix_of"], mcp=mcp) is None:
                raise LedgerError(
                    "INVALID_ARGUMENT",
                    message=f"{trace['fix_kind']} {trace['fix_of']} is not a visible record; nothing was saved",
                )
        body = {
            "kind": "decision",
            "summary": parsed["decision"],
            "situation": "; ".join(trace.get("inputs", [])) or parsed["decision"],
            "action": parsed["decision"],
            "rationale": parsed["why"],
            "constraints": [],
            "applicability": trace.get("rule") or parsed["decision"],
            "entities": parsed["entities"],
            "evidence": [],
            "sensitivity": "model_safe",
            "approval": (
                {"state": "self_reported", "authority_ref": "guru"}
                if trace["type"] == "user_decision"
                else {"state": "none", "authority_ref": None}
            ),
            "trace": trace,
        }
        args: dict[str, Any] = {
            "request_id": parsed["request_id"],
            "occurred_at": None,
            "body": body,
            "outcome": {"status": parsed["outcome"], "note": "", "evidence": []},
        }
        if parsed["actor"]:
            args["actor"] = parsed["actor"]
        receipt = self.record(args, mcp=mcp, tool="save")
        return {"decision_id": receipt["decision_id"], "revision": receipt["revision"]}

    def access_report(self) -> dict[str, Any]:
        """Per-caller lookups, bytes, and helpful marks (outcome.helpful on the returned ids)."""
        conn = self.open()
        by_caller: dict[str, dict[str, Any]] = {}
        returned: dict[str, set[str]] = {}
        for row in conn.execute("select tool, caller, task_id, n, bytes, ids from access_log"):
            item = by_caller.setdefault(row["caller"], {"find": 0, "get": 0, "bytes": 0, "hits": 0})
            item[row["tool"]] = item.get(row["tool"], 0) + 1
            item["bytes"] += row["bytes"]
            item["hits"] += row["n"]
            if row["task_id"]:
                returned.setdefault(row["task_id"], set()).update(parse_json(row["ids"]))
        helpful = {"marked": 0, "helpful": 0}
        for row in conn.execute("select decision_id, event_json from events"):
            ev = parse_json(row["event_json"])
            p = ev["payload"]
            if ev["event_type"] == "outcome" and "helpful" in p and row["decision_id"] in returned.get(p.get("task_id"), set()):
                helpful["marked"] += 1
                helpful["helpful"] += 1 if p["helpful"] else 0
        return {"by_caller": by_caller, "helpful": helpful}

    def browse(self, arguments: dict[str, Any], *, mcp: bool) -> dict[str, Any]:
        """Recent or filtered cards for the ledger view. MCP uses get's visibility."""
        self._reachable = False
        self._ensure_reachable()
        query = arguments.get("query", "")
        entities = arguments.get("entities") or []
        include = arguments.get("include_superseded", False)
        limit = arguments.get("limit", 12)
        offset = arguments.get("offset", 0)
        if (
            not isinstance(query, str)
            or not isinstance(entities, list)
            or any(type(item) is not str or not item for item in entities)
            or type(include) is not bool
            or type(limit) is not int
            or not 1 <= limit <= 24
            or type(offset) is not int
            or not 0 <= offset <= BROWSE_OFFSET_MAX
        ):
            raise LedgerError("INVALID_ARGUMENT")
        needle = query.casefold().strip()
        with self._tx() as conn:
            scope = self.binding["scope_id"]
            sql = "select projection_json from current_records where scope_id=?"
            if mcp:
                sql += " and sensitivity='model_safe'"
            if not include:
                sql += " and lifecycle='active'"
            sql += " order by updated_at desc, decision_id asc"
            rows = conn.execute(sql, (scope,)).fetchall()
            matched: list[dict[str, Any]] = []
            for row in rows:
                proj = parse_json(row["projection_json"])
                body = proj["body"]
                ents = list(body.get("entities") or [])
                if entities and not any(item in ents for item in entities):
                    continue
                if needle:
                    blob = " ".join([body.get("summary") or "", body.get("applicability") or "", *ents]).casefold()
                    if needle not in blob:
                        continue
                rec = self._get_record(conn, proj["decision_id"], mcp=mcp)
                if rec is None:
                    continue
                card = self._summary(rec)
                card["entities"] = list(rec["body"].get("entities") or [])
                matched.append(card)
            window = matched[offset:]
            matches: list[dict[str, Any]] = []
            has_more = False
            for card in window:
                trial = {"matches": matches + [card], "has_more": True}
                if len(matches) >= limit or len(canonical_json(trial).encode("utf-8")) > MAX_FIND_RESPONSE:
                    has_more = True
                    break
                matches.append(card)
            if has_more and offset + len(matches) > BROWSE_OFFSET_MAX:
                has_more = False
            return {"matches": matches, "has_more": has_more}

    def export(self, output: Path, *, include_local_only: bool) -> dict[str, Any]:
        output = _refuse_network(output)
        if not output.is_absolute():
            raise LedgerError("INVALID_ARGUMENT")
        if output.exists():
            raise LedgerError("INVALID_TRANSITION")
        _refuse_symlink(output)
        with self._tx() as conn:
            scope = self.binding["scope_id"]
            events = conn.execute(
                "select event_json, ingest_kind from events where scope_id=? order by decision_id, revision",
                (scope,),
            ).fetchall()
            tombstones = conn.execute(
                "select kind, id, scope_id, decision_id, revision, purged_at from tombstones where scope_id=? order by kind, id",
                (scope,),
            ).fetchall()
            projections = {
                row["decision_id"]: parse_json(row["projection_json"])
                for row in conn.execute(
                    "select decision_id, projection_json from current_records where scope_id=?",
                    (scope,),
                )
            }
            by_decision: dict[str, list[dict[str, Any]]] = {}
            for row in events:
                event = parse_json(row["event_json"])
                by_decision.setdefault(event["decision_id"], []).append(event)
            excluded: set[str] = set()
            if not include_local_only:
                for did, evs in by_decision.items():
                    rec = projections.get(did)
                    bodies = [e["payload"]["body"] for e in evs if e["event_type"] in {"created", "corrected"}]
                    current_unsafe = rec is not None and rec["body"]["sensitivity"] != "model_safe"
                    hist_unsafe = any(b["sensitivity"] != "model_safe" for b in bodies)
                    if rec is None or current_unsafe or hist_unsafe:
                        excluded.add(did)
                changed = True
                while changed:
                    changed = False
                    for did, evs in by_decision.items():
                        if did in excluded:
                            continue
                        rec = projections.get(did)
                        rels: set[str] = set()
                        if rec is not None:
                            if rec.get("replacement_id"):
                                rels.add(rec["replacement_id"])
                            for c in rec.get("conflicts") or []:
                                rels.add(c["other_id"])
                        for event in evs:
                            payload = event.get("payload") or {}
                            if event["event_type"] == "applied":
                                rels.update(payload.get("decision_ids") or [])
                            elif event["event_type"] == "superseded" and payload.get("replacement_id"):
                                rels.add(payload["replacement_id"])
                            elif event["event_type"] in {"conflict_opened", "conflict_resolved"} and payload.get("other_id"):
                                rels.add(payload["other_id"])
                        rels.discard(did)
                        if rels & excluded:
                            excluded.add(did)
                            changed = True
            lines = [
                canonical_json(
                    {
                        "format": "context-ledger",
                        "version": 1,
                        "source_ledger_id": self.binding["ledger_id"],
                        "scope_id": scope,
                        "includes_local_only": include_local_only,
                    }
                )
            ]
            exported = 0
            for did in sorted(by_decision):
                if did in excluded:
                    continue
                exported += 1
                for event in by_decision[did]:
                    lines.append(
                        canonical_json({"type": "event", "scope_id": scope, "event": event})
                    )
            for row in tombstones:
                if not include_local_only and row["decision_id"] in excluded:
                    continue
                lines.append(
                    canonical_json(
                        {
                            "type": "tombstone",
                            "kind": row["kind"],
                            "id": row["id"],
                            "scope_id": row["scope_id"],
                            "decision_id": row["decision_id"],
                            "revision": row["revision"],
                            "purged_at": row["purged_at"],
                        }
                    )
                )
            text = "\n".join(lines) + "\n"
            tmp = output.with_suffix(output.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(output)
            if os.name != "nt":
                os.chmod(output, 0o600)
        return {
            "exported_records": exported,
            "excluded_records": len(excluded),
            "output": str(output),
        }

    def import_file(
        self,
        input_path: Path,
        source_ledger: str,
        source_scope: str,
        *,
        include_local_only: bool,
    ) -> dict[str, Any]:
        input_path = _refuse_network(input_path)
        source_ledger = require_uuid(source_ledger)
        source_scope = require_scope(source_scope)
        try:
            size = input_path.stat().st_size
        except OSError as exc:
            raise LedgerError("UNAVAILABLE") from exc
        if size > MAX_IMPORT_BYTES:
            raise LedgerError("LIMIT_EXCEEDED")
        raw = input_path.read_bytes()
        if len(raw) > MAX_IMPORT_BYTES:
            raise LedgerError("LIMIT_EXCEEDED")
        text = raw.decode("utf-8")
        lines = text.splitlines()
        if len(lines) > MAX_IMPORT_LINES or not lines:
            raise LedgerError("LIMIT_EXCEEDED")
        header = parse_json(lines[0])
        header = require_keys(
            header, {"format", "version", "source_ledger_id", "scope_id", "includes_local_only"}
        )
        if header["format"] != "context-ledger" or header["version"] != 1:
            raise LedgerError("INVALID_ARGUMENT")
        if header["source_ledger_id"] != source_ledger or header["scope_id"] != source_scope:
            raise LedgerError("INVALID_ARGUMENT")
        if header["includes_local_only"] and not include_local_only:
            raise LedgerError("INVALID_ARGUMENT")
        events: list[dict[str, Any]] = []
        tombs: list[dict[str, Any]] = []
        for line in lines[1:]:
            obj = parse_json(line)
            kind = obj.get("type")
            if obj.get("scope_id") != source_scope:
                raise LedgerError("INVALID_ARGUMENT")
            if kind == "event":
                event = obj.get("event")
                encoded = canonical_json(event)
                if len(encoded.encode("utf-8")) > MAX_EVENT:
                    raise LedgerError("LIMIT_EXCEEDED")
                events.append(self._validate_imported_event(event))
            elif kind == "tombstone":
                if len(line.encode("utf-8")) > MAX_TOMBSTONE_BYTES:
                    raise LedgerError("LIMIT_EXCEEDED")
                tombs.append(self._validate_imported_tombstone(obj, source_scope=source_scope))
            else:
                raise LedgerError("INVALID_ARGUMENT")
        dest_scope = self.binding["scope_id"]
        received = now_ts()
        with self._tx() as conn:
            for tomb in tombs:
                tid = require_uuid(tomb["id"])
                existing_event = conn.execute("select 1 from events where event_id=?", (tid,)).fetchone()
                existing_dec = conn.execute(
                    "select 1 from current_records where decision_id=?", (tomb["decision_id"],)
                ).fetchone()
                if existing_event or existing_dec:
                    raise LedgerError("INVALID_TRANSITION")
                conn.execute(
                    "insert or ignore into tombstones(kind,id,scope_id,decision_id,revision,purged_at) values (?,?,?,?,?,?)",
                    (
                        tomb["kind"],
                        tid,
                        dest_scope,
                        require_uuid(tomb["decision_id"]),
                        tomb["revision"],
                        tomb["purged_at"],
                    ),
                )
            imported = 0
            for event in events:
                if conn.execute(
                    "select 1 from tombstones where id=? or id=?",
                    (event["event_id"], event["decision_id"]),
                ).fetchone():
                    raise LedgerError("INVALID_TRANSITION")
                existing = conn.execute(
                    "select event_json from events where event_id=?", (event["event_id"],)
                ).fetchone()
                encoded = canonical_json(event)
                if existing is not None:
                    if existing["event_json"] != encoded:
                        raise LedgerError("INVALID_TRANSITION")
                    continue
                created = conn.execute(
                    "select event_json from events where decision_id=? and revision=1",
                    (event["decision_id"],),
                ).fetchone()
                if created is not None and event["event_type"] == "created":
                    prev = parse_json(created["event_json"])
                    if prev["ledger_id"] != event["ledger_id"]:
                        raise LedgerError("INVALID_TRANSITION")
                self._append_event_row(
                    conn,
                    event,
                    ingest_kind="import",
                    request_hash=None,
                    mcp=False,
                    received_at=received,
                    map_scope=dest_scope,
                )
                imported += 1
            self._rebuild_projections(conn)
            self._assert_imported_graph(conn)
            for did in [r[0] for r in conn.execute("select distinct decision_id from events")]:
                self._assert_projections_fit(conn, did)
        return {"imported_events": imported}

    def purge(self, decision_id: str, expected_revision: int, confirm_ledger: str) -> dict[str, Any]:
        decision_id = require_uuid(decision_id)
        confirm_ledger = require_uuid(confirm_ledger)
        if confirm_ledger != self.binding["ledger_id"]:
            raise LedgerError("INVALID_ARGUMENT")
        with self._lock:
            conn = self.open()
            try:
                conn.execute("begin immediate")
            except sqlite3.OperationalError as exc:
                raise LedgerError("BUSY", retryable=True) from exc
            self._guard_allow_maintenance(conn, allow=None)
            existing_tomb = conn.execute(
                "select revision, scope_id from tombstones where kind='decision' and id=?",
                (decision_id,),
            ).fetchone()
            if existing_tomb is not None:
                if existing_tomb["scope_id"] != self.binding["scope_id"]:
                    conn.execute("rollback")
                    raise LedgerError("UNAVAILABLE")
                if existing_tomb["revision"] != expected_revision:
                    conn.execute("rollback")
                    raise LedgerError("REVISION_CONFLICT")
                conn.execute("rollback")
                return {"purged": False, "replayed": True}
            row = conn.execute(
                "select revision, scope_id from current_records where decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None or row["scope_id"] != self.binding["scope_id"]:
                conn.execute("rollback")
                raise LedgerError("UNAVAILABLE")
            if row["revision"] != expected_revision:
                conn.execute("rollback")
                raise LedgerError("REVISION_CONFLICT")
            target = canonical_json({"decision_id": decision_id, "revision": expected_revision})
            conn.execute(
                "update ledger_meta set maintenance='purge', maintenance_target=? where singleton=1",
                (target,),
            )
            self._purge_body(conn, decision_id, expected_revision)
            conn.execute("commit")
            self._finish_maintenance(conn)
            return {"purged": True, "replayed": False}

    def rebuild(self) -> dict[str, Any]:
        with self._lock:
            conn = self.open()
            try:
                conn.execute("begin immediate")
            except sqlite3.OperationalError as exc:
                raise LedgerError("BUSY", retryable=True) from exc
            self._guard_allow_maintenance(conn, allow=None)
            conn.execute(
                "update ledger_meta set maintenance='rebuild', maintenance_target=null where singleton=1"
            )
            self._rebuild_projections(conn)
            conn.execute("commit")
            self._finish_maintenance(conn)
            return {"rebuilt": True}

    def migrate(self) -> dict[str, Any]:
        with self._tx() as conn:
            ver = conn.execute("pragma user_version").fetchone()[0]
            if ver != SCHEMA_VERSION:
                raise LedgerError("UPGRADE_REQUIRED")
        return {"migrated": False, "schema_version": SCHEMA_VERSION}

    def resume_maintenance(self, confirm_ledger: str) -> dict[str, Any]:
        confirm_ledger = require_uuid(confirm_ledger)
        if confirm_ledger != self.binding["ledger_id"]:
            raise LedgerError("INVALID_ARGUMENT")
        with self._lock:
            conn = self.open()
            try:
                conn.execute("begin immediate")
            except sqlite3.OperationalError as exc:
                raise LedgerError("BUSY", retryable=True) from exc
            row = conn.execute(
                "select ledger_id, maintenance, maintenance_target from ledger_meta where singleton=1"
            ).fetchone()
            if row is None or row["ledger_id"] != self.binding["ledger_id"]:
                conn.execute("rollback")
                raise LedgerError("UNAVAILABLE")
            if row["maintenance"] is None:
                conn.execute("rollback")
                raise LedgerError("INVALID_TRANSITION")
            if row["maintenance"] == "purge" and row["maintenance_target"]:
                target = parse_json(row["maintenance_target"])
                still = conn.execute(
                    "select 1 from current_records where decision_id=?",
                    (target["decision_id"],),
                ).fetchone()
                if still:
                    self._purge_body(conn, target["decision_id"], target["revision"])
            if row["maintenance"] == "rebuild":
                self._rebuild_projections(conn)
            conn.execute("commit")
            self._finish_maintenance(conn)
            return {"resumed": True}

    def _guard_allow_maintenance(self, conn: sqlite3.Connection, allow: str | None) -> None:
        user_version = conn.execute("pragma user_version").fetchone()[0]
        if user_version != SCHEMA_VERSION:
            raise LedgerError("UPGRADE_REQUIRED")
        row = conn.execute(
            "select ledger_id, maintenance, schema_version from ledger_meta where singleton=1"
        ).fetchone()
        if row is None or row["ledger_id"] != self.binding["ledger_id"]:
            raise LedgerError("UNAVAILABLE")
        if row["schema_version"] != SCHEMA_VERSION:
            raise LedgerError("UPGRADE_REQUIRED")
        if row["maintenance"] is not None and row["maintenance"] != allow:
            raise LedgerError("MAINTENANCE", retryable=True)

    def _finish_maintenance(self, conn: sqlite3.Connection) -> None:
        conn.execute("vacuum")
        chk = conn.execute("pragma quick_check").fetchone()
        if chk is None or str(chk[0]) != "ok":
            raise LedgerError("CORRUPT_LEDGER")
        conn.execute("begin immediate")
        self._rebuild_projections(conn)
        conn.execute(
            "update ledger_meta set maintenance=null, maintenance_target=null where singleton=1"
        )
        conn.execute("commit")

    def _purge_body(self, conn: sqlite3.Connection, decision_id: str, revision: int) -> None:
        now = now_ts()
        events = conn.execute(
            "select event_id, revision, event_json from events where decision_id=?",
            (decision_id,),
        ).fetchall()
        for row in events:
            conn.execute(
                "insert or ignore into tombstones(kind,id,scope_id,decision_id,revision,purged_at) values ('event',?,?,?,?,?)",
                (row["event_id"], self.binding["scope_id"], decision_id, row["revision"], now),
            )
        conn.execute(
            "insert or replace into tombstones(kind,id,scope_id,decision_id,revision,purged_at) values ('decision',?,?,?,?,?)",
            (decision_id, self.binding["scope_id"], decision_id, revision, now),
        )
        others = conn.execute(
            "select event_id, decision_id, revision, event_json, scope_id from events where decision_id!=?",
            (decision_id,),
        ).fetchall()
        for row in others:
            event = parse_json(row["event_json"])
            payload = event.get("payload") or {}
            refs = []
            if event["event_type"] == "superseded":
                refs.append(payload.get("replacement_id"))
            if event["event_type"] in {"conflict_opened", "conflict_resolved"}:
                refs.append(payload.get("other_id"))
            if event["event_type"] == "applied" and decision_id in (payload.get("decision_ids") or []):
                kept = [item for item in payload["decision_ids"] if item != decision_id]
                if event["decision_id"] not in kept:
                    kept.insert(0, event["decision_id"])
                revised = dict(event)
                revised["payload"] = dict(payload)
                revised["payload"]["decision_ids"] = kept
                conn.execute(
                    "update events set event_json=? where event_id=?",
                    (canonical_json(revised), row["event_id"]),
                )
                continue
            if decision_id in refs:
                conn.execute(
                    "insert or ignore into tombstones(kind,id,scope_id,decision_id,revision,purged_at) values ('event',?,?,?,?,?)",
                    (
                        row["event_id"],
                        row["scope_id"],
                        row["decision_id"],
                        row["revision"],
                        now,
                    ),
                )
                conn.execute("delete from events where event_id=?", (row["event_id"],))
        conn.execute("delete from events where decision_id=?", (decision_id,))
        conn.execute("delete from requests where decision_id=?", (decision_id,))
        conn.execute("delete from current_records where decision_id=?", (decision_id,))
        conn.execute("delete from search_fts where decision_id=?", (decision_id,))
        self._rebuild_projections(conn)

    def _append_event_row(
        self,
        conn: sqlite3.Connection,
        event: dict[str, Any],
        *,
        ingest_kind: str,
        request_hash: str | None,
        mcp: bool,
        received_at: str | None = None,
        map_scope: str | None = None,
    ) -> dict[str, Any]:
        pause = os.environ.get("CONTEXT_LEDGER_TEST_PAUSE")
        encoded = canonical_json(event)
        if len(encoded.encode("utf-8")) > MAX_EVENT:
            raise LedgerError("LIMIT_EXCEEDED")
        dest_scope = map_scope or event["scope_id"]
        if dest_scope != self.binding["scope_id"] and ingest_kind == "local":
            raise LedgerError("UNAVAILABLE")
        self._validate_transition(conn, event, mcp=mcp, ingest_kind=ingest_kind)
        self._check_ceilings(conn, extra_events=1)
        received = received_at or now_ts()
        try:
            conn.execute(
                "insert into events(event_id,decision_id,scope_id,revision,event_json,ingest_kind,received_at) "
                "values (?,?,?,?,?,?,?)",
                (
                    event["event_id"],
                    event["decision_id"],
                    dest_scope,
                    event["revision"],
                    encoded,
                    ingest_kind,
                    received,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LedgerError("INVALID_TRANSITION") from exc
        if ingest_kind != "import":
            self._rebuild_one(conn, event["decision_id"])
            if event["event_type"] in {"conflict_opened", "conflict_resolved"}:
                self._rebuild_one(conn, event["payload"]["other_id"])
            if event["event_type"] == "superseded":
                self._rebuild_one(conn, event["payload"]["replacement_id"])
            self._assert_projections_fit(conn, event["decision_id"])
        receipt = {
            "ledger_id": self.binding["ledger_id"],
            "decision_id": event["decision_id"],
            "event_id": event["event_id"],
            "revision": event["revision"],
            "replayed": False,
        }
        if request_hash is not None:
            conn.execute(
                "insert into requests(scope_id,request_id,request_hash,decision_id,receipt_json) values (?,?,?,?,?)",
                (
                    self.binding["scope_id"],
                    event["request_id"],
                    request_hash,
                    event["decision_id"],
                    canonical_json(receipt),
                ),
            )
        if pause:
            time.sleep(float(pause))
        return receipt

    def _validate_imported_event(self, event: Any) -> dict[str, Any]:
        obj = require_keys(
            event,
            {
                "schema_version",
                "ledger_id",
                "event_id",
                "decision_id",
                "scope_id",
                "revision",
                "event_type",
                "recorded_at",
                "occurred_at",
                "provenance",
                "request_id",
                "expected_revision",
                "payload",
            },
        )
        if obj["schema_version"] != 1:
            raise LedgerError("UPGRADE_REQUIRED")
        event_type = require_enum(obj["event_type"], EVENT_TYPE)
        if type(obj["revision"]) is not int or obj["revision"] < 1:
            raise LedgerError("INVALID_ARGUMENT")
        revision = obj["revision"]
        expected = obj["expected_revision"]
        if type(expected) is not int or expected != revision - 1:
            raise LedgerError("INVALID_TRANSITION")
        prov = require_keys(obj["provenance"], {"actor_id", "channel"})
        return {
            "schema_version": 1,
            "ledger_id": require_uuid(obj["ledger_id"]),
            "event_id": require_uuid(obj["event_id"]),
            "decision_id": require_uuid(obj["decision_id"]),
            "scope_id": require_scope(obj["scope_id"]),
            "revision": revision,
            "event_type": event_type,
            "recorded_at": require_ts(obj["recorded_at"]),
            "occurred_at": require_ts(obj["occurred_at"], allow_null=True),
            "provenance": {
                "actor_id": require_scope(prov["actor_id"]),
                "channel": prov["channel"] if prov["channel"] in {"agent", "owner_cli"} else (_ for _ in ()).throw(LedgerError("INVALID_ARGUMENT")),
            },
            "request_id": require_uuid(obj["request_id"]),
            "expected_revision": expected,
            "payload": validate_payload(event_type, obj["payload"], allow_owner_attested=True),
        }

    def _validate_imported_tombstone(self, obj: Any, *, source_scope: str) -> dict[str, Any]:
        row = require_keys(
            obj, {"type", "kind", "id", "scope_id", "decision_id", "revision", "purged_at"}
        )
        if row["type"] != "tombstone":
            raise LedgerError("INVALID_ARGUMENT")
        kind = require_enum(row["kind"], ("decision", "event"))
        if type(row["revision"]) is not int or row["revision"] < 1:
            raise LedgerError("INVALID_ARGUMENT")
        scope_id = require_scope(row["scope_id"])
        if scope_id != source_scope:
            raise LedgerError("INVALID_ARGUMENT")
        return {
            "type": "tombstone",
            "kind": kind,
            "id": require_uuid(row["id"]),
            "scope_id": scope_id,
            "decision_id": require_uuid(row["decision_id"]),
            "revision": row["revision"],
            "purged_at": require_ts(row["purged_at"]),
        }

    def _validate_transition(
        self,
        conn: sqlite3.Connection,
        event: dict[str, Any],
        *,
        mcp: bool,
        ingest_kind: str,
    ) -> None:
        et = event["event_type"]
        payload = event["payload"]
        if et == "created":
            if event["revision"] != 1 or event["expected_revision"] != 0:
                raise LedgerError("INVALID_TRANSITION")
            return
        cur = conn.execute(
            "select projection_json, revision, lifecycle, sensitivity, scope_id from current_records where decision_id=?",
            (event["decision_id"],),
        ).fetchone()
        if cur is None:
            if ingest_kind == "import":
                return
            raise LedgerError("UNAVAILABLE")
        rec = parse_json(cur["projection_json"])
        if mcp and rec["body"]["sensitivity"] == "local_only":
            raise LedgerError("UNAVAILABLE")
        if et == "corrected":
            if payload["body"]["kind"] != rec["body"]["kind"]:
                raise LedgerError("INVALID_TRANSITION")
            if mcp and rec["body"]["sensitivity"] == "local_only" and payload["body"]["sensitivity"] == "model_safe":
                raise LedgerError("INVALID_TRANSITION")
            if mcp and rec["body"]["sensitivity"] == "local_only":
                raise LedgerError("UNAVAILABLE")
            if mcp and payload["body"]["sensitivity"] == "model_safe" and rec["body"]["sensitivity"] == "local_only":
                raise LedgerError("INVALID_TRANSITION")
            if mcp and rec["body"]["sensitivity"] == "local_only":
                raise LedgerError("UNAVAILABLE")
            if mcp and payload["body"]["sensitivity"] == "model_safe" and rec["body"]["sensitivity"] != "model_safe":
                raise LedgerError("INVALID_TRANSITION")
            return
        if et == "superseded":
            if rec["lifecycle"] != "active":
                raise LedgerError("INVALID_TRANSITION")
            if rec.get("replacement_id"):
                raise LedgerError("INVALID_TRANSITION")
            if ingest_kind == "import":
                return
            _other, orec = self._require_visible_counterpart(
                conn, payload["replacement_id"], mcp=mcp, required_scope=cur["scope_id"]
            )
            if orec["lifecycle"] != "active":
                raise LedgerError("INVALID_TRANSITION")
            if self._supersession_cycle(conn, event["decision_id"], payload["replacement_id"]):
                raise LedgerError("INVALID_TRANSITION")
            return
        if et == "conflict_opened":
            if payload["other_id"] == event["decision_id"]:
                raise LedgerError("INVALID_ARGUMENT")
            if ingest_kind == "import":
                return
            self._require_visible_counterpart(
                conn, payload["other_id"], mcp=mcp, required_scope=cur["scope_id"]
            )
            pair = tuple(sorted([event["decision_id"], payload["other_id"]]))
            existing = rec.get("conflicts") or []
            if any(tuple(sorted([event["decision_id"], c["other_id"]])) == pair for c in existing):
                raise LedgerError("INVALID_TRANSITION")
            if len(existing) >= MAX_CONFLICTS:
                raise LedgerError("LIMIT_EXCEEDED")
            return
        if et == "conflict_resolved":
            if ingest_kind == "import":
                return
            visible = self._visible_conflicts(conn, rec, mcp=mcp, required_scope=cur["scope_id"])
            found = None
            for c in visible:
                if c["other_id"] == payload["other_id"] and c["opened_event_id"] == payload["opened_event_id"]:
                    found = c
                    break
            if found is None:
                raise LedgerError("INVALID_TRANSITION")
            return
        if et == "attested" and mcp:
            raise LedgerError("INVALID_ARGUMENT")
        if et == "applied":
            if event["decision_id"] not in payload["decision_ids"]:
                raise LedgerError(
                    "INVALID_ARGUMENT",
                    message="applied decision_ids must include this decision",
                )
            if ingest_kind != "import":
                for did in payload["decision_ids"]:
                    self._require_visible_counterpart(
                        conn, did, mcp=mcp, required_scope=cur["scope_id"]
                    )
            return
        if et == "outcome":
            note_required = rec["outcome"]["status"] in {
                "success",
                "failed",
                "inconclusive",
                "abandoned",
            } and payload["status"] != rec["outcome"]["status"]
            if note_required and not payload["note"]:
                raise LedgerError("INVALID_ARGUMENT")
            applied_event_id = payload.get("applied_event_id")
            if applied_event_id:
                self._require_applied_link(
                    conn,
                    applied_event_id,
                    decision_id=event["decision_id"],
                    task_id=payload.get("task_id"),
                )

    def _require_applied_link(
        self,
        conn: sqlite3.Connection,
        applied_event_id: str,
        *,
        decision_id: str,
        task_id: str | None,
    ) -> None:
        row = conn.execute(
            "select event_json from events where event_id=?",
            (applied_event_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(
                "INVALID_ARGUMENT",
                message="applied_event_id does not match an applied event",
            )
        linked = parse_json(row["event_json"])
        if linked["event_type"] != "applied" or linked["decision_id"] != decision_id:
            raise LedgerError(
                "INVALID_ARGUMENT",
                message="applied_event_id does not match an applied event on this decision",
            )
        if task_id and linked["payload"].get("task_id") != task_id:
            raise LedgerError(
                "INVALID_ARGUMENT",
                message="outcome task_id does not match the applied event",
            )

    def _require_visible_counterpart(
        self,
        conn: sqlite3.Connection,
        decision_id: str,
        *,
        mcp: bool,
        required_scope: str,
    ) -> tuple[Any, dict[str, Any]]:
        row = conn.execute(
            "select projection_json, lifecycle, sensitivity, scope_id from current_records where decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None or row["scope_id"] != required_scope:
            raise LedgerError("UNAVAILABLE")
        rec = parse_json(row["projection_json"])
        if mcp and rec["body"]["sensitivity"] != "model_safe":
            raise LedgerError("UNAVAILABLE")
        return row, rec

    def _visible_conflicts(
        self,
        conn: sqlite3.Connection,
        rec: dict[str, Any],
        *,
        mcp: bool,
        required_scope: str,
    ) -> list[dict[str, Any]]:
        visible = []
        for c in rec.get("conflicts") or []:
            other = conn.execute(
                "select sensitivity, scope_id from current_records where decision_id=?",
                (c["other_id"],),
            ).fetchone()
            if other is None or other["scope_id"] != required_scope:
                continue
            if mcp and other["sensitivity"] != "model_safe":
                continue
            visible.append(c)
        return visible

    def _assert_imported_graph(self, conn: sqlite3.Connection) -> None:
        ids = [r[0] for r in conn.execute("select distinct decision_id from events")]
        reduced = {did: self._reduce_decision(conn, did) for did in ids}
        opened: dict[str, tuple[str, str]] = {}
        resolved_counts: dict[str, int] = {}
        resolves: list[dict[str, Any]] = []
        for row in conn.execute("select event_json from events"):
            event = parse_json(row["event_json"])
            et = event["event_type"]
            payload = event["payload"]
            if et == "superseded":
                rid = payload["replacement_id"]
                if rid not in reduced:
                    raise LedgerError("INVALID_TRANSITION")
                if reduced[rid]["scope_id"] != reduced[event["decision_id"]]["scope_id"]:
                    raise LedgerError("INVALID_TRANSITION")
            elif et == "conflict_opened":
                opened[event["event_id"]] = (event["decision_id"], payload["other_id"])
                oid = payload["other_id"]
                if oid not in reduced:
                    raise LedgerError("INVALID_TRANSITION")
                if reduced[oid]["scope_id"] != reduced[event["decision_id"]]["scope_id"]:
                    raise LedgerError("INVALID_TRANSITION")
            elif et == "conflict_resolved":
                resolves.append(event)
                oid = payload["opened_event_id"]
                resolved_counts[oid] = resolved_counts.get(oid, 0) + 1
            elif et == "applied":
                owner = reduced[event["decision_id"]]
                for did in payload["decision_ids"]:
                    if did in reduced:
                        if reduced[did]["scope_id"] != owner["scope_id"]:
                            raise LedgerError("INVALID_TRANSITION")
                        continue
                    tomb = conn.execute(
                        "select scope_id from tombstones where kind='decision' and id=?",
                        (did,),
                    ).fetchone()
                    if tomb is None or tomb["scope_id"] != owner["scope_id"]:
                        raise LedgerError("INVALID_TRANSITION")
            elif et == "outcome" and payload.get("applied_event_id"):
                linked = conn.execute(
                    "select event_json from events where event_id=?",
                    (payload["applied_event_id"],),
                ).fetchone()
                if linked is None:
                    raise LedgerError("INVALID_TRANSITION")
                applied = parse_json(linked["event_json"])
                if (
                    applied["event_type"] != "applied"
                    or applied["decision_id"] != event["decision_id"]
                ):
                    raise LedgerError("INVALID_TRANSITION")
                if payload.get("task_id") and applied["payload"].get("task_id") != payload["task_id"]:
                    raise LedgerError("INVALID_TRANSITION")
        for oid, count in resolved_counts.items():
            if count > 1 or oid not in opened:
                raise LedgerError("INVALID_TRANSITION")
        for event in resolves:
            payload = event["payload"]
            opener = opened[payload["opened_event_id"]]
            if tuple(sorted(opener)) != tuple(sorted([event["decision_id"], payload["other_id"]])):
                raise LedgerError("INVALID_TRANSITION")
        self._relations(reduced)
        for did, state in reduced.items():
            seen = {did}
            cur = state["replacement_id"]
            while cur:
                if cur not in reduced or cur in seen:
                    raise LedgerError("INVALID_TRANSITION")
                seen.add(cur)
                cur = reduced[cur]["replacement_id"]

    def _supersession_cycle(self, conn: sqlite3.Connection, src: str, dst: str) -> bool:
        seen = {src}
        cur = dst
        while cur:
            if cur in seen:
                return True
            seen.add(cur)
            row = conn.execute(
                "select projection_json from current_records where decision_id=?",
                (cur,),
            ).fetchone()
            if row is None:
                return False
            rec = parse_json(row["projection_json"])
            cur = rec.get("replacement_id")
        return False

    def _check_ceilings(self, conn: sqlite3.Connection, extra_events: int) -> None:
        events = conn.execute("select count(*) from events").fetchone()[0]
        tombs = conn.execute("select count(*) from tombstones").fetchone()[0]
        current = conn.execute("select count(*) from current_records").fetchone()[0]
        if events + extra_events > MAX_EVENTS:
            raise LedgerError("LIMIT_EXCEEDED")
        if events + extra_events + tombs + current > MAX_COMBINED_ROWS:
            raise LedgerError("LIMIT_EXCEEDED")
        page_count = conn.execute("pragma page_count").fetchone()[0]
        page_size = conn.execute("pragma page_size").fetchone()[0]
        if page_count * page_size > MAX_DB_BYTES:
            raise LedgerError("LIMIT_EXCEEDED")

    def _assert_projections_fit(self, conn: sqlite3.Connection, decision_id: str) -> None:
        ids = {decision_id}
        rec = self._projection_from_db(conn, decision_id)
        if rec:
            if rec.get("replacement_id"):
                ids.add(rec["replacement_id"])
            for c in rec.get("conflicts") or []:
                ids.add(c["other_id"])
        for did in ids:
            row = conn.execute(
                "select projection_json from current_records where decision_id=?",
                (did,),
            ).fetchone()
            if row is None:
                continue
            if len(row["projection_json"].encode("utf-8")) > MAX_GET_RESPONSE:
                raise LedgerError("LIMIT_EXCEEDED")

    def _projection_from_db(self, conn: sqlite3.Connection, decision_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "select projection_json from current_records where decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return parse_json(row["projection_json"])

    def _rebuild_projections(self, conn: sqlite3.Connection) -> None:
        conn.execute("delete from current_records")
        conn.execute("delete from search_fts")
        conn.execute("delete from links")
        conn.execute("delete from fix_links")
        ids = [r[0] for r in conn.execute("select distinct decision_id from events")]
        reduced = {did: self._reduce_decision(conn, did) for did in ids}
        rels = self._relations(reduced)
        for did, state in reduced.items():
            self._write_projection(conn, did, state, rels)

    def _rebuild_one(self, conn: sqlite3.Connection, decision_id: str) -> None:
        ids = [r[0] for r in conn.execute("select distinct decision_id from events")]
        reduced = {did: self._reduce_decision(conn, did) for did in ids}
        rels = self._relations(reduced)
        if decision_id in reduced:
            self._write_projection(conn, decision_id, reduced[decision_id], rels)
        for did, state in reduced.items():
            if did == decision_id:
                continue
            conflicts = rels["conflicts"].get(did, [])
            if any(c["other_id"] == decision_id for c in conflicts) or rels["replacement"].get(did) == decision_id:
                self._write_projection(conn, did, state, rels)

    def _reduce_decision(self, conn: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
        rows = conn.execute(
            "select revision, event_json, ingest_kind, received_at, scope_id from events where decision_id=? order by revision",
            (decision_id,),
        ).fetchall()
        if not rows:
            raise LedgerError("CORRUPT_LEDGER")
        scopes = {r["scope_id"] for r in rows}
        if len(scopes) != 1:
            raise LedgerError("CORRUPT_LEDGER")
        scope_id = next(iter(scopes))
        tombs = {
            r["revision"]
            for r in conn.execute(
                "select revision from tombstones where kind='event' and decision_id=?",
                (decision_id,),
            )
        }
        present = {r["revision"] for r in rows}
        max_rev = max(present | tombs)
        for rev in range(1, max_rev + 1):
            if rev not in present and rev not in tombs:
                raise LedgerError("INVALID_TRANSITION")
        body = None
        outcome = None
        created_at = None
        imported = False
        last_actor = None
        last_channel = None
        last_received = None
        last_ingest = "local"
        evidence_event_ingest = "local"
        imported_evidence_ids: set[str] = set()
        task_outcomes: dict[str, dict[str, Any]] = {}
        approval_event_ingest = "local"
        kind = None
        superseded = False
        replacement_id = None
        openings: list[dict[str, Any]] = []
        resolved_openings: set[str] = set()
        for row in rows:
            event = parse_json(row["event_json"])
            ingest = row["ingest_kind"]
            if ingest == "import":
                imported = True
            last_received = row["received_at"]
            last_ingest = ingest
            last_actor = event["provenance"]["actor_id"]
            last_channel = "import" if ingest == "import" else event["provenance"]["channel"]
            et = event["event_type"]
            payload = event["payload"]
            if et == "created":
                body = payload["body"]
                outcome = payload["outcome"]
                created_at = event["recorded_at"]
                kind = body["kind"]
                evidence_event_ingest = ingest
                approval_event_ingest = ingest
                supplied = self._selected_evidence(outcome, body)
                if ingest == "import":
                    imported_evidence_ids |= self._evidence_identities(supplied)
            elif et == "outcome":
                prev_selected = self._selected_evidence(outcome, body)
                supplied = payload.get("evidence") or []
                outcome = payload
                if _counts_as_use(payload):
                    task_outcomes[payload["task_id"]] = payload
                evidence_event_ingest = self._supplied_evidence_ingest(
                    prev_ingest=evidence_event_ingest,
                    prev_selected=prev_selected,
                    supplied=supplied,
                    ingest=ingest,
                )
                if ingest == "import":
                    imported_evidence_ids |= self._evidence_identities(supplied)
            elif et == "corrected":
                if payload["body"]["kind"] != kind:
                    raise LedgerError("INVALID_TRANSITION")
                prev_selected = self._selected_evidence(outcome, body)
                body = payload["body"]
                supplied = payload["body"].get("evidence") or []
                if supplied and not (outcome and outcome.get("evidence")):
                    evidence_event_ingest = self._supplied_evidence_ingest(
                        prev_ingest=evidence_event_ingest,
                        prev_selected=prev_selected,
                        supplied=supplied,
                        ingest=ingest,
                    )
                approval_event_ingest = ingest
                if ingest == "import":
                    imported_evidence_ids |= self._evidence_identities(supplied)
            elif et == "superseded":
                if superseded:
                    raise LedgerError("INVALID_TRANSITION")
                superseded = True
                replacement_id = payload["replacement_id"]
            elif et == "conflict_opened":
                openings.append(
                    {"other_id": payload["other_id"], "opened_event_id": event["event_id"]}
                )
            elif et == "conflict_resolved":
                oid = payload["opened_event_id"]
                if oid in resolved_openings:
                    raise LedgerError("INVALID_TRANSITION")
                resolved_openings.add(oid)
                openings = [o for o in openings if o["opened_event_id"] != oid]
            elif et == "attested":
                assert body is not None
                body = dict(body)
                body["approval"] = payload["approval"]
                approval_event_ingest = ingest
        assert body is not None and outcome is not None and created_at is not None
        successes = sum(1 for item in task_outcomes.values() if item["status"] == "success")
        failures = sum(1 for item in task_outcomes.values() if item["status"] == "failed")
        inconclusive = sum(1 for item in task_outcomes.values() if item["status"] == "inconclusive")
        sample_count = successes + failures
        return {
            "decision_id": decision_id,
            "revision": max_rev,
            "body": body,
            "outcome": outcome,
            "confidence": {
                "score": successes / sample_count if sample_count else None,
                "successes": successes,
                "failures": failures,
                "inconclusive": inconclusive,
                "sample_count": sample_count,
            },
            "lifecycle": "superseded" if superseded else "active",
            "replacement_id": replacement_id,
            "openings": openings,
            "resolved_openings": resolved_openings,
            "created_at": created_at,
            "updated_at": last_received,
            "imported": imported,
            "last_actor": last_actor,
            "last_channel": last_channel,
            "evidence_ingest": evidence_event_ingest,
            "imported_evidence_ids": imported_evidence_ids,
            "approval_ingest": approval_event_ingest,
            "last_ingest": last_ingest,
            "scope_id": scope_id,
        }

    def _relations(self, reduced: dict[str, dict[str, Any]]) -> dict[str, Any]:
        resolved: set[str] = set()
        for state in reduced.values():
            resolved |= state["resolved_openings"]
        conflicts: dict[str, list[dict[str, Any]]] = {did: [] for did in reduced}
        pair_open: dict[tuple[str, str], str] = {}
        for did, state in reduced.items():
            for o in state["openings"]:
                if o["opened_event_id"] in resolved:
                    continue
                pair = tuple(sorted([did, o["other_id"]]))
                if pair in pair_open and pair_open[pair] != o["opened_event_id"]:
                    raise LedgerError("INVALID_TRANSITION")
                pair_open[pair] = o["opened_event_id"]
        for (a, b), opened in pair_open.items():
            if a in reduced:
                conflicts[a].append({"other_id": b, "opened_event_id": opened})
            if b in reduced:
                conflicts[b].append({"other_id": a, "opened_event_id": opened})
        for did in conflicts:
            conflicts[did] = sorted(conflicts[did], key=lambda c: c["other_id"])
            if len(conflicts[did]) > MAX_CONFLICTS:
                raise LedgerError("LIMIT_EXCEEDED")
        replacement = {did: reduced[did]["replacement_id"] for did in reduced}
        return {"conflicts": conflicts, "replacement": replacement}

    def _write_projection(
        self,
        conn: sqlite3.Connection,
        decision_id: str,
        state: dict[str, Any],
        rels: dict[str, Any],
    ) -> None:
        body = dict(state["body"])
        outcome = dict(state["outcome"])
        imported_ids = state.get("imported_evidence_ids") or set()
        body["evidence"] = [self._cap_imported_item(e, imported_ids) for e in body.get("evidence") or []]
        outcome["evidence"] = [self._cap_imported_item(e, imported_ids) for e in outcome.get("evidence") or []]
        evidence_state = self._evidence_state(outcome, body, state["evidence_ingest"])
        approval = dict(body["approval"])
        if state["approval_ingest"] == "import" and approval["state"] == "owner_attested":
            approval["state"] = "self_reported"
        body["approval"] = approval
        staleness = "flagged" if evidence_state in {"invalid", "missing"} else "unchecked"
        conflicts = []
        for c in rels["conflicts"].get(decision_id, []):
            other = conn.execute(
                "select sensitivity from current_records where decision_id=?",
                (c["other_id"],),
            ).fetchone()
            # during rebuild current_records may be empty; include structurally
            conflicts.append(c)
        record = {
            "decision_id": decision_id,
            "revision": state["revision"],
            "body": body,
            "outcome": outcome,
            "confidence": state["confidence"],
            "lifecycle": state["lifecycle"],
            "replacement_id": state["replacement_id"],
            "conflicts": conflicts,
            "evidence_state": evidence_state,
            "staleness": staleness,
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
            "provenance": {
                "actor_id": state["last_actor"],
                "channel": state["last_channel"],
            },
            "imported": state["imported"],
        }
        encoded = canonical_json(record)
        conn.execute(
            "insert or replace into current_records(decision_id,scope_id,revision,sensitivity,lifecycle,updated_at,projection_json) "
            "values (?,?,?,?,?,?,?)",
            (
                decision_id,
                state["scope_id"],
                state["revision"],
                body["sensitivity"],
                state["lifecycle"],
                state["updated_at"],
                encoded,
            ),
        )
        self._write_links(conn, decision_id, body)
        conn.execute("delete from search_fts where decision_id=?", (decision_id,))
        if body["sensitivity"] == "model_safe":
            text = " ".join(
                [
                    body["summary"],
                    body["action"],
                    body["rationale"],
                    body["applicability"],
                    *body["constraints"],
                    *body["entities"],
                ]
            )
            conn.execute(
                "insert into search_fts(decision_id, text) values (?,?)",
                (decision_id, text),
            )

    def _cap_evidence(self, item: dict[str, Any]) -> dict[str, Any]:
        out = dict(item)
        if out["state"] == "reported_verified":
            out["state"] = "unverified"
        return out

    def _cap_imported_item(self, item: dict[str, Any], imported_ids: set[str]) -> dict[str, Any]:
        ident = canonical_json(
            {"ref": item["ref"], "revision": item["revision"], "sha256": item["sha256"]}
        )
        if ident in imported_ids:
            return self._cap_evidence(item)
        return dict(item)

    def _evidence_state(self, outcome: dict[str, Any], body: dict[str, Any], ingest: str) -> str:
        ev = outcome["evidence"] if outcome.get("evidence") else body.get("evidence") or []
        if not ev:
            state = "none"
        elif any(e["state"] in {"invalid", "missing"} for e in ev):
            state = "invalid" if any(e["state"] == "invalid" for e in ev) else "invalid"
            if any(e["state"] == "invalid" for e in ev) or any(e["state"] == "missing" for e in ev):
                state = "invalid"
        elif any(e["state"] == "unverified" for e in ev):
            state = "unverified"
        elif ev and all(e["state"] == "reported_verified" for e in ev):
            state = "reported_verified"
        else:
            state = "none"
        if ingest == "import" and state == "reported_verified":
            return "unverified"
        return state

    @staticmethod
    def _selected_evidence(outcome: dict[str, Any] | None, body: dict[str, Any] | None) -> list[dict[str, Any]]:
        if outcome and outcome.get("evidence"):
            return outcome["evidence"]
        if body and body.get("evidence"):
            return body["evidence"]
        return []

    @staticmethod
    def _evidence_identities(items: list[dict[str, Any]]) -> set[str]:
        return {
            canonical_json({"ref": item["ref"], "revision": item["revision"], "sha256": item["sha256"]})
            for item in items
        }

    @staticmethod
    def _supplied_evidence_ingest(
        *,
        prev_ingest: str,
        prev_selected: list[dict[str, Any]],
        supplied: list[dict[str, Any]],
        ingest: str,
    ) -> str:
        if not supplied:
            return prev_ingest
        if ingest == "local" and prev_ingest == "import":
            if Store._evidence_identities(supplied) <= Store._evidence_identities(prev_selected):
                return "import"
        return ingest

    def _get_record(self, conn: sqlite3.Connection, decision_id: str, *, mcp: bool) -> dict[str, Any] | None:
        row = conn.execute(
            "select projection_json, scope_id, sensitivity from current_records where decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        if row["scope_id"] != self.binding["scope_id"]:
            return None
        rec = parse_json(row["projection_json"])
        if mcp and rec["body"]["sensitivity"] != "model_safe":
            return None
        rec = dict(rec)
        visible_conflicts = []
        for c in rec.get("conflicts") or []:
            other = conn.execute(
                "select sensitivity, scope_id from current_records where decision_id=?",
                (c["other_id"],),
            ).fetchone()
            if other is None:
                continue
            if other["scope_id"] != self.binding["scope_id"]:
                continue
            if mcp and other["sensitivity"] != "model_safe":
                continue
            visible_conflicts.append(c)
        rec["conflicts"] = visible_conflicts
        if rec.get("replacement_id"):
            other = conn.execute(
                "select sensitivity, scope_id from current_records where decision_id=?",
                (rec["replacement_id"],),
            ).fetchone()
            if (
                other is None
                or other["scope_id"] != self.binding["scope_id"]
                or (mcp and other["sensitivity"] != "model_safe")
            ):
                rec["replacement_id"] = None
        return rec

    def _summary(self, rec: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision_id": rec["decision_id"],
            "revision": rec["revision"],
            "summary": rec["body"]["summary"],
            "applicability": truncate_unicode(rec["body"]["applicability"], 160),
            "outcome": rec["outcome"]["status"],
            "confidence": rec["confidence"],
            "evidence_state": rec["evidence_state"],
            "approval_state": rec["body"]["approval"]["state"],
            "lifecycle": rec["lifecycle"],
            "has_conflict": bool(rec.get("conflicts")),
            "has_replacement": rec.get("replacement_id") is not None,
            "staleness": rec["staleness"],
            "flag": self._visible_flag(rec),
        }

    def _visible_flag(self, rec: dict[str, Any]) -> str | None:
        if rec["lifecycle"] == "superseded":
            replacement = rec.get("replacement_id")
            if replacement:
                return f"superseded_by {replacement}"
            return "superseded"
        if rec["outcome"]["status"] == "failed":
            return "status failed"
        return None

    def _outcome_rank(self, rec: dict[str, Any]) -> int:
        # Latest outcome status, with or without task_id. Confidence is separate.
        # success, then pending or unknown, then abandoned or inconclusive,
        # then failed or superseded.
        if rec["lifecycle"] == "superseded" or rec["outcome"]["status"] == "failed":
            return 3
        if rec["outcome"]["status"] in {"abandoned", "inconclusive"}:
            return 2
        if rec["outcome"]["status"] == "success":
            return 0
        return 1

    def _rank(self, rec: dict[str, Any], parsed: dict[str, Any], tokens: list[str]) -> tuple:
        ents = rec["body"]["entities"]
        entity_hits = sum(1 for e in parsed["entities"] if e in ents)
        cons = rec["body"]["constraints"]
        constraint_hits = sum(1 for c in parsed["constraints"] if c in cons)
        ev = rec["evidence_state"]
        ev_rank = 0 if ev == "reported_verified" else 1 if ev in {"unverified", "none"} else 2
        blob = " ".join(
            [
                rec["body"]["summary"],
                rec["body"]["action"],
                rec["body"]["rationale"],
                rec["body"]["applicability"],
                *rec["body"]["constraints"],
                *rec["body"]["entities"],
            ]
        ).casefold()
        token_hits = sum(1 for t in set(tokens) if t in blob)
        return (
            -entity_hits,
            -constraint_hits,
            self._outcome_rank(rec),
            ev_rank,
            -token_hits,
            -_ts_key(rec["updated_at"]),
            rec["decision_id"],
        )


def _ts_key(ts: str) -> int:
    digits = "".join(ch for ch in ts if ch.isdigit())
    return int(digits) if digits else 0


class _Txn:
    def __init__(self, store: Store, conn: sqlite3.Connection) -> None:
        self.store = store
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        try:
            self.store._begin(self.conn)
        except BaseException:
            self.store._lock.release()
            raise
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc is None:
                self.conn.execute("commit")
            else:
                try:
                    self.conn.execute("rollback")
                except sqlite3.Error:
                    pass
        finally:
            self.store._lock.release()
        return False


def init_ledger(
    plugin_data: Path,
    ledger_dir: Path | None,
    scope_id: str,
    actor_id: str,
) -> dict[str, Any]:
    scope_id = require_scope(scope_id)
    actor_id = require_scope(actor_id)
    plugin_data = _refuse_network(plugin_data)
    if binding_path(plugin_data).exists():
        raise LedgerError("INVALID_TRANSITION")
    if ledger_dir is None:
        ledger_dir = plugin_data / "ledger"
    ledger_dir = _refuse_network(ledger_dir)
    if not ledger_dir.is_absolute():
        raise LedgerError("INVALID_ARGUMENT")
    db_path = ledger_dir / "ledger.sqlite3"
    if db_path.exists():
        raise LedgerError("INVALID_TRANSITION")
    _posix_secure_dir(ledger_dir)
    ledger_id = new_uuid()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("pragma foreign_keys=ON")
        conn.execute("pragma journal_mode=DELETE")
        conn.execute("pragma synchronous=FULL")
        conn.execute("pragma secure_delete=ON")
        conn.executescript(DDL)
        conn.execute("pragma user_version=1")
        conn.execute(
            "insert into ledger_meta(singleton, ledger_id, maintenance, maintenance_target, schema_version) values (1,?,null,null,1)",
            (ledger_id,),
        )
        conn.execute("insert into scopes(scope_id) values (?)", (scope_id,))
    finally:
        conn.close()
    os.chmod(db_path, 0o600)
    binding = {
        "schema_version": 1,
        "ledger_dir": str(ledger_dir),
        "ledger_id": ledger_id,
        "scope_id": scope_id,
        "actor_id": actor_id,
    }
    write_binding(plugin_data, binding, replace=False)
    return binding


def bind_ledger(
    plugin_data: Path,
    ledger_dir: Path,
    ledger_id: str,
    scope_id: str,
    actor_id: str,
    *,
    replace: bool,
) -> dict[str, Any]:
    ledger_dir = _refuse_network(ledger_dir)
    ledger_id = require_uuid(ledger_id)
    scope_id = require_scope(scope_id)
    actor_id = require_scope(actor_id)
    db_path = ledger_dir / "ledger.sqlite3"
    _posix_check_dir(ledger_dir)
    _posix_check_file(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("pragma foreign_keys=ON")
        row = conn.execute(
            "select ledger_id, schema_version from ledger_meta where singleton=1"
        ).fetchone()
        if row is None or row[0] != ledger_id:
            raise LedgerError("UNAVAILABLE")
        if row[1] != SCHEMA_VERSION:
            raise LedgerError("UPGRADE_REQUIRED")
        found = conn.execute("select 1 from scopes where scope_id=?", (scope_id,)).fetchone()
        if found is None:
            raise LedgerError("UNAVAILABLE")
    finally:
        conn.close()
    binding = {
        "schema_version": 1,
        "ledger_dir": str(ledger_dir),
        "ledger_id": ledger_id,
        "scope_id": scope_id,
        "actor_id": actor_id,
    }
    write_binding(plugin_data, binding, replace=replace)
    return binding
