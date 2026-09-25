#!/usr/bin/env python3
"""Create or validate a portable Agent Plugin and register it in the collection."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
COLLECTION_ENV = "AGENT_PLUGINS_COLLECTION"
KNOWN_COLLECTIONS = (
    Path.home() / "Documents/remote-claude/active/apps/agent-plugins",
)
DEFAULT_AUTHOR = {
    "name": "ingpoc",
    "url": "https://github.com/ingpoc",
}
DEFAULT_REPOSITORY = "https://github.com/ingpoc/agent-plugins"
DEFAULT_MCP_INSTALL = (
    "Source `mcp.json` stays portable: `command` is `./bin/<name>-mcp` "
    "(or a bare executable), never absolute, never a shell, never "
    "`${PLUGIN_ROOT}` in `command`. Prefer a `#!/bin/sh` launcher that "
    "resolves `python3`/`python` (optional `*_PYTHON` override) so Cursor's "
    "thin PATH does not ENOENT. "
    "Clients should expand `${PLUGIN_DATA}` / `${PLUGIN_ROOT}` in args/cwd; "
    "Cursor often leaves them literal — expand in the bootstrap or bake an "
    "absolute `--data` default (e.g. `~/.context-ledger/`) for Cursor dests. "
    "If Cursor: run the plugin's dest-rewrite script (e.g. "
    "`scripts/install_cursor_dest.py` or `install_harness.py cursor-plugin`) "
    "so **local + marketplace cache** `mcp.json` use the absolute dest "
    "launcher with `cwd` `./`. After marketplace re-Add/refresh, re-run "
    "(or `--rewrite-only`). Do not copy dest-absolute `command` back into "
    "source `mcp.json`. Comet-style plugins may omit `mcp.json` entirely."
)
FORBIDDEN_CLIENT_DIRS = (".cursor-plugin", ".codex-plugin")
README_AXES = (
    "Reliability",
    "Robustness",
    "Context efficiency",
    "Speed",
    "Efficiency",
)
AXIS_TO_RATING = {
    "Reliability": "reliability",
    "Robustness": "robustness",
    "Context efficiency": "token_efficiency",
    "Speed": "speed",
    "Efficiency": "efficiency",
}
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


def plugin_name(value: str) -> str:
    raw = value.strip()
    if NAME_RE.fullmatch(raw) and 1 <= len(raw) <= 64:
        return raw
    slugged = re.sub(r"[^a-z0-9.]+", "-", raw.lower()).strip("-.")
    slugged = re.sub(r"-{2,}", "-", slugged)
    slugged = re.sub(r"\.{2,}", ".", slugged)
    if not NAME_RE.fullmatch(slugged) or not (1 <= len(slugged) <= 64):
        raise ValueError(
            "Plugin name must be 1-64 chars, [a-z0-9.-], start/end alphanumeric, "
            "no -- or .. (Agent Plugins name constraints)."
        )
    return slugged


def skill_name(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not result:
        raise ValueError("Skill name must contain a letter or number.")
    return result


def is_skills_catalog(path: Path) -> bool:
    return (path / "scripts" / "plugin-deny.txt").is_file()


def refuse_skills_catalog(path: Path) -> None:
    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if is_skills_catalog(candidate):
            raise ValueError(
                "Refuse: agent-skills is ~/.agents/skills/ only. "
                "Write plugins to ingpoc/agent-plugins / ~/.agents/plugins/."
            )


def is_collection(path: Path) -> bool:
    if is_skills_catalog(path):
        return False
    return (path / "plugins").is_dir() and (path / "AGENTS.md").is_file()


def find_collection() -> Path | None:
    env = os.environ.get(COLLECTION_ENV)
    if env:
        path = Path(env).expanduser().resolve()
        if not is_collection(path):
            raise ValueError(f"{COLLECTION_ENV} is not a collection: {path}")
        return path
    for known in KNOWN_COLLECTIONS:
        if is_collection(known):
            return known
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if is_collection(candidate):
            return candidate
    return None


def fetch_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def live_schema_ids() -> tuple[str, str]:
    plugin = fetch_json(PLUGIN_SCHEMA_URL)
    mcp = fetch_json(MCP_SCHEMA_URL)
    plugin_id = (
        plugin.get("$id")
        if isinstance(plugin, dict) and isinstance(plugin.get("$id"), str)
        else PLUGIN_SCHEMA_URL
    )
    mcp_id = (
        mcp.get("$id")
        if isinstance(mcp, dict) and isinstance(mcp.get("$id"), str)
        else MCP_SCHEMA_URL
    )
    return plugin_id, mcp_id


def write_plugin_readme(
    root: Path,
    name: str,
    description: str,
    *,
    ratings: dict | None = None,
    source: str = "unmeasured",
) -> None:
    purpose = description.strip() or name
    rows = []
    for axis in README_AXES:
        key = AXIS_TO_RATING[axis]
        value = "unmeasured"
        if isinstance(ratings, dict) and ratings.get(key) is not None:
            value = str(ratings[key])
        rows.append(f"| {axis} | {value} |")
    table = "\n".join(rows)
    (root / "README.md").write_text(
        f"""# {name}

