# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit universal doctrine from the global `AGENTS.md`. This file owns only install routing for this collection.
- Do not create a second Cursor or Codex plugin. Packages here are [Agent Plugins](https://agent-plugins.org/specification) only.

## Scope

Portable plugins for any [compatible client](https://agent-plugins.org/compatible-clients). Install target is always `plugins/<name>/` (the directory that contains `plugin.json`). Never the repo root.

## Trigger Map

| Trigger | First hop |
| --- | --- |
| Install a plugin for the current client | This file → Client install. Then that client's setup URL. |
| Add or rename a plugin | Create `plugins/<name>/` → update the Plugins table here in the same pass |
| Change plugin behavior | That plugin's `SKILL.md` and `mcp.json` |

## Plugins

| Name | Path | Extra |
| --- | --- | --- |
| `agent-computer-use` | `plugins/agent-computer-use/` | macOS. Install [cua-driver](https://cua.ai/docs/cua-driver). Grant Accessibility and Screen Recording to **Cua Driver.app**, not the host IDE. |

## Client install

1. Detect the running client.
2. Open that client's setup URL below before improvising.
3. Install `plugins/<name>/` as an Agent Plugin (root `plugin.json` + `skills/` + `mcp.json`).
4. Do not add `.cursor-plugin/`, `.codex-plugin/`, or a dual marketplace catalog.
5. If the client needs a local index, point it at `plugins/<name>/` only.

| Client | Setup | How to load this repo |
| --- | --- | --- |
| Cursor | [cursor.com/docs/plugins](https://cursor.com/docs/plugins) | `mkdir -p ~/.cursor/plugins/local && ln -sfn "$REPO/plugins/<name>" ~/.cursor/plugins/local/<name>` then **Developer: Reload Window**. CLI: `cursor-agent --plugin-dir "$REPO/plugins/<name>"`. |
| ChatGPT & Codex | [developers.openai.com/plugins](https://developers.openai.com/plugins) | Load the plugin directory. If the client requires a catalog, add an entry whose `source.path` is `./plugins/<name>` (or a git-subdir to that folder). Then `codex plugin add <name>@<marketplace>`. |
| VS Code | [Agent plugins](https://code.visualstudio.com/docs/agent-customization/agent-plugins) | Set `chat.plugins.enabled` true. Register the folder: `"chat.pluginLocations": { "<abs>/plugins/<name>": true }`. Or **Chat: Install Plugin From Source** only if `plugin.json` is at the clone root. |
| GitHub Copilot | [Finding and installing](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/plugins-finding-installing) | `copilot plugin install` from a registered marketplace, or a local marketplace whose plugin path is `plugins/<name>/`. |
| Kiro | [Powers](https://kiro.dev/docs/powers/) | Install the plugin directory (Kiro calls Agent Plugins "powers"). Prefer a GitHub/local path to `plugins/<name>/`. |
| Hermes Agent | [Portable packages](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#portable-agent-plugins-v1-packages) | `hermes plugins install <path-or-owner/repo> --no-enable` then `hermes plugins enable <name>`. Path must resolve to the `plugin.json` directory. |
| OpenClaw | [Plugin bundles](https://docs.openclaw.ai/plugins/bundles) | `openclaw plugins install "$REPO/plugins/<name>"` then `openclaw gateway restart`. |
| Grok Bot | [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations) | **Settings → Plugins**. Install the packaged plugin / folder the client accepts. |
| NanoClaw | [Templates](https://github.com/nanocoai/nanoclaw/blob/main/docs/templates.md) | Stamp `plugins/<name>/` as the plugin root (`plugin.json` in that folder). |
| Other listed client | [Compatible clients](https://agent-plugins.org/compatible-clients) | Use that card's setup link. Same install target: `plugins/<name>/`. |

Unknown client: fetch [compatible-clients](https://agent-plugins.org/compatible-clients), open its setup URL, install `plugins/<name>/`. Stop if the client cannot load a directory that has `plugin.json`.

## Repo Rules

- One portable package per `plugins/<name>/`. No client-native manifests.
- Adding a plugin without updating the Plugins table is incomplete.
- Do not infer Vahan, fees, or personal case data. This repo is public.
