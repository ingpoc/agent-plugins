# context-ledger — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.codex-plugin/`. Cursor stdio uses `.cursor-plugin/plugin.json` and `.cursor-plugin/mcp.json`. Claude Code uses `.claude-plugin/plugin.json` and `.claude-plugin/mcp.json`.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. Run `python bin/context_ledger.py setup` from this directory (network, or `--wheelhouse ABSOLUTE_DIR` for offline). The default data root is `~/.context-ledger/` (created mode `0700`), even when the client sets `PLUGIN_DATA`. Override it with `--data ABSOLUTE_DIR` or `CONTEXT_LEDGER_DATA` only when selecting another root. Repeat setup when `requirements.lock` changes.
2. Run `python bin/context_ledger.py init --scope SCOPE --actor ACTOR`. Optional `--ledger-dir ABSOLUTE_DIR` (default `~/.context-ledger/ledger`). Binding is owner-selected; tools cannot choose paths. To share Cursor and Codex, bind both to the same `--ledger-dir` and `ledger_id`. Sharing is the bind, not one MCP process.
3. MCP is loaded from the client manifest. Do not hand-add a server entry. All three start `bin/context-ledger-mcp serve`:
   - Portable `mcp.json` (Codex, Grok Build, other spec clients): `command` `sh`, `args` `["-c", <resolver>, "context-ledger-mcp", "${PLUGIN_ROOT}", "serve"]`, no `cwd` (omitted cwd is the plugin root). The resolver contains no `${`. It tries the root arg (skip when empty or still starting with `$`), then `$PLUGIN_ROOT`, `$CLAUDE_PLUGIN_ROOT`, `$CURSOR_PLUGIN_ROOT`, then `$PWD`, and execs `$root/bin/context-ledger-mcp`. If none match, stderr `PLUGIN_ROOT_UNRESOLVED`, exit 127.
   - Cursor: `.cursor-plugin/plugin.json` → `.cursor-plugin/mcp.json`. `command` `${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp`, `args` `["serve"]`, `cwd` `${CURSOR_PLUGIN_ROOT}`.
   - Claude Code: `.claude-plugin/plugin.json` → `.claude-plugin/mcp.json`. `command` `${CLAUDE_PLUGIN_ROOT}/bin/context-ledger-mcp`, `args` `["serve"]`.
   Cursor expands `${CURSOR_PLUGIN_ROOT}`. A root `./` command resolves against the workspace (ENOENT) and is not shipped. `doctor` exits nonzero with `CURSOR_DEST` when a dest has no healthy override and still has a relative command or a literal `${PLUGIN_*}` token. `python3 scripts/install_cursor_dest.py` repairs those pre-override dests to the absolute launcher (`cwd` `./`) and leaves a healthy override untouched. Do not put absolute interpreter paths in source `mcp.json`. Optional override: `CONTEXT_LEDGER_PYTHON`.
4. Client lists four tools: `find`, `get`, `record`, `append_event`. One server name, `context-ledger`. `serve` (and `--data DIR serve`) uses local stdio when `DATA/binding.json` exists (`--data`, else `CONTEXT_LEDGER_DATA`, else `~/.context-ledger`). With no binding and a non-empty `CONTEXT_LEDGER_MCP_TOKEN`, it bridges to `serve-http`. With neither, a macOS Keychain item (service `context-ledger.mcp`, account `bearer_token`; tool `security`, override `CONTEXT_LEDGER_SECURITY_BIN`) supplies that token to the bridge process only. Otherwise it exits nonzero with `LEDGER_UNBOUND` (the message names `bin/context_ledger.py init`, `CONTEXT_LEDGER_MCP_TOKEN`, and that keychain item) and does not create a data directory. No HTTP or SSE `mcp.json` entry. Only the ledger helper subagent uses the MCP; the parent only exchanges JSON with it. Owner CLI (`doctor`, `export`, `import`, `attest`, `purge`, `rebuild`, `migrate`, `resume-maintenance`, `scope-add`, `ensure-global-triggers`) is not an MCP tool.
   Remote (owner-run): `./bin/context-ledger-mcp serve-http [--port 8787] [--allowed-host HOST]` serves the same four tools over Streamable HTTP at `http://127.0.0.1:PORT/mcp` (loopback only). It refuses to start unless `CONTEXT_LEDGER_MCP_TOKEN` (32+ chars) is set and answers 401 to any request without `Authorization: Bearer <token>`. Tunnel and launchd: `skills/context-ledger/references/remote-mcp.md`.
5. Ensure always-on ledger triggers in `~/.codex/AGENTS.md` if that file exists:
   `python bin/context_ledger.py ensure-global-triggers`
   Idempotent. Inserts missing BEFORE lookup and AFTER save lines or updates existing marked lines. Then `workflow lint`. Do not rewrite other rules. Skip if the file is absent.
6. Before uninstall, export the owner ledger. The default `~/.context-ledger/` is independent of the client cache; a client-managed custom data root may not persist.

## Support (this pass)

Exercised: macOS arm64 with the local CPython that ran contract tests. Release-blocked until run: macOS x86_64 (cryptography 50.0.1 has no macOS x86_64 wheel), Ubuntu 22.04/24.04, Windows 11, CPython versions not used in this pass. Windows arm64 is deferred.

A hostile local user with write access to plugin, runtime, or binding can subvert the service. Local storage does not prevent disclosure to the model provider.

Published scores: this plugin's `README.md`. Leave `unmeasured` until a suite exists.

Behavior: `skills/context-ledger/SKILL.md`.