{purpose}

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

## Benchmarks

Scale 0–10 unless noted. Context efficiency maps to the rating key `token_efficiency`. Source: {source}.

| Axis | Score |
| --- | --- |
{table}

### Reliability

Repeatable pass rate. Spread penalty when p95/p50 duration > 1.5.

### Robustness

Fraction of repeats that stay on the owner path (`measured.robust`).

### Context efficiency

Proof bytes / driver RPCs vs floor (`token_efficiency`). Compact query/diff, no chat dumps.

### Speed

Wall time and per-step latency vs floors.

### Efficiency

Round-trips / AX snapshots vs floor. Prefer one asserted run over N granular calls.

Replace `unmeasured` after a real suite. Do not invent scores.
"""
    )


def write_plugin_agents(root: Path, name: str, extra: str) -> None:
    extra_block = extra.strip() or "None beyond the client load path."
    (root / "AGENTS.md").write_text(
        f"""# {name} — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.cursor-plugin/` or `.codex-plugin/` to this package.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

{extra_block}
"""
    )


def create(
    parent: Path,
    name: str,
    skill: str,
    with_mcp: bool,
    description: str,
    plugin_schema: str,
    mcp_schema: str,
) -> Path:
    root = parent / name
    if root.exists():
        raise FileExistsError(f"Already exists: {root}")
    (root / "skills" / skill).mkdir(parents=True)
    manifest: dict = {
        "$schema": plugin_schema,
        "name": name,
        "version": "0.1.0",
        "author": DEFAULT_AUTHOR,
        "homepage": DEFAULT_REPOSITORY,
        "repository": DEFAULT_REPOSITORY,
        "license": "MIT",
    }
    if description:
        manifest["description"] = description
    (root / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n")
    summary = description or "TODO"
    (root / "skills" / skill / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: {summary}\n---\n\n{summary}\n"
    )
    if with_mcp:
        (root / "mcp.json").write_text(
            json.dumps(
                {
                    "$schema": mcp_schema,
                    "mcpServers": {
                        name: {
                            "type": "stdio",
                            "command": f"./bin/{name}",
                            "cwd": "./",
                        }
                    },
                },
                indent=2,
            )
            + "\n"
        )
        launcher = root / "bin" / name
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nset -eu\necho 'replace this launcher' >&2\nexit 127\n")
        launcher.chmod(0o755)
    return root


def register_in_collection(collection: Path, name: str, extra: str) -> None:
    agents = collection / "AGENTS.md"
    text = agents.read_text()
    needle = f"| `{name}` |"
    if needle in text:
        return
    row = f"| `{name}` | `plugins/{name}/` | `plugins/{name}/AGENTS.md` |\n"
    marker = "| Name | Path | Install |"
    start = text.find(marker)
    if start == -1:
        raise ValueError(f"{agents} is missing the Plugins table.")
    lines = text[start:].splitlines(keepends=True)
    last = 0
    for index, line in enumerate(lines):
        if line.startswith("|"):
            last = index
        elif last:
            break
    insert_at = start + sum(len(line) for line in lines[: last + 1])
    agents.write_text(text[:insert_at] + row + text[insert_at:])


def validate(root: Path, plugin_schema: str, mcp_schema: str) -> list[str]:
    errors: list[str] = []
    manifest = root / "plugin.json"
    if not manifest.is_file():
        return ["plugin.json is missing from the package root."]
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError as error:
        return [f"plugin.json is not valid JSON: {error.msg}"]
    extra = set(data) - CLOSED_MANIFEST
    if extra:
        errors.append(
            "plugin.json unknown top-level fields (clients ignore, do not add): "
            + ", ".join(sorted(extra))
        )
    if data.get("$schema") != plugin_schema:
        errors.append(f"plugin.json $schema must be {plugin_schema}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not (1 <= len(name) <= 64):
        errors.append("plugin.json name violates Agent Plugins name constraints.")
    elif name != root.name:
        errors.append("plugin.json name must match the package directory name.")
    author = data.get("author")
    if author is not None:
        if not isinstance(author, dict) or set(author) - {"name", "email", "url"}:
            errors.append("plugin.json author may only contain name, email, url.")
        elif any(not isinstance(value, str) for value in author.values()):
            errors.append("plugin.json author fields must be strings.")
    skills = root / "skills"
    if not skills.is_dir() or not any(path.is_file() for path in skills.glob("*/SKILL.md")):
        errors.append("Add at least one skills/<name>/SKILL.md file.")
    mcp = root / "mcp.json"
    if mcp.is_file():
        errors.extend(_validate_mcp(mcp, mcp_schema))
    if not (root / "AGENTS.md").is_file():
        errors.append("plugins/<name>/AGENTS.md is required (install lives there, not the collection root).")
    readme = root / "README.md"
    if not readme.is_file():
        errors.append("plugins/<name>/README.md is required (benchmark axes).")
    else:
        text = readme.read_text()
        missing = [axis for axis in README_AXES if f"### {axis}" not in text]
        if missing:
            errors.append(
                "README.md must contain ### headings for "
                + ", ".join(README_AXES)
                + f" (missing: {', '.join(missing)})."
            )
    for forbidden in FORBIDDEN_CLIENT_DIRS:
        if (root / forbidden).exists():
            errors.append(f"{forbidden}/ is not part of an Agent Plugin package.")
    return errors


def _validate_mcp(path: Path, mcp_schema: str) -> list[str]:
    errors: list[str] = []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        return [f"mcp.json is not valid JSON: {error.msg}"]
    extra = set(data) - {"$schema", "mcpServers"}
    if extra:
        errors.append("mcp.json allows only $schema and mcpServers.")
    if data.get("$schema") != mcp_schema:
        errors.append(f"mcp.json $schema must be {mcp_schema}")
    if "servers" in data and "mcpServers" not in data:
        errors.append("mcp.json must use mcpServers, not servers.")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        errors.append("mcp.json must define mcpServers.")
        return errors
    for name, server in servers.items():
        if not isinstance(server, dict):
            errors.append(f"mcpServers.{name} must be an object.")
            continue
        transport = server.get("type")
        if transport == "stdio":
            command = str(server.get("command") or "")
            if not command:
                errors.append(f"mcpServers.{name}.command is required.")
            elif command.startswith("/") or " " in command or "${" in command:
                errors.append(
                    f"mcpServers.{name}.command must be a bare name or ./ path; "
                    "no shell, no absolute path, no ${{PLUGIN_ROOT}}."
                )
            elif "/" in command and not command.startswith("./"):
                errors.append(f"mcpServers.{name}.command plugin paths must start with ./")
            cwd = server.get("cwd")
            if cwd is not None and not CWD_RE.match(str(cwd)):
                errors.append(
                    f"mcpServers.{name}.cwd must be ./…, ${{PLUGIN_ROOT}}, or ${{PLUGIN_DATA}}."
                )
            env = server.get("env")
            if isinstance(env, dict) and ("PLUGIN_ROOT" in env or "PLUGIN_DATA" in env):
                errors.append(f"mcpServers.{name}.env must not set PLUGIN_ROOT or PLUGIN_DATA.")
        elif transport in {"streamable-http", "sse"}:
            if not server.get("url"):
                errors.append(f"mcpServers.{name}.url is required.")
        else:
            errors.append(f"mcpServers.{name}.type must be stdio, streamable-http, or sse.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_name", nargs="?")
    parser.add_argument(
        "--path",
        type=Path,
        help="Parent directory for the plugin. Default: <collection>/plugins.",
    )
    parser.add_argument("--skill", default="main")
    parser.add_argument("--description", default="")
    parser.add_argument("--with-mcp", action="store_true")
    parser.add_argument(
        "--extra",
        default="",
        help=(
            "Body for plugins/<name>/AGENTS.md after-load section. "
            "Default with --with-mcp: dest-rewrite note."
        ),
    )
    parser.add_argument(
        "--no-collection",
        action="store_true",
        help="Create under --path or cwd. Do not update AGENTS.md.",
    )
    parser.add_argument("--validate", type=Path)
    parser.add_argument(
        "--update",
        type=Path,
        help="Fill missing AGENTS.md / README.md on an existing plugin. Does not overwrite either if present unless --refresh-readme.",
    )
    parser.add_argument(
        "--refresh-readme",
        action="store_true",
        help="With --update, rewrite README.md (keeps AGENTS.md).",
    )
    parser.add_argument(
        "--from-bench",
        type=Path,
        help="With --update --refresh-readme, fill scores from a benchmarks-latest.json ratings object.",
    )
    args = parser.parse_args()
    plugin_schema, mcp_schema = live_schema_ids()
    try:
        if args.validate:
            errors = validate(args.validate, plugin_schema, mcp_schema)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(f"Valid Agent Plugin: {args.validate}")
            return 0
        if args.update:
            root = args.update.expanduser().resolve()
            data = json.loads((root / "plugin.json").read_text())
            name = str(data.get("name") or root.name)
            description = str(data.get("description") or "")
            if not (root / "AGENTS.md").is_file():
                write_plugin_agents(root, name, DEFAULT_MCP_INSTALL if (root / "mcp.json").is_file() else "")
            ratings = None
            source = "unmeasured"
            if args.from_bench:
                bench = json.loads(args.from_bench.expanduser().read_text())
                ratings = bench.get("ratings") if isinstance(bench, dict) else None
                source = str(args.from_bench)
            if args.refresh_readme or not (root / "README.md").is_file():
                write_plugin_readme(root, name, description, ratings=ratings, source=source)
            errors = validate(root, plugin_schema, mcp_schema)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(root)
            return 0
        if not args.plugin_name:
            parser.error("plugin_name is required unless --validate is used")
        name = plugin_name(args.plugin_name)
        skill = skill_name(args.skill)
        collection = None if args.no_collection else find_collection()
        if args.path is not None:
            parent = args.path.expanduser().resolve()
        elif collection is not None:
            parent = collection / "plugins"
        else:
            parent = Path.cwd()
        refuse_skills_catalog(parent)
        root = create(
            parent,
            name,
            skill,
            args.with_mcp,
            args.description,
            plugin_schema,
            mcp_schema,
        )
        extra = args.extra.strip() or (DEFAULT_MCP_INSTALL if args.with_mcp else "")
        write_plugin_agents(root, name, extra)
        write_plugin_readme(root, name, args.description)
        if collection is not None and root.parent == collection / "plugins":
            register_in_collection(collection, name, extra)
        errors = validate(root, plugin_schema, mcp_schema)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(root)
        return 0
    except (ValueError, FileExistsError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
