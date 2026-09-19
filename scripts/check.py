#!/usr/bin/env python3
"""Repo checks. Pre-commit and CI run this file; do not add a parallel lint list.

Owner of portable package shape: the same rules as agent-plugin-creator --validate.
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
        "skills/macos-cua/tests/test_plugin_package.py",
        "-q",
    ],
}


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
    for forbidden in (".cursor-plugin", ".codex-plugin"):
        if (root / forbidden).exists():
            errors.append(f"{root}: {forbidden}/ is not part of an Agent Plugin package.")
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
    return errors


def check_host_dest() -> list[str]:
    if os.environ.get("CI") == "true":
        return []
    errors: list[str] = []
    dests = [
        Path.home() / ".agents/plugins/context-ledger/mcp.json",
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
