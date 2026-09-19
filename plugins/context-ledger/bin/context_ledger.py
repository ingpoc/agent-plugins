#!/usr/bin/env python3
"""Stdlib bootstrap for context-ledger. Never install dependencies at serve time."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import venv
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = PLUGIN_ROOT / "requirements.lock"
MIN_PY = (3, 11)
MAX_PY = (3, 14)
READY_NAME = ".ready"


def _err(code: str, retryable: bool = False) -> int:
    sys.stderr.write(f'{{"ok":false,"error":{{"code":"{code}","retryable":{str(retryable).lower()}}}}}\n')
    return 2


def _lock_sha() -> str:
    return hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest()


def _runtime_dir(data: Path, sha: str) -> Path:
    return data / "runtime" / sha


def _runtime_python(runtime: Path) -> Path:
    if os.name == "nt":
        return runtime / "Scripts" / "python.exe"
    return runtime / "bin" / "python"


def _ready_path(runtime: Path) -> Path:
    return runtime / READY_NAME


def _python_supported() -> bool:
    v = sys.version_info
    return MIN_PY <= (v.major, v.minor) <= MAX_PY


def _sqlite_fts5_ok() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        cur = con.execute("select sqlite_version()")
        ver = cur.fetchone()[0]
        parts = tuple(int(p) for p in ver.split(".")[:2])
        if parts < (3, 35):
            con.close()
            return False
        con.execute("create virtual table t using fts5(x)")
        con.close()
        return True
    except sqlite3.Error:
        return False


def _parse(argv: list[str]) -> tuple[Path | None, str, list[str]]:
    rest = list(argv)
    data: Path | None = None
    if rest and rest[0] == "--data":
        if len(rest) < 2:
            raise SystemExit(_err("INVALID_ARGUMENT"))
        data = Path(rest[1])
        rest = rest[2:]
    if not rest:
        raise SystemExit(_err("INVALID_ARGUMENT"))
    return data, rest[0], rest[1:]


def _resolve_data(data: Path | None) -> Path:
    if data is not None:
        return data
    env = os.environ.get("PLUGIN_DATA")
    if not env:
        raise SystemExit(_err("INVALID_ARGUMENT"))
    return Path(env)


def _require_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise SystemExit(_err("INVALID_ARGUMENT"))
    return path


def _help() -> int:
    sys.stderr.write(
        "usage: context_ledger.py --data ABSOLUTE_DIR "
        "setup|init|bind|serve|doctor|export|import|attest|purge|rebuild|migrate|"
        "resume-maintenance|scope-add|ensure-global-triggers ...\n"
    )
    return 0


LOOKUP_MARK = "**Ledger lookup**"
SAVE_MARK = "**Ledger save**"
LOOKUP_LINE = (
    "- **Ledger lookup** → `context-ledger` if history or precedent could "
    "change next decision; else skip"
)
SAVE_LINE = (
    "- **Ledger save** → `context-ledger` if settled decision, useful failure, "
    "or material update; else skip"
)


def _section_bounds(text: str, heading: str) -> tuple[int, int] | None:
    start = text.find(heading)
    if start < 0:
        return None
    line_end = text.find("\n", start)
    if line_end < 0:
        return None
    body_at = line_end + 1
    nxt = text.find("\n## ", body_at)
    end = len(text) if nxt < 0 else nxt
    return body_at, end


def _insert_in_section(
    text: str,
    heading: str,
    line: str,
    *,
    after_first_bullet: bool = False,
    before_mark: str | None = None,
) -> str | None:
    bounds = _section_bounds(text, heading)
    if bounds is None:
        return None
    body_at, end = bounds
    head, body, tail = text[:body_at], text[body_at:end], text[end:]
    if before_mark and before_mark in body:
        mark_at = body.find(before_mark)
        line_start = body.rfind("\n", 0, mark_at) + 1
        body = body[:line_start] + line + "\n" + body[line_start:]
        return head + body + tail
    if after_first_bullet:
        match = re.search(r"(?m)^- .*$", body)
        if match:
            at = match.end()
            body = body[:at] + "\n" + line + body[at:]
            return head + body + tail
    stripped = body.rstrip()
    suffix = "\n" if tail.startswith("\n## ") or not tail else ""
    body = stripped + "\n" + line + "\n" + suffix
    return head + body + tail


def _cmd_ensure_global_triggers(args: list[str]) -> int:
    agents = Path.home() / ".codex" / "AGENTS.md"
    i = 0
    while i < len(args):
        if args[i] == "--agents-md":
            if i + 1 >= len(args):
                return _err("INVALID_ARGUMENT")
            agents = _require_absolute(Path(args[i + 1]))
            i += 2
            continue
        return _err("INVALID_ARGUMENT")
    if not agents.is_file():
        sys.stderr.write('{"ok":true,"data":{"skipped":"absent"}}\n')
        return 0
    try:
        text = agents.read_text(encoding="utf-8")
    except OSError:
        return _err("UNAVAILABLE")
    added_lookup = False
    added_save = False
    if LOOKUP_MARK not in text:
        nxt = _insert_in_section(text, "## BEFORE", LOOKUP_LINE, after_first_bullet=True)
        if nxt is None:
            return _err("UNAVAILABLE")
        text = nxt
        added_lookup = True
    if SAVE_MARK not in text:
        nxt = _insert_in_section(
            text,
            "## AFTER",
            SAVE_LINE,
            before_mark="**Durable-learning",
        )
        if nxt is None:
            return _err("UNAVAILABLE")
        text = nxt
        added_save = True
    if added_lookup or added_save:
        try:
            agents.write_text(text, encoding="utf-8")
        except OSError:
            return _err("UNAVAILABLE")
    sys.stderr.write(
        '{"ok":true,"data":{"added":'
        + ("true" if added_lookup or added_save else "false")
        + ',"lookup":'
        + ("true" if added_lookup else "false")
        + ',"save":'
        + ("true" if added_save else "false")
        + "}}\n"
    )
    return 0


def _cmd_setup(data: Path, args: list[str]) -> int:
    wheelhouse: Path | None = None
    repair = False
    i = 0
    while i < len(args):
        if args[i] == "--wheelhouse":
            if i + 1 >= len(args):
                return _err("INVALID_ARGUMENT")
            wheelhouse = _require_absolute(Path(args[i + 1]))
            i += 2
            continue
        if args[i] == "--repair":
            repair = True
            i += 1
            continue
        return _err("INVALID_ARGUMENT")
    if not LOCK_PATH.is_file():
        return _err("UNAVAILABLE")
    if not _python_supported() or not _sqlite_fts5_ok():
        return _err("UPGRADE_REQUIRED")
    sha = _lock_sha()
    runtime = _runtime_dir(data, sha)
    ready = _ready_path(runtime)
    if ready.is_file():
        try:
            if ready.read_text(encoding="utf-8").strip() == sha and _runtime_python(runtime).is_file():
                sys.stderr.write('{"ok":true,"data":{"reused":true}}\n')
                return 0
        except OSError:
            return _err("UNAVAILABLE")
    if runtime.exists():
        if not repair:
            return _err("UNAVAILABLE")
        if ready.is_file():
            return _err("INVALID_TRANSITION")
        import shutil

        shutil.rmtree(runtime)
    data.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(with_pip=True, system_site_packages=False, clear=False, symlinks=True)
    builder.create(runtime)
    py = _runtime_python(runtime)
    pip_cmd = [
        str(py),
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "-r",
        str(LOCK_PATH),
        "--disable-pip-version-check",
        "--no-input",
    ]
    if wheelhouse is not None:
        pip_cmd.extend(["--no-index", "--find-links", str(wheelhouse)])
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    proc = subprocess.run(pip_cmd, cwd=str(PLUGIN_ROOT), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:] if proc.stderr else "")
        return _err("UNAVAILABLE")
    smoke = subprocess.run(
        [str(py), "-c", "import mcp, jsonschema, sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"],
        env={**env, "PYTHONPATH": str(PLUGIN_ROOT)},
        capture_output=True,
        text=True,
    )
    if smoke.returncode != 0:
        return _err("UNAVAILABLE")
    tmp = runtime / ".ready.tmp"
    tmp.write_text(sha + "\n", encoding="utf-8")
    tmp.replace(ready)
    sys.stderr.write('{"ok":true,"data":{"reused":false}}\n')
    return 0


def _runtime_ready(data: Path) -> Path | None:
    if not LOCK_PATH.is_file():
        return None
    sha = _lock_sha()
    runtime = _runtime_dir(data, sha)
    ready = _ready_path(runtime)
    py = _runtime_python(runtime)
    if not ready.is_file() or not py.is_file():
        return None
    try:
        if ready.read_text(encoding="utf-8").strip() != sha:
            return None
    except OSError:
        return None
    return py


def _reexec(py: Path, data: Path, command: str, args: list[str]) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLUGIN_ROOT)
    env["PYTHONNOUSERSITE"] = "1"
    env["CONTEXT_LEDGER_BOOTSTRAPPED"] = "1"
    argv = [str(py), "-m", "context_ledger", "--data", str(data), command, *args]
    os.execve(str(py), argv, env)
    return 2


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] in {"-h", "--help"}:
        return _help()
    try:
        data, command, args = _parse(raw)
    except SystemExit as exc:
        return int(exc.code or 2)
    if command == "setup":
        path = _require_absolute(_resolve_data(data))
        return _cmd_setup(path, args)
    if command == "ensure-global-triggers":
        _require_absolute(_resolve_data(data))
        return _cmd_ensure_global_triggers(args)
    path = _require_absolute(_resolve_data(data))
    if os.environ.get("CONTEXT_LEDGER_BOOTSTRAPPED") == "1":
        from context_ledger.__main__ import dispatch

        return dispatch(path, command, args)
    py = _runtime_ready(path)
    if py is None:
        return _err("UNAVAILABLE")
    return _reexec(py, path, command, args)


if __name__ == "__main__":
    sys.exit(main())
