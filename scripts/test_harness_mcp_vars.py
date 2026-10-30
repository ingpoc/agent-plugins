#!/usr/bin/env python3
"""Each harness manifest's MCP config resolves with that harness's own variables.

Runs anywhere (no macOS, no client installed). For every plugin and every
harness manifest it ships, load the MCP config the manifest points at, then:
- every ${...} placeholder must be one that harness expands (or a plain env name
  where the harness supports it), never another harness's root variable;
- expanding the harness's root variables against the package path must leave no
  plugin-root placeholder behind, and a launcher command must exist in the package;
- no command or cwd may be cwd-relative.

Sources for the variable sets:
- Cursor: cursor.com/docs/reference/plugins#mcp-servers (expands ${CURSOR_PLUGIN_ROOT}
  and ${CLAUDE_PLUGIN_ROOT}; not ${PLUGIN_ROOT}/${PLUGIN_DATA}); env via ${env:VAR}.
- Grok Build: ~/.grok/docs/user-guide/09-plugins.md and grok-build docs
  "GROK_PLUGIN_ROOT and GROK_PLUGIN_DATA" (substituted in MCP config strings, with
  ${CLAUDE_PLUGIN_ROOT}/${CLAUDE_PLUGIN_DATA} aliases); env values as ${VAR}.
- Claude Code: ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA}.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = sorted(p.parent for p in (ROOT / "plugins").glob("*/plugin.json"))
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
ROOT_VAR = re.compile(r"^(?:[A-Z]+_)?PLUGIN_(?:ROOT|DATA)$")

HARNESSES = {
    # manifest dir: (root vars it expands, accepts ${env:VAR}, accepts plain ${VAR} env names)
    ".cursor-plugin": ({"CURSOR_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"}, True, False),
    ".grok-plugin": ({"GROK_PLUGIN_ROOT", "GROK_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"}, False, True),
    ".claude-plugin": ({"CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"}, False, True),
}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


# Which manifest each harness actually reads, in its own search order, and the
# convention MCP file it falls back to when that manifest names no mcpServers.
SEARCH = {
    ".cursor-plugin": ([".cursor-plugin/plugin.json", "plugin.json"], "mcp.json"),
    # Grok: plugin.json, then .grok-plugin/, then .claude-plugin/; convention .mcp.json.
    ".grok-plugin": (["plugin.json", ".grok-plugin/plugin.json", ".claude-plugin/plugin.json"], ".mcp.json"),
    ".claude-plugin": ([".claude-plugin/plugin.json"], ".mcp.json"),
}


def claims(plugin: Path, harness: str) -> bool:
    return (plugin / harness / "plugin.json").is_file()


def mcp_config(plugin: Path, harness: str) -> dict | None:
    """The MCP config this harness really loads for the plugin (None if none)."""
    order, convention = SEARCH[harness]
    manifest = next((plugin / rel for rel in order if (plugin / rel).is_file()), None)
    ref = json.loads(manifest.read_text()).get("mcpServers") if manifest else None
    if isinstance(ref, dict):
        return ref if "mcpServers" in ref else {"mcpServers": ref}
    path = (plugin / (ref or convention)).resolve()
    assert path.is_relative_to(plugin.resolve()), f"{plugin.name}: mcpServers escapes the plugin"
    if not path.is_file():
        # A manifest that names an MCP file must ship it; only the convention file is optional.
        assert not ref, f"{plugin.name}/{harness}: mcpServers names missing {ref}"
        return None
    return json.loads(path.read_text())


def violations(plugin: Path, harness: str, config: dict) -> list[str]:
    allowed_roots, env_colon, env_plain = HARNESSES[harness]
    errors = []
    for name, server in config.get("mcpServers", {}).items():
        where = f"{plugin.name}/{harness} {name}"
        for text in strings(server):
            for token in PLACEHOLDER.findall(text):
                if token.startswith("env:"):
                    if not env_colon:
                        errors.append(f"{where}: ${{{token}}} is Cursor-only syntax")
                elif ROOT_VAR.match(token):
                    if token not in allowed_roots:
                        errors.append(f"{where}: ${{{token}}} is not expanded by this harness")
                elif not env_plain:
                    errors.append(f"{where}: ${{{token}}} is not a variable this harness expands")
        if "url" in server:
            continue
        command = server.get("command", "")
        cwd = server.get("cwd")
        anchored = ("/", "${")
        if ("/" in command and not command.startswith(anchored)) or (cwd is not None and not str(cwd).startswith(anchored)):
            errors.append(f"{where}: cwd-relative command or cwd")
        expanded = command
        for var in allowed_roots:
            expanded = expanded.replace("${%s}" % var, str(plugin))
        if "/" in expanded:
            if PLACEHOLDER.search(expanded):
                errors.append(f"{where}: command keeps an unexpanded placeholder: {command}")
            elif not Path(expanded).is_file():
                errors.append(f"{where}: command does not resolve to a packaged file: {command}")
        else:
            # A bare interpreter (sh resolver): the plugin root must reach it via args.
            args = " ".join(server.get("args", []))
            if not any("${%s}" % var in args for var in allowed_roots):
                errors.append(f"{where}: resolver gets no root variable this harness expands")
    return errors


class HarnessMcpVariables(unittest.TestCase):
    def test_every_harness_manifest_uses_its_own_variables(self):
        checked = 0
        errors: list[str] = []
        for plugin in PLUGINS:
            for harness in HARNESSES:
                if not claims(plugin, harness):
                    continue
                config = mcp_config(plugin, harness)
                if config is None:
                    continue
                checked += 1
                errors.extend(violations(plugin, harness, config))
        self.assertGreater(checked, 0)
        self.assertEqual(errors, [])

    def test_cursor_variables_in_a_grok_config_fail(self):
        plugin = ROOT / "plugins" / "agent-computer-use"
        bad = {"mcpServers": {"x": {"type": "stdio", "command": "${CURSOR_PLUGIN_ROOT}/bin/agent-computer-use-mcp"}}}
        self.assertTrue(violations(plugin, ".grok-plugin", bad))
        env = {"mcpServers": {"x": {"command": "${GROK_PLUGIN_ROOT}/bin/agent-computer-use-mcp",
                                    "env": {"T": "${env:TOKEN}"}}}}
        self.assertTrue(violations(plugin, ".grok-plugin", env))

    def test_grok_variables_in_a_cursor_config_fail(self):
        plugin = ROOT / "plugins" / "agent-computer-use"
        bad = {"mcpServers": {"x": {"command": "${GROK_PLUGIN_ROOT}/bin/agent-computer-use-mcp"}}}
        self.assertTrue(violations(plugin, ".cursor-plugin", bad))

    def test_relative_command_fails(self):
        plugin = ROOT / "plugins" / "agent-computer-use"
        bad = {"mcpServers": {"x": {"command": "./bin/agent-computer-use-mcp", "cwd": "./"}}}
        self.assertTrue(violations(plugin, ".grok-plugin", bad))
        sub = {"mcpServers": {"x": {"command": "${GROK_PLUGIN_ROOT}/bin/agent-computer-use-mcp", "cwd": "subdir"}}}
        self.assertTrue(violations(plugin, ".grok-plugin", sub))
        bare = {"mcpServers": {"x": {"command": "bin/agent-computer-use-mcp"}}}
        self.assertTrue(violations(plugin, ".grok-plugin", bare))

    def test_grok_loads_mcp_for_plugins_that_ship_a_server(self):
        # agent-computer-use is the harness-claimed MCP plugin; Grok must see its server.
        plugin = ROOT / "plugins" / "agent-computer-use"
        config = mcp_config(plugin, ".grok-plugin")
        self.assertIsNotNone(config, "Grok would load no MCP server")
        self.assertIn("agent-computer-use", config["mcpServers"])


if __name__ == "__main__":
    unittest.main()
