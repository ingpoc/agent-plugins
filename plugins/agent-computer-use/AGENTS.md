# agent-computer-use — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.cursor-plugin/` or `.codex-plugin/` to this package.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. If the client is Cursor: run `skills/macos-cua/scripts/install_harness.py cursor-plugin` from this directory. Cursor resolves `./bin` against the workspace, not the plugin root. Dest `mcp.json` `command` must be the absolute dest launcher; `cwd` stays `./`. Do not copy that dest-absolute `command` back into source `mcp.json`.
2. Install [cua-driver](https://cua.ai/docs/cua-driver) if missing.
3. Stop only for the OS prompt that grants Accessibility / Screen Recording to **Cua Driver.app**.
4. Verify the client lists `agent-computer-use` and MCP tools `start_session` / `state` / `act` / `verify` / `end_session` are available.

Published scores: this plugin's `README.md`. Refresh only from a warm `python3 skills/macos-cua/scripts/run_benchmarks.py --repeat 5 --rate`. Do not paste a cold run or an older cache. Do not add `.cursor-plugin/` or `logo` on `plugin.json` for a Customize icon.

Behavior after install: `skills/macos-cua/SKILL.md` — **MCP fast path**
(`start_session` / compact `state` / batched `act` / `verify` / `end_session`).
AX first, then fallback on miss. WhatsApp send/attach: `$whatsapp`, not this package.
