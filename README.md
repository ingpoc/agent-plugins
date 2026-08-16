# Agent Plugins

Portable [Agent Plugins](https://agent-plugins.org/) for any conformant client (Cursor, Codex, Copilot, VS Code, and others). One package. Not a Cursor plugin and a Codex plugin.

```text
plugins/agent-computer-use/
├── plugin.json
├── mcp.json
├── skills/macos-cua/
└── bin/
```

That is the whole contract: root `plugin.json`, skills under `skills/`, MCP in `mcp.json`. Clients load the directory. Installation UX stays with the client.

Agents: follow [`AGENTS.md`](AGENTS.md). Detect the current client, open its setup link, install `plugins/<name>/`.

## Add another plugin

Create `plugins/<name>/` with the same three files. Do not add `.cursor-plugin/` or `.codex-plugin/` manifests. Client extras, if ever needed, go in a reverse-domain folder the client documents (`com.example.client/`).
