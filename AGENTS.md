# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit universal doctrine from the global `AGENTS.md`. This file owns routing, triggers, and durable repo facts. It does not own per-plugin install.
- Client setup pages are not owned here. [Compatible clients](https://agent-plugins.org/compatible-clients) is.

## Durable facts

This repository is a **collection of portable [Agent Plugins](https://agent-plugins.org/specification)**.

| In the repo | Not in the repo |
| --- | --- |
| `plugins/<name>/` with closed `plugin.json` | Cursor-specific plugin packages (`.cursor-plugin/plugin.json`) |
| `skills/`, optional `mcp.json`, optional `bin/`, runtime files that package needs | Codex plugin packages (`.codex-plugin/`) |
| Thin Codex, Cursor, and Grok marketplace catalogs that point at the same portable packages | Duplicate client-specific copies of plugin contents |

The collection root is not a plugin. The install unit is always `plugins/<name>/`.

The user gives their agent the GitHub URL (`https://github.com/ingpoc/agent-plugins`) and a plugin name. The agent does the install. The user does not clone, symlink, edit settings, or follow a recipe.

## Trigger Map

| Trigger | First hop |
| --- | --- |
| Install a plugin | That plugin's `AGENTS.md`. Do not dump steps here. |
| Create or add a plugin | `$agent-plugin-creator`. Fetch [spec](https://github.com/agentplugins/agent-plugins-spec), [plugin-authors](https://agent-plugins.org/plugin-authors), and [manifest](https://agent-plugins.org/plugin-authors/manifest) first. Land in `plugins/<name>/` with a plugin `AGENTS.md`. Add a row to the Plugins table here. |
| Add or rename a plugin | `plugins/<name>/` → update the Plugins table here in the same pass |
| Change plugin behavior | That plugin's `SKILL.md` and `mcp.json` |
| Change how this plugin installs | That plugin's `AGENTS.md` |
| Change published benchmark scores | That plugin's `README.md` |

## Plugins

| Name | Path | Install |
| --- | --- | --- |
| `agent-computer-use` | `plugins/agent-computer-use/` | `plugins/agent-computer-use/AGENTS.md` |
| `comet-control` | `plugins/comet-control/` | `plugins/comet-control/AGENTS.md` |

## Agent install (every client)

1. Detect the running client.
2. Open [compatible-clients](https://agent-plugins.org/compatible-clients). Match the client. Take its setup-instructions link. If the page has no cards (JS shell), read the owner [`lib/compatible-clients.ts`](https://raw.githubusercontent.com/agentplugins/agent-plugins-site/main/lib/compatible-clients.ts) and use that row's `instructionsUrl`.
3. Prefer the repository catalog for Codex (`.agents/plugins/marketplace.json`), Cursor (`.cursor-plugin/marketplace.json`), or Grok Build (`.grok-plugin/marketplace.json`). All catalogs load **`plugins/<name>/` only** (the directory with `plugin.json`); the collection root is never a plugin.
4. Read and apply `plugins/<name>/AGENTS.md`.
5. Verify the client lists the plugin. Fix from the same setup page if it does not.
6. Stop only for an OS or store consent the agent cannot complete.

If the client is missing or has no setup link, say so and stop.

## Repo Rules

- One portable package per `plugins/<name>/`. Shape comes from the live spec, not this file.
- Adding a plugin without a Plugins row, `plugins/<name>/AGENTS.md`, and `plugins/<name>/README.md` is incomplete.
- This repo is public. No personal case data.
