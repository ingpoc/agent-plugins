#!/usr/bin/env python3
"""Repo checks. CI runs this file and the pre-push hook runs --validate-only; do not add a parallel lint list.

Owner of portable package shape: same rules as scripts/create_agent_plugin.py --validate.
Owner of context-ledger regressions: plugins/context-ledger/tests/test_contract.py.

Every plugins/ and scripts/ test_*.py that defines unittest.TestCase, outside
VCS, SwiftPM checkouts, and caches, is executed. A path with a tests/ segment
is unittest discover of that file. Other test_*.py files run as a single
module. test_*.py scripts that only provide main() are reported as
script_files and are not counted as a passing run. Swift packages that declare
.testTarget run on macOS. Off macOS, CUAService is an allow-listed harness skip.
plugins/**/integration_*.py are opt-in live suites that declare
INTEGRATION_REQUIRES. Each one prints "integration: skipped <path>, needs
<requires>" unless AGENT_PLUGINS_INTEGRATION=1, which runs it under
run_strict.py, where a skip or a missing service fails.

--fast tests only the plugins touched by the index (pre-commit shape).
--changed-since REF (or AGENT_PLUGINS_CHECK_BASE) tests only the plugins the
diff REF...HEAD touches; scripts/, CI files, catalogs and any path outside
plugins/ and docs run everything. Collection scripts/ tests always run, every
package is always validated, and a selection that would run no test fails.
--jobs N runs test processes in parallel (default: CPU count, never more).
--validate-only runs the manifest and catalog contract checks, no tests (the
pre-push hook uses it).
--audit-tests prints one JSON line of found/run/skip counts and exits 0
without running tests.

Each Python test process writes Samantha activity to a temporary log, never the live dashboard feed.

comet-control tests need `pip install 'websockets==16.0' Pillow`.
context-ledger HTTP tests need `pip install 'mcp==2.2.0'` when this
interpreter cannot import mcp.server.mcpserver.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import check_inventory
import release_agent_computer_use

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
CWD_RE = re.compile(r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))")
CLOSED_MANIFEST = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
README_AXES = (
    "Reliability",
    "Robustness",
    "Context efficiency",
    "Speed",
    "Efficiency",
)
CATALOGS = (
    ".cursor-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    ".grok-plugin/marketplace.json",
)
# Claude Code catalog. Lists only plugins that ship .claude-plugin/, not the full set.
CLAUDE_CATALOG = ".claude-plugin/marketplace.json"
CLAUDE_MCP_REL = "./.claude-plugin/mcp.json"
CURSOR_PLUGIN_KEYS = {"name", "version", "description", "mcpServers", "hooks"}
CURSOR_MCP_REL = "./.cursor-plugin/mcp.json"
GROK_MCP_REL = "./.mcp.json"
CURSOR_ENOENT = "Cursor resolves ./ against the workspace -> ENOENT"
# Component fields on a marketplace entry shadow the package's own files.
CURSOR_COMPONENT_FIELDS = {"mcpServers", "skills", "rules", "agents", "commands", "hooks"}
FULL_TRIGGERS = (
    "scripts/check.py",
    "AGENTS.md",
    ".github/workflows/ci.yml",
    ".githooks/pre-push",
    "scripts/prepush.py",
    *CATALOGS,
    CLAUDE_CATALOG,
)
def plugin_dirs() -> list[Path]:
    return sorted(path.parent for path in (ROOT / "plugins").glob("*/plugin.json"))


def staged_paths() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [item for item in proc.stdout.split("\0") if item]


def changed_paths(base: str) -> list[str] | None:
    """Paths changed on HEAD since its merge base with base; None when git can't say."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "-z", f"{base}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return [item for item in proc.stdout.split("\0") if item]


# Paths no plugin test reads (docs; the Render-hosted services/ have no tests here).
# Everything else outside plugins/<name>/ runs all plugins.
NO_PLUGIN_INPUT_PREFIXES = ("docs/", "services/")
NO_PLUGIN_INPUT_FILES = {"README.md", "LICENSE", "render.yaml"}


def plugins_for_paths(paths: list[str]) -> tuple[list[Path], str]:
    """Plugins a change set can break, plus the reason, failing safe to all."""
    plugins = plugin_dirs()
    names = {plugin.name for plugin in plugins}
    if not paths:
        return plugins, "no changed paths; full run"
    wanted: set[str] = set()
    for path in paths:
        parts = Path(path).parts
        if path in FULL_TRIGGERS or path.startswith((".github/", ".githooks/", "scripts/")):
            return plugins, f"full run: {path} is collection tooling"
        if len(parts) >= 2 and parts[0] == "plugins":
            if parts[1] not in names:
                return plugins, f"full run: {path} is under no known plugin"
            wanted.add(parts[1])
            continue
        if path.startswith(NO_PLUGIN_INPUT_PREFIXES) or path in NO_PLUGIN_INPUT_FILES:
            continue
        return plugins, f"full run: {path} is not mapped to a plugin"
    chosen = [plugin for plugin in plugins if plugin.name in wanted]
    label = ", ".join(plugin.name for plugin in chosen) or "none (docs or services only)"
    return chosen, f"plugins touched: {label}"


