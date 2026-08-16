# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit universal doctrine from the global `AGENTS.md`. This file owns only this repo's plugin inventory and the install trigger.
- Client setup is not owned here. [Compatible clients](https://agent-plugins.org/compatible-clients) is.

## Scope

The agent installs. The user does not clone, symlink, edit settings, or follow a recipe.

Install target is `plugins/<name>/` (the directory with `plugin.json`). Never the repo root. These are [Agent Plugins](https://agent-plugins.org/specification) only — no Cursor or Codex fork.

## Trigger Map

| Trigger | First hop |
| --- | --- |
| Install a plugin | Agent install. Do not dump steps. |
| Create or add a plugin | `$agent-plugin-creator`. Fetch [spec](https://github.com/agentplugins/agent-plugins-spec), [plugin-authors](https://agent-plugins.org/plugin-authors), and [manifest](https://agent-plugins.org/plugin-authors/manifest) first. Land in `plugins/<name>/` and update this table. |
| Add or rename a plugin | `plugins/<name>/` → update the Plugins table here in the same pass |
| Change plugin behavior | That plugin's `SKILL.md` and `mcp.json` |

## Plugins

| Name | Path | Extra the agent must also do |
| --- | --- | --- |
| `agent-computer-use` | `plugins/agent-computer-use/` | After a Cursor local copy, run `skills/macos-cua/scripts/install_harness.py cursor-plugin` (Cursor resolves `./bin` against the workspace, not the plugin root). Install [cua-driver](https://cua.ai/docs/cua-driver) if missing. Stop only for the OS prompt that grants Accessibility / Screen Recording to **Cua Driver.app**. |
| `comet-control` | `plugins/comet-control/` | Requires Comet.app. First load: unpack deploy/extension in the logged-in Comet profile at chrome://extensions (use agent-computer-use for that chrome:// page). Never load it in Google Chrome. |

## Agent install

1. Detect the running client.
2. Open [compatible-clients](https://agent-plugins.org/compatible-clients). Match the client. Take its setup-instructions link. If the page has no cards (JS shell), read the owner [`lib/compatible-clients.ts`](https://raw.githubusercontent.com/agentplugins/agent-plugins-site/main/lib/compatible-clients.ts) and use that row's `instructionsUrl`.
3. Fetch that setup page in full and follow it to load `plugins/<name>/`.
4. Apply that plugin's Extra row above.
5. Verify the client lists the plugin. Fix from the same setup page if it does not.
6. Stop only for an OS or store consent the agent cannot complete.

Do not add `.cursor-plugin/`, `.codex-plugin/`, or a dual marketplace catalog. If the client needs an index, point it at `plugins/<name>/` only. If the client is missing or has no setup link, say so and stop.

## Repo Rules

- One portable package per `plugins/<name>/`. Shape comes from the live spec, not this file.
- Adding a plugin without updating the Plugins table is incomplete.
- This repo is public. No personal case data.
