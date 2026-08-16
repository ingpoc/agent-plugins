# Agent Plugins

Collection of portable [Agent Plugins](https://agent-plugins.org/). The install unit is `plugins/<name>/` (has `plugin.json`). This repository root is not a plugin.

Agents: read [`AGENTS.md`](AGENTS.md). Find the current client on [compatible-clients](https://agent-plugins.org/compatible-clients), open that client's setup link, and install `plugins/<name>/`. Do the work. Do not hand the user a recipe.

```text
plugins/agent-computer-use/
├── plugin.json
├── mcp.json
├── skills/macos-cua/
└── bin/
```

## Add another plugin

Use `$agent-plugin-creator`. It fetches the live [spec](https://github.com/agentplugins/agent-plugins-spec) and [plugin-authors](https://agent-plugins.org/plugin-authors) docs, writes `plugins/<name>/`, and updates the `AGENTS.md` Plugins table. Do not add `.cursor-plugin/` or `.codex-plugin/` manifests.