def selected_plugins(fast: bool, base: str | None = None) -> tuple[list[Path], str]:
    if base:
        paths = changed_paths(base)
        if paths is None:
            return plugin_dirs(), f"full run: git diff {base}...HEAD failed"
        return plugins_for_paths(paths)
    if not fast:
        return plugin_dirs(), "full run"
    staged = staged_paths()
    if not staged:
        return plugin_dirs(), "full run: nothing staged"
    chosen, reason = plugins_for_paths(staged)
    if not chosen:
        return plugin_dirs(), "full run: staged change touches no plugin"
    return chosen, reason


def validate_mcp(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{path}: mcp.json is not valid JSON: {error.msg}"]
    extra = set(data) - {"$schema", "mcpServers"}
    if extra:
        errors.append(f"{path}: mcp.json allows only $schema and mcpServers.")
    if data.get("$schema") != MCP_SCHEMA:
        errors.append(f"{path}: mcp.json $schema must be {MCP_SCHEMA}")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        errors.append(f"{path}: mcp.json must define mcpServers.")
        return errors
    for name, server in servers.items():
        if not isinstance(server, dict):
            errors.append(f"{path}: mcpServers.{name} must be an object.")
            continue
        transport = server.get("type")
        if transport == "stdio":
            command = str(server.get("command") or "")
            if not command:
                errors.append(f"{path}: mcpServers.{name}.command is required.")
            elif command.startswith("/") or " " in command or "${" in command:
                errors.append(
                    f"{path}: mcpServers.{name}.command must be a bare name or ./ path; "
                    "no shell, no absolute path, no ${PLUGIN_ROOT}."
                )
            elif "/" in command and not command.startswith("./"):
                errors.append(f"{path}: mcpServers.{name}.command plugin paths must start with ./")
            cwd = server.get("cwd")
            if cwd is not None and not CWD_RE.match(str(cwd)):
                errors.append(
                    f"{path}: mcpServers.{name}.cwd must be ./…, ${{PLUGIN_ROOT}}, or ${{PLUGIN_DATA}}."
                )
        elif transport in {"streamable-http", "sse"}:
            if not server.get("url"):
                errors.append(f"{path}: mcpServers.{name}.url is required.")
        else:
            errors.append(f"{path}: mcpServers.{name}.type must be stdio, streamable-http, or sse.")
    return errors


def _string_values(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)


_ABSENT = object()


def _cursor_stdio_expectation(root_server: dict) -> tuple[str | None, object]:
    """Cursor override for a legacy `./…` stdio command.

    Maps to `${CURSOR_PLUGIN_ROOT}/…` with the same args. A bare `sh` resolver
    is validated by `_sh_cursor_override_errors` (no cwd; the script falls
    through to a local cache copy, then a pinned tag bootstrap).
    """
    command = str(root_server.get("command") or "")
    if command.startswith("./"):
        return "${CURSOR_PLUGIN_ROOT}/" + command[2:], root_server.get("args", _ABSENT)
    return None, _ABSENT


def _is_sh_resolver(server: dict) -> bool:
    command = str(server.get("command") or "")
    args = server.get("args")
    return bool(
        command == "sh"
        and isinstance(args, list)
        and args[:1] == ["-c"]
        and len(args) >= 4
        and isinstance(args[1], str)
        and "${" not in args[1]
        and "|| true" not in args[1]
    )


def _stdio_root_tail(args: list) -> list | None:
    try:
        root_at = args.index("${PLUGIN_ROOT}")
    except ValueError:
        return None
    if root_at < 2:
        return None
    tail = args[root_at + 1 :]
    placeholders = {"${PLUGIN_ROOT}", "${CURSOR_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}"}
    while tail and isinstance(tail[0], str) and tail[0] in placeholders:
        tail = tail[1:]
    return list(tail)


def _launcher_from_resolver(script: str) -> str | None:
    names = re.findall(r"bin/([A-Za-z0-9._-]+)", script)
    if not names or any(name != names[0] for name in names):
        return None
    return names[0]


def _sh_cursor_override_errors(root: Path, name: str, server: dict, root_server: dict) -> list[str]:
    """Cursor cloud manifest: sh, one root arg, no cwd. Same launcher and tail."""
    errors: list[str] = []
    prefix = f"{root}: .cursor-plugin/mcp.json mcpServers.{name}"
    if server.get("type") != "stdio":
        errors.append(f"{prefix}.type must be stdio.")
    if server.get("command") != "sh":
        errors.append(f"{prefix}.command must be sh.")
    if server.get("cwd") is not None:
        errors.append(f"{prefix}.cwd must be omitted.")
    args = server.get("args")
    root_args = root_server.get("args")
    if not isinstance(args, list) or not isinstance(root_args, list):
        errors.append(f"{prefix}.args must be a list.")
        return errors
    root_script = root_args[1] if len(root_args) > 1 and isinstance(root_args[1], str) else ""
    script = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
    launcher = _launcher_from_resolver(root_script)
    tail = _stdio_root_tail(root_args)
    if (
        not _is_sh_resolver(server)
        or launcher is None
        or _launcher_from_resolver(script) != launcher
        or args[2:4] != [launcher, "${CURSOR_PLUGIN_ROOT}"]
        or args[4:] != tail
    ):
        errors.append(
            f"{prefix}.args must be a sh resolver with {launcher}, "
            "${CURSOR_PLUGIN_ROOT}, and the root manifest tail."
        )
    if server.get("env") != root_server.get("env"):
        errors.append(f"{prefix}.env must match root mcp.json.")
    return errors


def _root_stdio_needs_cursor_override(servers: dict) -> bool:
    for server in servers.values():
        if not isinstance(server, dict) or server.get("type") != "stdio":
            continue
        command = server.get("command")
        if isinstance(command, str) and command.startswith("./"):
            return True
    return False


def cursor_package_errors(root: Path, manifest: dict) -> list[str]:
    """Per-package Cursor override. Root mcp.json rules stay in validate_mcp.

    A stdio command that starts with ./ must ship .cursor-plugin/plugin.json
    and .cursor-plugin/mcp.json. Cursor resolves ./ against the workspace.
    """
    errors: list[str] = []
    root_servers: dict = {}
    mcp_path = root / "mcp.json"
    if mcp_path.is_file():
        try:
            mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mcp_data = None
        if isinstance(mcp_data, dict) and isinstance(mcp_data.get("mcpServers"), dict):
            root_servers = mcp_data["mcpServers"]
    needs = _root_stdio_needs_cursor_override(root_servers)
    cursor_dir = root / ".cursor-plugin"
    plugin_path = cursor_dir / "plugin.json"
    cursor_mcp_path = cursor_dir / "mcp.json"
    present = cursor_dir.is_dir() and plugin_path.is_file() and cursor_mcp_path.is_file()
    if needs and not present:
        errors.append(
            f"{root}: root mcp.json stdio command starts with ./ but the "
            f".cursor-plugin override is missing. {CURSOR_ENOENT}."
        )
    if not cursor_dir.exists():
        return errors
    if not cursor_dir.is_dir():
        errors.append(f"{root}: .cursor-plugin must be a directory.")
        return errors
    names = sorted(path.name for path in cursor_dir.iterdir())
    if names != ["mcp.json", "plugin.json"]:
        errors.append(f"{root}: .cursor-plugin/ may contain exactly plugin.json and mcp.json.")
    if not present:
        return errors
    try:
        cursor_manifest = json.loads(plugin_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return errors + [f"{root}: .cursor-plugin/plugin.json is not valid JSON: {error.msg}"]
    if not isinstance(cursor_manifest, dict):
        return errors + [f"{root}: .cursor-plugin/plugin.json must be an object."]
    extra = set(cursor_manifest) - CURSOR_PLUGIN_KEYS
    if extra:
        errors.append(
            f"{root}: .cursor-plugin/plugin.json keys must be a subset of "
            + ", ".join(sorted(CURSOR_PLUGIN_KEYS))
            + ": "
            + ", ".join(sorted(extra))
        )
    if cursor_manifest.get("name") != root.name:
        errors.append(f"{root}: .cursor-plugin/plugin.json name must match the package directory.")
    if cursor_manifest.get("version") != manifest.get("version"):
        errors.append(
            f"{root}: .cursor-plugin/plugin.json version must match plugin.json "
            f"({manifest.get('version')})."
        )
    if cursor_manifest.get("mcpServers") != CURSOR_MCP_REL:
        errors.append(
            f"{root}: .cursor-plugin/plugin.json mcpServers must be {CURSOR_MCP_REL}."
        )
    if "hooks" in cursor_manifest and cursor_manifest["hooks"] != "./hooks/cursor-hooks.json":
        errors.append(
            f"{root}: .cursor-plugin/plugin.json hooks must be ./hooks/cursor-hooks.json."
        )
    raw = cursor_mcp_path.read_text(encoding="utf-8")
    if "${PLUGIN_ROOT}" in raw or "${PLUGIN_DATA}" in raw:
        errors.append(
            f"{root}: .cursor-plugin/mcp.json must not contain "
            "${PLUGIN_ROOT} or ${PLUGIN_DATA}."
        )
    try:
        cursor_mcp = json.loads(raw)
    except json.JSONDecodeError as error:
        return errors + [f"{root}: .cursor-plugin/mcp.json is not valid JSON: {error.msg}"]
    if not isinstance(cursor_mcp, dict) or set(cursor_mcp) != {"mcpServers"}:
        errors.append(f"{root}: .cursor-plugin/mcp.json must contain only mcpServers (no $schema).")
        return errors
    servers = cursor_mcp["mcpServers"]
    if not isinstance(servers, dict):
        errors.append(f"{root}: .cursor-plugin/mcp.json mcpServers must be an object.")
        return errors
    if set(servers) != set(root_servers):
        errors.append(f"{root}: .cursor-plugin/mcp.json server names must match root mcp.json.")
        return errors
    sh_resolvers = [
        server
        for server in root_servers.values()
        if isinstance(server, dict) and _is_sh_resolver(server)
    ]
    if sh_resolvers:
        version = str(manifest.get("version") or "")
        for path in (mcp_path, cursor_mcp_path):
            count = path.read_text(encoding="utf-8").count(version)
            if not version or count != 1:
                errors.append(
                    f"{path}: version {version} must appear exactly once "
                    "(pinned tag and cache dir share that single literal)."
                )
    for name, server in servers.items():
        root_server = root_servers[name]
        if not isinstance(server, dict) or not isinstance(root_server, dict):
            errors.append(f"{root}: .cursor-plugin/mcp.json mcpServers.{name} must be an object.")
            continue
        for value in _string_values(server):
            if value.startswith("/"):
                errors.append(
                    f"{root}: .cursor-plugin/mcp.json mcpServers.{name} has an absolute path."
                )
                break
        if root_server.get("type") != "stdio":
            if server != root_server:
                errors.append(
                    f"{root}: .cursor-plugin/mcp.json mcpServers.{name} must equal the root entry."
                )
            continue
        if _is_sh_resolver(root_server):
            errors.extend(_sh_cursor_override_errors(root, name, server, root_server))
            continue
        expected, expected_args = _cursor_stdio_expectation(root_server)
        if server.get("type") != "stdio":
            errors.append(f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.type must be stdio.")
        if expected is None or server.get("command") != expected:
            errors.append(
                f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.command must be {expected}."
            )
        if server.get("args", _ABSENT) != expected_args:
            errors.append(
                f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.args must match root mcp.json."
            )
        if server.get("cwd") != "${CURSOR_PLUGIN_ROOT}":
            errors.append(
                f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.cwd must be "
                "${CURSOR_PLUGIN_ROOT}."
            )
    return errors


def claude_package_errors(root: Path, manifest: dict) -> list[str]:
    """Optional Claude Code package. Absent for plugins that have no Claude manifest."""
    claude_dir = root / ".claude-plugin"
    if not claude_dir.exists():
        return []
    plugin_path = claude_dir / "plugin.json"
    mcp_path = claude_dir / "mcp.json"
    if not plugin_path.is_file() or not mcp_path.is_file():
        return [f"{root}: .claude-plugin/ must contain plugin.json and mcp.json."]
    errors: list[str] = []
    try:
        data = json.loads(plugin_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return errors + [f"{root}: .claude-plugin/plugin.json is not valid JSON: {error.msg}"]
    if not isinstance(data, dict):
        return errors + [f"{root}: .claude-plugin/plugin.json must be an object."]
    if data.get("name") != root.name:
        errors.append(f"{root}: .claude-plugin/plugin.json name must match the package directory.")
    if data.get("version") != manifest.get("version"):
        errors.append(
            f"{root}: .claude-plugin/plugin.json version must match plugin.json "
            f"({manifest.get('version')})."
        )
    if data.get("description") != manifest.get("description"):
        errors.append(f"{root}: .claude-plugin/plugin.json description must match plugin.json.")
    if data.get("mcpServers") != CLAUDE_MCP_REL:
        errors.append(
            f"{root}: .claude-plugin/plugin.json mcpServers must be {CLAUDE_MCP_REL}."
        )
    return errors


GROK_PLUGIN_KEYS = {
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "logo",
    "mcpServers",
}
LOGO_SUFFIXES = (".svg", ".png")


def _relative_asset_errors(root: Path, where: str, rel: object) -> list[str]:
    if not isinstance(rel, str) or not rel:
        return [f"{root}: {where} logo must be a non-empty relative path."]
    if rel.startswith("/") or "://" in rel or ".." in Path(rel).parts:
        return [f"{root}: {where} logo {rel!r} must be relative to the plugin (no /, .., or URL)."]
    if not rel.endswith(LOGO_SUFFIXES):
        return [f"{root}: {where} logo {rel!r} must be .svg or .png."]
    if not (root / rel).is_file():
        return [f"{root}: {where} logo {rel!r} does not exist."]
    return []


def grok_package_errors(root: Path, manifest: dict) -> list[str]:
    """Optional Grok manifest: catalog metadata twin of plugin.json.

    Grok picks the first of plugin.json, .grok-plugin/plugin.json and
    .claude-plugin/plugin.json, so with a root plugin.json (no mcpServers) it loads MCP
    from the root `.mcp.json` convention file and never reads this manifest's
    mcpServers. Grok expands ${GROK_PLUGIN_ROOT} (and ${CLAUDE_PLUGIN_ROOT}) there but
    not ${CURSOR_PLUGIN_ROOT} or Cursor's ${env:VAR}, so `.mcp.json` is the Cursor
    config translated, and this manifest names the same file (or nothing).
    """
    path = root / ".grok-plugin" / "plugin.json"
    if not (root / ".grok-plugin").exists():
        return []
    if not path.is_file():
        return [f"{root}: .grok-plugin/ must contain plugin.json."]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{root}: .grok-plugin/plugin.json is not valid JSON: {error.msg}"]
    if not isinstance(data, dict):
        return [f"{root}: .grok-plugin/plugin.json must be an object."]
    errors: list[str] = []
    extra = set(data) - GROK_PLUGIN_KEYS
    if extra:
        errors.append(f"{root}: .grok-plugin/plugin.json unknown keys: " + ", ".join(sorted(extra)))
    for key in ("name", "version", "description", "license"):
        if data.get(key) != manifest.get(key):
            errors.append(f"{root}: .grok-plugin/plugin.json {key} must match plugin.json.")
    if "logo" in data:
        errors.extend(_relative_asset_errors(root, ".grok-plugin/plugin.json", data["logo"]))
    cursor_mcp = root / ".cursor-plugin" / "mcp.json"
    expected_mcp = GROK_MCP_REL if (root / ".mcp.json").is_file() else None
    if data.get("mcpServers") != expected_mcp:
        errors.append(
            f"{root}: .grok-plugin/plugin.json mcpServers must be "
            f"{expected_mcp or 'absent'} (Grok's own MCP config)."
        )
    if expected_mcp:
        grok_mcp = root / ".mcp.json"
        try:
            got = json.loads(grok_mcp.read_text(encoding="utf-8"))
            want = grok_mcp_from_cursor(json.loads(cursor_mcp.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            return errors + [f"{root}: .mcp.json unreadable: {error}"]
        if got != want:
            errors.append(
                f"{root}: .mcp.json (Grok) must equal .cursor-plugin/mcp.json with "
                "${CURSOR_PLUGIN_ROOT} -> ${GROK_PLUGIN_ROOT} and ${env:VAR} -> ${VAR}."
            )
    return errors


def grok_mcp_from_cursor(cursor: object) -> object:
    """Translate Cursor-only MCP variables into the forms Grok expands."""
    if isinstance(cursor, dict):
        return {key: grok_mcp_from_cursor(value) for key, value in cursor.items()}
    if isinstance(cursor, list):
        return [grok_mcp_from_cursor(value) for value in cursor]
    if isinstance(cursor, str):
        text = cursor.replace("${CURSOR_PLUGIN_ROOT}", "${GROK_PLUGIN_ROOT}")
        return re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", r"${\1}", text)
    return cursor


def check_catalog_logos() -> list[str]:
    errors: list[str] = []
    data = json.loads((ROOT / ".cursor-plugin/marketplace.json").read_text(encoding="utf-8"))
    for entry in data.get("plugins", []):
        if not isinstance(entry, dict) or "logo" not in entry:
            continue
        source = str(entry.get("source") or "")
        errors.extend(
            _relative_asset_errors(ROOT / source, ".cursor-plugin/marketplace.json", entry["logo"])
        )
    return errors


def validate_plugin(root: Path) -> list[str]:
    errors: list[str] = []
    manifest = root / "plugin.json"
    if not manifest.is_file():
        return [f"{root}: plugin.json is missing."]
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{root}: plugin.json is not valid JSON: {error.msg}"]
    extra = set(data) - CLOSED_MANIFEST
    if extra:
        errors.append(f"{root}: plugin.json unknown top-level fields: " + ", ".join(sorted(extra)))
    if data.get("$schema") != PLUGIN_SCHEMA:
        errors.append(f"{root}: plugin.json $schema must be {PLUGIN_SCHEMA}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not (1 <= len(name) <= 64):
        errors.append(f"{root}: plugin.json name violates Agent Plugins name constraints.")
    elif name != root.name:
        errors.append(f"{root}: plugin.json name must match the package directory name.")
    version = data.get("version")
    init = root / name.replace("-", "_") / "__init__.py"
    if isinstance(version, str) and init.is_file():
        text = init.read_text(encoding="utf-8")
        if f'__version__ = "{version}"' not in text and f"__version__ = '{version}'" not in text:
            errors.append(f"{root}: {init.name} __version__ must match plugin.json version {version}.")
    skills = root / "skills"
    if not skills.is_dir() or not any(path.is_file() for path in skills.glob("*/SKILL.md")):
        errors.append(f"{root}: add at least one skills/<name>/SKILL.md file.")
    mcp = root / "mcp.json"
    if mcp.is_file():
        errors.extend(validate_mcp(mcp))
    if not (root / "AGENTS.md").is_file():
        errors.append(f"{root}: AGENTS.md is required.")
    readme = root / "README.md"
    if not readme.is_file():
        errors.append(f"{root}: README.md is required.")
    else:
        text = readme.read_text(encoding="utf-8")
        missing = [axis for axis in README_AXES if f"### {axis}" not in text]
        if missing:
            errors.append(f"{root}: README.md missing ### headings: " + ", ".join(missing))
    if (root / ".codex-plugin").exists():
        errors.append(f"{root}: .codex-plugin/ is not part of an Agent Plugin package.")
    errors.extend(cursor_package_errors(root, data))
    errors.extend(claude_package_errors(root, data))
    errors.extend(grok_package_errors(root, data))
    return errors


def catalog_names(rel: str) -> list[str]:
    data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
    return [plugin["name"] for plugin in data["plugins"]]


def check_catalogs() -> list[str]:
    errors: list[str] = []
    names = {path.name for path in plugin_dirs()}
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for name in sorted(names):
        if f"| `{name}` | `plugins/{name}/` |" not in agents:
            errors.append(f"AGENTS.md missing Plugins row for {name}")
    for rel in CATALOGS:
        listed = set(catalog_names(rel))
        if listed != names:
            errors.append(f"{rel} plugin names {sorted(listed)} != {sorted(names)}")
    cursor_catalog = json.loads((ROOT / ".cursor-plugin/marketplace.json").read_text(encoding="utf-8"))
    for entry in cursor_catalog.get("plugins", []):
        if not isinstance(entry, dict):
            errors.append(".cursor-plugin/marketplace.json: plugin entry must be an object.")
            continue
        shadow = sorted(CURSOR_COMPONENT_FIELDS & set(entry))
        if shadow:
            errors.append(
                ".cursor-plugin/marketplace.json: "
                f"{entry.get('name')} must not carry {', '.join(shadow)} (shadows the package)."
            )
    errors.extend(check_claude_catalog())
    errors.extend(check_catalog_logos())
    return errors


def check_claude_catalog() -> list[str]:
    """Claude marketplace lists the subset of plugins that ship .claude-plugin/."""
    errors: list[str] = []
    path = ROOT / CLAUDE_CATALOG
    if not path.is_file():
        return [f"{CLAUDE_CATALOG} is missing."]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{CLAUDE_CATALOG} is not valid JSON: {error.msg}"]
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return [f"{CLAUDE_CATALOG} plugins must be a list."]
    names = {plugin.name for plugin in plugin_dirs()}
    listed: list[str] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            errors.append(f"{CLAUDE_CATALOG}: plugin entry must be an object.")
            continue
        name = entry.get("name")
        if name not in names:
            errors.append(f"{CLAUDE_CATALOG} lists unknown plugin {name}.")
            continue
        listed.append(name)
        if not (ROOT / "plugins" / name / ".claude-plugin" / "plugin.json").is_file():
            errors.append(
                f"{CLAUDE_CATALOG} lists {name} but .claude-plugin/plugin.json is missing."
            )
        if entry.get("source") != f"./plugins/{name}":
            errors.append(f"{CLAUDE_CATALOG} {name} source must be ./plugins/{name}.")
    for plugin in plugin_dirs():
        if (plugin / ".claude-plugin" / "plugin.json").is_file() and plugin.name not in listed:
            errors.append(
                f"{CLAUDE_CATALOG} omits {plugin.name}, which ships .claude-plugin/."
            )
    return errors


def check_host_dest() -> list[str]:
    if os.environ.get("CI") == "true":
        return []
    errors: list[str] = []
    dests = [
        Path.home() / ".agents/plugins/context-ledger/mcp.json",
        Path.home() / ".codex/plugins/cache/personal/context-ledger/0.1.3/mcp.json",
        Path.home() / ".codex/plugins/cache/personal/context-ledger/0.1.2/mcp.json",
        Path.home() / ".codex/plugins/cache/personal/context-ledger/0.1.1/mcp.json",
    ]
    for dest in dests:
        if dest.is_file():
            errors.extend(validate_mcp(dest))
    return errors


def _missing_comet_deps() -> list[str]:
    missing: list[str] = []
    for name in ("websockets", "PIL"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    return missing


def _python_for(plugin: Path, env: dict[str, str]) -> str:
    """Prefer an installed plugin runtime; otherwise use the collection interpreter."""
    if plugin.name == "developer-observability":
        runtime = Path.home() / ".local/share/developer-observability/runtime/bin/python3"
        return str(runtime) if runtime.is_file() else sys.executable
    if plugin.name != "context-ledger":
        return sys.executable
    probe = subprocess.run(
        [sys.executable, "-c", "import mcp.server.mcpserver"],
        env=env,
        capture_output=True,
    )
    if probe.returncode == 0:
        return sys.executable
    lock = plugin / "requirements.lock"
    if not lock.is_file():
        return sys.executable
    import hashlib

    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    runtime = Path.home() / ".context-ledger" / "runtime" / digest / "bin" / "python3"
    if runtime.is_file():
        return str(runtime)
    return sys.executable


@functools.lru_cache(maxsize=1)
def _git_local_env_names():
    return frozenset(subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"], cwd=ROOT, text=True).splitlines())


def test_environment(env=None):
    # Hook repository variables must not retarget fixture or SwiftPM Git commands.
    return {key: value for key, value in (os.environ if env is None else env).items()
            if key not in _git_local_env_names()}


class Job(NamedTuple):  # not a dataclass: create_agent_plugin.py loads this file outside sys.modules
    label: str
    cmd: list[str]
    cwd: Path
    env: dict[str, str]


def _python_job(
    run: check_inventory.PythonRun, env: dict[str, str], python: str | None = None
) -> Job | None:
    """The test process for one module, or None for an allow-listed harness skip."""
    env = test_environment(env)
    rel = run.path.relative_to(ROOT).as_posix()
    rule = check_inventory.load_allowlist(check_inventory.DEFAULT_ALLOWLIST).match(
        "harness",
        check_inventory.module_id(ROOT, run.path),
        sys.platform,
        os.environ,
    )
    if rule is not None:
        print(f"allowed skip {rel}: {rule.reason}", flush=True)
        return None
    # One module per process so a re-exec or a darwin-only import cannot drop
    # the rest of its tests directory. The runner filename must not contain
    # "unittest": test_http_server re-execs `python -m unittest` when argv[0] does.
    cmd = [
        python or sys.executable,
        str(ROOT / "scripts" / "run_strict.py"),
        "--allowlist",
        str(check_inventory.DEFAULT_ALLOWLIST),
        "-q",
    ]
    if run.mode == "discover":
        assert run.start is not None
        cmd.extend(["discover", "-s", str(run.start.relative_to(run.cwd)), "-p", run.path.name])
    else:
        cmd.extend(["module", str(run.path.relative_to(run.cwd))])
    return Job(f"test {rel}", cmd, run.cwd, env)


def _run_job(job: Job, capture: bool) -> tuple[int, str, float]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="samantha-test-log-") as log_dir:
        env = dict(job.env)
        env["VOICE_CUA_ACTIVITY_LOG"] = str(Path(log_dir) / "activity.jsonl")
        if capture:
            proc = subprocess.run(
                job.cmd, cwd=job.cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            output = proc.stdout or ""
        else:
            proc = subprocess.run(job.cmd, cwd=job.cwd, env=env)
            output = ""
    return proc.returncode, output, time.monotonic() - started


def _exec_python(run: check_inventory.PythonRun, env: dict[str, str], python: str | None = None) -> int:
    """Run one module now, streaming its output (integration suites and callers that need one)."""
    job = _python_job(run, env, python)
    if job is None:
        return 0
    print(job.label, flush=True)
    return _run_job(job, capture=False)[0]


def _timings_path() -> Path:
    return Path(os.environ.get("AGENT_PLUGINS_CHECK_TIMINGS")
                or Path.home() / ".cache" / "agent-plugins" / "check-timings.json")


def _load_timings() -> dict[str, float]:
    try:
        data = json.loads(_timings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def _save_timings(timings: list[tuple[float, str]]) -> None:
    known = _load_timings()
    known.update({label: round(seconds, 1) for seconds, label in timings})
    path = _timings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(known, indent=0, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"note: could not save test timings to {path}: {exc}", flush=True)


def run_jobs(jobs: list[Job], workers: int) -> int:
    """Run test processes, at most workers at once; every job runs, first failure wins.

    In parallel, the slowest jobs of the last run start first (unknown jobs count as slow),
    so one long module does not start last and set the wall time.
    """
    if not jobs:
        print("every test was skipped: refusing a green run that tested nothing", file=sys.stderr)
        return 1
    timings: list[tuple[float, str]] = []
    failed: list[tuple[str, int]] = []
    if workers <= 1:
        for job in jobs:
            print(job.label, flush=True)
            code, _, seconds = _run_job(job, capture=False)
            timings.append((seconds, job.label))
            if code != 0:
                failed.append((job.label, code))
                break
    else:
        last = _load_timings()
        jobs = sorted(jobs, key=lambda job: -last.get(job.label, float("inf")))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_job, job, True): job for job in jobs}
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                code, output, seconds = future.result()
                timings.append((seconds, job.label))
                print(f"{job.label} ({seconds:.1f}s, exit {code})", flush=True)
                if output:
                    print(output, end="" if output.endswith("\n") else "\n", flush=True)
                if code != 0:
                    failed.append((job.label, code))
        _save_timings(timings)
    if len(timings) > 1:
        slowest = ", ".join(f"{label.removeprefix('test ')} {seconds:.1f}s"
                            for seconds, label in sorted(timings, reverse=True)[:3])
        print(f"check: ran {len(timings)} test processes, {workers} at a time; slowest: {slowest}",
              flush=True)
    if failed:
        print("failed: " + ", ".join(f"{label} (exit {code})" for label, code in failed),
              file=sys.stderr)
        return failed[0][1] or 1
    return 0


def plugin_jobs(plugin: Path) -> list[Job] | None:
    if plugin.name == "comet-control" and _missing_comet_deps():
        print(
            "comet-control tests need websockets and Pillow "
            f"(missing {', '.join(_missing_comet_deps())}). "
            "pip install 'websockets==16.0' Pillow",
            file=sys.stderr,
        )
        return None
    env = os.environ.copy()
    env["PYTHONPATH"] = str(plugin)
    env["PYTHONNOUSERSITE"] = "1"
    env["AGENT_PLUGINS_COLLECTION"] = str(ROOT)
    # Tests never post to a live island; island tests that need publishing opt back in.
    env["ACU_ISLAND_ACTIVITY"] = "0"
    python = _python_for(plugin, env)
    jobs: list[Job] = []
    for run in check_inventory.python_runs(ROOT, {plugin.name}):
        if run.path.relative_to(ROOT).parts[0] != "plugins":
            continue
        job = _python_job(run, env, python)
        if job is not None:
            jobs.append(job)
    return jobs


def run_tests(plugin: Path) -> int:
    jobs = plugin_jobs(plugin)
    return 1 if jobs is None else run_jobs(jobs, 1) if jobs else 0


def collection_jobs() -> list[Job]:
    env = os.environ.copy()
    jobs: list[Job] = []
    for run in check_inventory.python_runs(ROOT):
        if run.path.relative_to(ROOT).parts[0] != "scripts":
            continue
        job = _python_job(run, env)
        if job is not None:
            jobs.append(job)
    return jobs


def swift_jobs(plugins: list[Path], workers: int = 1) -> list[Job] | None:
    """SwiftPM test jobs. AGENT_PLUGINS_SWIFT_SCRATCH keeps a warm build dir per package.

    With workers > 1, XCTest runs in parallel on half the cores; the Python jobs share the rest.
    """
    names = {plugin.name for plugin in plugins}
    allow = check_inventory.load_allowlist(check_inventory.DEFAULT_ALLOWLIST)
    scratch_root = os.environ.get("AGENT_PLUGINS_SWIFT_SCRATCH")
    jobs: list[Job] = []
    for package in check_inventory.swift_packages(ROOT):
        if package.relative_to(ROOT).parts[1] not in names:
            continue
        rel = package.relative_to(ROOT).as_posix()
        if sys.platform != "darwin":
            rule = allow.match("harness", check_inventory.swift_id(ROOT, package), sys.platform, os.environ)
            if rule is not None:
                print(f"allowed skip {check_inventory.swift_id(ROOT, package)}: {rule.reason}", flush=True)
                continue
            print(f"swift package {rel} did not run and is not allow-listed", file=sys.stderr)
            return None
        cmd = ["swift", "test", "--package-path", str(package), "-q"]
        if workers > 1:
            cmd.extend(["--parallel", "--num-workers", str(max(1, workers // 2))])
        if scratch_root:
            cmd.extend(["--scratch-path", str(Path(scratch_root) / rel.replace("/", "_"))])
        jobs.append(Job(f"test swift {rel}", cmd, ROOT, test_environment()))
    return jobs


def run_swift(plugins: list[Path]) -> int:
    jobs = swift_jobs(plugins)
    return 1 if jobs is None else run_jobs(jobs, 1) if jobs else 0


def run_integration(plugins: list[Path]) -> int:
    names = {plugin.name for plugin in plugins}
    enabled = os.environ.get(check_inventory.INTEGRATION_ENV) == "1"
    for suite in check_inventory.integration_suites(ROOT, names):
        rel = suite.path.relative_to(ROOT).as_posix()
        if not suite.requires:
            print(f"integration suite {rel} must declare INTEGRATION_REQUIRES", file=sys.stderr)
            return 1
        if not enabled:
            print(
                f"integration: skipped {rel}, needs {suite.requires} "
                f"(set {check_inventory.INTEGRATION_ENV}=1 to run)",
                flush=True,
            )
            continue
        plugin = ROOT / "plugins" / suite.path.relative_to(ROOT).parts[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(plugin)
        env["PYTHONNOUSERSITE"] = "1"
        env["AGENT_PLUGINS_COLLECTION"] = str(ROOT)
        run = check_inventory.PythonRun(suite.path, plugin, "module", None)
        print(f"integration {rel}", flush=True)
        code = _exec_python(run, env, _python_for(plugin, env))
        if code != 0:
            return code
    return 0


TRIAGE_RENDERER = ROOT / "plugins/batched-change-list/skills/batched-change-list/scripts/render.py"


def check_triage_renders() -> list[str]:
    """The current change-list page must be a fresh, untouched render of its tracker JSON.

    For each docs/triage/*-findings.json, the page named for the tracker's current PR
    (tracking.pull_request.url .../pull/N -> docs/triage/pr-N.html) must be byte-identical
    to render.py's output. Earlier PR pages are frozen snapshots and are not re-rendered.
    """
    import re
    import tempfile

    errors: list[str] = []
    triage = ROOT / "docs/triage"
    if not TRIAGE_RENDERER.is_file():
        return errors
    for data in sorted(triage.glob("*-findings.json")):
        try:
            url = (json.loads(data.read_text()).get("tracking", {}).get("pull_request") or {}).get("url", "")
        except (OSError, ValueError) as exc:
            errors.append(f"{data.relative_to(ROOT)}: unreadable tracker ({exc})")
            continue
        match = re.search(r"/pull/(\d+)", url)
        if not match:
            continue
        report = triage / f"pr-{match.group(1)}.html"
        if not report.is_file():
            errors.append(f"{report.relative_to(ROOT)}: missing; render it from {data.name} with render.py")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / report.name
            proc = subprocess.run([sys.executable, str(TRIAGE_RENDERER), str(data), str(fresh)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                errors.append(f"{data.relative_to(ROOT)}: render.py failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
            elif fresh.read_bytes() != report.read_bytes():
                errors.append(f"{report.relative_to(ROOT)}: differs from a fresh render of {data.name}; "
                              "regenerate with render.py (no hand edits)")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate packages and run discovered tests.")
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Validate and test only plugins touched by the index. "
            "Test execution uses the same discovery rules, scoped to those plugins. "
            "Collection scripts/ tests still run."
        ),
    )
    parser.add_argument(
        "--audit-tests",
        action="store_true",
        help=(
            "Print one JSON line of python and swift found, run, and skip counts, "
            "then exit 0 without running tests."
        ),
    )
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        default=os.environ.get("AGENT_PLUGINS_CHECK_BASE") or None,
        help="Test only plugins the diff REF...HEAD touches (default: $AGENT_PLUGINS_CHECK_BASE).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=int(os.environ.get("AGENT_PLUGINS_CHECK_JOBS") or 0) or None,
        help="Parallel test processes (default and maximum: CPU count; 1 is serial).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Run manifest and catalog contract checks only, no tests.",
    )
    args = parser.parse_args(argv)
    plugins, reason = selected_plugins(args.fast, args.changed_since)
    cores = os.cpu_count() or 1
    workers = max(1, min(args.jobs or cores, cores))
    if args.audit_tests:
        try:
            report = check_inventory.audit(
                ROOT,
                plugin_names={plugin.name for plugin in plugins} if args.fast else None,
            )
        except check_inventory.AllowlistError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(report))
        return 0
    errors = check_catalogs()
    try:
        release_agent_computer_use.policy(ROOT)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        errors.append(f"ACU release policy: {exc}")
    for plugin in plugin_dirs() if args.changed_since or args.validate_only else plugins:
        print(f"validate {plugin.relative_to(ROOT)}", flush=True)
        errors.extend(validate_plugin(plugin))
    errors.extend(check_host_dest())
    errors.extend(check_triage_renders())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    try:
        check_inventory.load_allowlist(check_inventory.DEFAULT_ALLOWLIST)
    except check_inventory.AllowlistError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.validate_only:
        print("validate ok", flush=True)
        return 0
    print(f"selection: {reason}", flush=True)
    jobs: list[Job] = []
    swift = swift_jobs(plugins, workers)
    if swift is None:
        return 1
    jobs.extend(swift)  # longest single job: start it first
    for plugin in plugins:
        found = plugin_jobs(plugin)
        if found is None:
            return 1
        jobs.extend(found)
    jobs.extend(collection_jobs())
    code = run_jobs(jobs, workers)
    if code != 0:
        return code
    code = run_integration(plugins)
    if code != 0:
        return code
    print("check ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
