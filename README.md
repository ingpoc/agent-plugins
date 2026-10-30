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

## Install

Three plugins, all macOS only: `agent-computer-use`, `comet-control`, `context-ledger`. Swap the name in the commands below.

| Client | Install | Update |
| --- | --- | --- |
| Codex | `codex plugin marketplace add ingpoc/agent-plugins`, then `codex plugin add agent-computer-use@ingpoc-agent-plugins`. Start a new thread. | `codex plugin marketplace upgrade ingpoc-agent-plugins`, then run `codex plugin add` again and start a new thread. |
| Cursor | Teams and Enterprise: an admin imports `https://github.com/ingpoc/agent-plugins` under Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo, then members install from **Customize**. Without a team plan, copy `plugins/<name>/` into `~/.cursor/plugins/local/<name>/` and reload the window. The plugins are not yet listed in the public Cursor Marketplace. | Team marketplace: turn on Auto Refresh (needs the Cursor GitHub App) or click **Refresh**, then start a new agent. Local copy: replace the folder and reload the window. |
| Grok Build | `grok plugin marketplace add ingpoc/agent-plugins`, then `grok plugin install agent-computer-use`. | `grok plugin marketplace update`, then `grok plugin install agent-computer-use` again. |
| Agent Plugins Installer | Ask the Installer bot to install the plugin. It reads the site `manifest.json`, downloads the notarized DMG from the GitHub release and checks its SHA-256. | Ask it again. It compares the installed version with the manifest. |

`agent-computer-use` also needs the **Agent Computer Use** app: download the notarized DMG from the [latest release](https://github.com/ingpoc/agent-plugins/releases/latest), drag it to Applications and open it. On first run, macOS asks for:

- **Accessibility**, to read and act on app windows.
- **Screen Recording**. Quit and reopen the app after granting it.
- **Automation**, once per app the agent drives through Apple Events.
- **Microphone**, only if you use the Samantha voice runtime (GPT-Live-1 or Grok Voice 3.0).

`comet-control` also needs [Perplexity Comet](https://www.perplexity.ai/comet) and its unpacked extension. It works only inside the leased Comet window, with its own in-page cursor; it never moves the Mac's mouse or keyboard. See [`plugins/comet-control/README.md`](plugins/comet-control/README.md).

## Add another plugin

Use `workflow summary agent-plugin-routing-sync` then `python3 scripts/create_agent_plugin.py <name> --skill <skill> --description "…"` (optional `--with-mcp`). It writes `plugins/<name>/` (including that plugin's `AGENTS.md`) and a row in the root Plugins table. Do not add `.codex-plugin/` manifests. A stdio `./` command ships `.cursor-plugin/plugin.json` and `.cursor-plugin/mcp.json`. After the catalog changes, refresh the Cursor marketplace (remove/re-add or update) before claiming the new plugin appears under Ingpoc with **Add**.
