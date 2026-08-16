# Agent Plugins

Collection of portable [Agent Plugins](https://agent-plugins.org/). The install unit is `plugins/<name>/` (has `plugin.json`). This repository root is not a plugin.

Agents: read [`AGENTS.md`](AGENTS.md). Find the current client on [compatible-clients](https://agent-plugins.org/compatible-clients), open that client's setup link, and install `plugins/<name>/`. Do the work. Do not hand the user a recipe.

```text
plugins/agent-computer-use/
├── plugin.json
├── mcp.json
├── assets/logo.svg
├── .cursor-plugin/plugin.json   # Cursor overlay only; logo path
├── skills/macos-cua/
└── bin/
plugins/comet-control/
├── plugin.json
├── assets/logo.svg
├── .cursor-plugin/plugin.json
├── skills/comet-control/
└── plugin/comet_control/   # extension + broker
```

## Add another plugin

Use `$agent-plugin-creator`. It fetches the live [spec](https://github.com/agentplugins/agent-plugins-spec) and [plugin-authors](https://agent-plugins.org/plugin-authors) docs, writes `plugins/<name>/` (including committed `assets/logo.svg` and Cursor overlay), and updates the `AGENTS.md` Plugins table. Do not put `logo` on portable `plugin.json`.
