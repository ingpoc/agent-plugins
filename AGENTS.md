# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit universal doctrine from the global `AGENTS.md`. This file owns only agent-performed install routing.
- Packages here are [Agent Plugins](https://agent-plugins.org/specification) only. Do not create a Cursor or Codex fork.

## Scope

The agent installs the plugin. The user does not clone, symlink, edit settings, or follow a recipe.

Install target is always `plugins/<name>/` (the directory with `plugin.json`). Never the repo root.

## Trigger Map

| Trigger | First hop |
| --- | --- |
| User wants this plugin / these plugins | Agent install below. Do not dump install steps. |
| Add or rename a plugin | `plugins/<name>/` → update the Plugins table here in the same pass |
| Change plugin behavior | That plugin's `SKILL.md` and `mcp.json` |

## Plugins

| Name | Path | Extra the agent must also do |
| --- | --- | --- |
| `agent-computer-use` | `plugins/agent-computer-use/` | Install [cua-driver](https://cua.ai/docs/cua-driver) if missing. Stop only for the OS prompt that grants Accessibility / Screen Recording to **Cua Driver.app**. |

## Agent install

Do this yourself. Do not ask the user to install.

1. Detect the running client (Cursor, Codex/ChatGPT, VS Code, Copilot, Kiro, Hermes, OpenClaw, Grok Bot, NanoClaw, or other).
2. Fetch the live client list: [compatible-clients](https://agent-plugins.org/compatible-clients) or its owner [`lib/compatible-clients.ts`](https://raw.githubusercontent.com/agentplugins/agent-plugins-site/main/lib/compatible-clients.ts).
3. Read that client's `instructionsUrl` in full. That page is the install contract. Cached URLs below are hints only.
4. Follow that page to load `plugins/<name>/` as an Agent Plugin (`plugin.json` + `skills/` + `mcp.json`). Prefer the client's local / from-source / CLI path so the user never leaves the session.
5. Verify the client lists the plugin (skill and MCP). Fix from the same setup page if it does not.
6. Stop and name the exact OS/consent control only when the client or OS requires a human (permission dialog, store review). Never stop for mkdir, symlink, settings JSON, or CLI install.

Do not add `.cursor-plugin/`, `.codex-plugin/`, or a dual marketplace catalog. If the client needs an index, point it at `plugins/<name>/` only.

| Client | `instructionsUrl` (refresh from the live list) |
| --- | --- |
| Cursor | <https://cursor.com/docs/plugins> |
| ChatGPT & Codex | <https://developers.openai.com/plugins> |
| VS Code | <https://code.visualstudio.com/docs/agent-customization/agent-plugins> |
| GitHub Copilot | <https://docs.github.com/en/copilot/concepts/agents/about-plugins> |
| Kiro | <https://kiro.dev/docs/powers/> |
| Hermes Agent | <https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#portable-agent-plugins-v1-packages> |
| OpenClaw | <https://docs.openclaw.ai/plugins/bundles> |
| Grok Bot | <https://docs.x.ai/grok-bot/skills-routines-and-automations> |
| NanoClaw | <https://github.com/nanocoai/nanoclaw/blob/main/docs/templates.md> |

Unknown client: same loop from the live list. If there is no `instructionsUrl` or the client cannot load a `plugin.json` directory, say so and stop.

## Repo Rules

- One portable package per `plugins/<name>/`. No client-native manifests.
- Adding a plugin without updating the Plugins table is incomplete.
- This repo is public. No personal case data.
