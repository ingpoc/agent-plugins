# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit universal doctrine from the global `AGENTS.md`. This file owns only this repo's plugin inventory and the install trigger.
- Client setup is not owned here. [Compatible clients](https://agent-plugins.org/compatible-clients) is.

## Scope

The agent installs. The user does not clone, symlink, edit settings, or follow a recipe.

Install target is `plugins/<name>/` (the directory with `plugin.json`). Never the repo root. Portable contract is [Agent Plugins](https://agent-plugins.org/specification). Cursor/Codex Extra may copy `.cursor-plugin/` and `assets/` so the client can resolve a logo; do not put `logo` on portable `plugin.json`.

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
| `agent-computer-use` | `plugins/agent-computer-use/` | Copy the whole package including `assets/logo.svg` and `.cursor-plugin/`. After a Cursor local copy, run `skills/macos-cua/scripts/install_harness.py cursor-plugin` (Cursor resolves `./bin` against the workspace, not the plugin root). Install [cua-driver](https://cua.ai/docs/cua-driver) if missing. Stop only for the OS prompt that grants Accessibility / Screen Recording to **Cua Driver.app**. Visible Customize icon: GitHub/team import or [Marketplace publish](https://cursor.com/marketplace/publish) so catalog `logoUrl` comes from `.cursor-plugin/plugin.json` `logo`. Local-only list stays a cube. Codex: load `plugins/agent-computer-use/` per <https://developers.openai.com/plugins> with the same assets. Do not put `logo` on portable `plugin.json`. |
| `comet-control` | `plugins/comet-control/` | Copy the whole package including `assets/logo.svg` and `.cursor-plugin/`. Requires Comet.app. First load: unpack `plugin/comet_control/extension` in the logged-in Comet profile at chrome://extensions (use agent-computer-use for that chrome:// page). Never load it in Google Chrome. Cursor visible icon: import or Marketplace publish as above. |

## Agent install

1. Detect the running client.
2. Open [compatible-clients](https://agent-plugins.org/compatible-clients). Match the client. Take its setup-instructions link. If the page has no cards (JS shell), read the owner [`lib/compatible-clients.ts`](https://raw.githubusercontent.com/agentplugins/agent-plugins-site/main/lib/compatible-clients.ts) and use that row's `instructionsUrl`.
3. Fetch that setup page in full and follow it to load `plugins/<name>/` including `assets/` and `.cursor-plugin/`.
   Cursor setup owner: [cursor.com/docs/plugins](https://cursor.com/docs/plugins) (`instructionsUrl` in compatible-clients). Local load is `~/.cursor/plugins/local/<name>` then Extra, then reload. For a visible logo, also import the GitHub repo or publish to Marketplace so catalog `logoUrl` is set. Codex: <https://developers.openai.com/plugins>.
4. Apply that plugin's Extra row above.
5. Verify the client lists the plugin and, after import/publish, shows the logo (not the gray cube). Fix from the same setup page if it does not.
6. Stop only for an OS or store consent the agent cannot complete.

Do not put `logo` on portable `plugin.json`. Do not use `.cursor-plugin/` or a dual marketplace catalog as the portable unit. If the client is missing or has no setup link, say so and stop.

## Repo Rules

- One portable package per `plugins/<name>/`. Shape comes from the live spec, not this file.
- Adding a plugin without updating the Plugins table is incomplete.
- This repo is public. No personal case data.
