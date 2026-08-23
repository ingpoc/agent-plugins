# Agent Plugins

Collection of portable [Agent Plugins](https://agent-plugins.org/). The install unit is `plugins/<name>/` (has `plugin.json`). This repository root is a marketplace, not a plugin. Thin catalogs for Codex, Cursor, and Grok all point to the same portable packages.

A user asks their agent to install a named plugin from this GitHub URL. The agent reads root [`AGENTS.md`](AGENTS.md) for routing, then [`plugins/<name>/AGENTS.md`](plugins/agent-computer-use/AGENTS.md) for that plugin's install, matches the running client on [compatible-clients](https://agent-plugins.org/compatible-clients), and loads `plugins/<name>/`. Do the work. Do not hand the user a recipe.

```text
plugins/agent-computer-use/
├── AGENTS.md
├── README.md
├── plugin.json
├── mcp.json
├── skills/macos-cua/
└── bin/
plugins/comet-control/
├── AGENTS.md
├── README.md
├── plugin.json
├── skills/comet-control/
└── plugin/comet_control/
```

## Add another plugin

Use `$agent-plugin-creator`. It writes `plugins/<name>/` (including that plugin's `AGENTS.md`) and a row in the root Plugins table. Do not add `.cursor-plugin/` or `.codex-plugin/` manifests.
