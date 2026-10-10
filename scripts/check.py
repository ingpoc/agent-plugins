#!/usr/bin/env python3
"""Repo checks. Pre-commit and CI run this file; do not add a parallel lint list.

Owner of portable package shape: same rules as scripts/create_agent_plugin.py --validate.
Owner of context-ledger regressions: plugins/context-ledger/tests/test_contract.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
CURSOR_PLUGIN_KEYS = {"name", "version", "description", "mcpServers"}
CURSOR_MCP_REL = "./.cursor-plugin/mcp.json"
CURSOR_ENOENT = "Cursor resolves ./ against the workspace -> ENOENT"
# Component fields on a marketplace entry shadow the package's own files.
CURSOR_COMPONENT_FIELDS = {"mcpServers", "skills", "rules", "agents", "commands", "hooks"}
FULL_TRIGGERS = (
    "scripts/check.py",
    "AGENTS.md",
    ".github/workflows/ci.yml",
    ".githooks/pre-commit",
    *CATALOGS,
)
PLUGIN_TESTS = {
    "context-ledger": [
        sys.executable,
        "tests/test_contract.py",
        "-q",
    ],
    "comet-control": [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        "plugin/comet_control/tests",
        "-q",
    ],
    "agent-computer-use": [
        sys.executable,
        "-m",
        "unittest",
        "skills/macos-cua/tests/test_plugin_package.py",
        "skills/macos-cua/tests/test_jev_act.py",
        "-q",
    ],
}

# Comet broker/isolation suites import these at module load; missing → soft-skip.
COMET_OPTIONAL_IMPORTS = ("websockets", "PIL")


def comet_optional_deps_ok() -> bool:
    for name in COMET_OPTIONAL_IMPORTS:
        try:
            __import__(name)
        except ImportError:
            return False
    return True


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


def selected_plugins(fast: bool) -> list[Path]:
    plugins = plugin_dirs()
    if not fast:
        return plugins
    staged = staged_paths()
    if not staged:
        return plugins
    if any(path in FULL_TRIGGERS or path.startswith(".github/") for path in staged):
        return plugins
    wanted = set()
    for path in staged:
        parts = Path(path).parts
        if len(parts) >= 2 and parts[0] == "plugins":
            wanted.add(parts[1])
    if not wanted:
        return plugins
    return [path for path in plugins if path.name in wanted]


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
        root_command = str(root_server.get("command") or "")
        expected = "${CURSOR_PLUGIN_ROOT}/" + root_command[2:]
        if server.get("type") != "stdio":
            errors.append(f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.type must be stdio.")
        if server.get("command") != expected:
            errors.append(
                f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.command must be {expected}."
            )
        missing = object()
        if server.get("args", missing) != root_server.get("args", missing):
            errors.append(
                f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.args must match root mcp.json."
            )
        if server.get("cwd") != "${CURSOR_PLUGIN_ROOT}":
            errors.append(
                f"{root}: .cursor-plugin/mcp.json mcpServers.{name}.cwd must be "
                "${CURSOR_PLUGIN_ROOT}."
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


def run_tests(plugin: Path) -> int:
    argv = PLUGIN_TESTS.get(plugin.name)
    if not argv:
        return 0
    if plugin.name == "comet-control" and not comet_optional_deps_ok():
        print(
            "skip comet-control tests: missing optional deps "
            f"({', '.join(COMET_OPTIONAL_IMPORTS)}); "
            "CI installs websockets+Pillow before check.py",
            flush=True,
        )
        return 0
    env = os.environ.copy()
    env["PYTHONPATH"] = str(plugin)
    env["PYTHONNOUSERSITE"] = "1"
    env["AGENT_PLUGINS_COLLECTION"] = str(ROOT)
    print(f"test {plugin.name}", flush=True)
    proc = subprocess.run(argv, cwd=plugin, env=env)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="Changed plugins only (pre-commit).")
    args = parser.parse_args()
    errors = check_catalogs()
    plugins = selected_plugins(args.fast)
    for plugin in plugins:
        print(f"validate {plugin.relative_to(ROOT)}", flush=True)
        errors.extend(validate_plugin(plugin))
    errors.extend(check_host_dest())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    for plugin in plugins:
        code = run_tests(plugin)
        if code != 0:
            return code
    print("check ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
