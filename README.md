# Agent Plugins

Collection of portable [Agent Plugins](https://agent-plugins.org/). The install unit is `plugins/<name>/` (has `plugin.json`). This repository root is a marketplace, not a plugin. Thin catalogs for Codex, Cursor, and Grok all point to the same portable packages.

Cursor Origin is the forge of record: [gurusharan/agent-plugins](https://cursor.com/codebase/gurusharan/agent-plugins). GitHub is the outbound mirror of `main`. Changes land through an Origin pull request; `scripts/check.py` is the required check, and direct pushes to `main` are rejected. See [`docs/AGENTS.md`](docs/AGENTS.md).

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
plugins/context-ledger/
├── AGENTS.md
├── README.md
├── plugin.json
├── mcp.json
├── skills/context-ledger/
├── context_ledger/
└── bin/
```

Catalog: `.cursor-plugin/marketplace.json` (and the Codex/Grok twins) lists all three. Cursor clients that already added this marketplace only show previously **Added** plugins until the marketplace is removed and re-added (or updated) so the catalog re-indexes — a new row in `marketplace.json` alone is not enough.

## Add another plugin

Use `workflow summary agent-plugin-routing-sync` then `python3 scripts/create_agent_plugin.py <name> --skill <skill> --description "…"` (optional `--with-mcp`). It writes `plugins/<name>/` (including that plugin's `AGENTS.md`) and a row in the root Plugins table. Do not add `.codex-plugin/` manifests. A stdio `./` command ships `.cursor-plugin/plugin.json` and `.cursor-plugin/mcp.json`. After the catalog changes, refresh the Cursor marketplace (remove/re-add or update) before claiming the new plugin appears under Ingpoc with **Add**.
