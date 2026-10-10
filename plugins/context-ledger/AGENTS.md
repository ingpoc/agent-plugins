# context-ledger — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.codex-plugin/`. Cursor stdio uses `.cursor-plugin/plugin.json` and `.cursor-plugin/mcp.json`.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. Run `python bin/context_ledger.py setup` from this directory (network, or `--wheelhouse ABSOLUTE_DIR` for offline). The default data root is `~/.context-ledger/` (created mode `0700`), even when the client sets `PLUGIN_DATA`. Override it with `--data ABSOLUTE_DIR` or `CONTEXT_LEDGER_DATA` only when selecting another root. Repeat setup when `requirements.lock` changes.
2. Run `python bin/context_ledger.py init --scope SCOPE --actor ACTOR`. Optional `--ledger-dir ABSOLUTE_DIR` (default `~/.context-ledger/ledger`). Binding is owner-selected; tools cannot choose paths. To share Cursor and Codex, bind both to the same `--ledger-dir` and `ledger_id`. Sharing is the bind, not one MCP process.
3. Portable root `mcp.json` stays `./bin/context-ledger-mcp` and `cwd` `./` for Codex and Grok (`#!/bin/sh` launcher). Cursor loads `.cursor-plugin/plugin.json` → `.cursor-plugin/mcp.json`: `command` `${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp`, `args` `["serve"]`, `cwd` `${CURSOR_PLUGIN_ROOT}`. Cursor expands `${CURSOR_PLUGIN_ROOT}` and resolves a root `./` command against the workspace (ENOENT). It does not expand `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`. `doctor` still exits nonzero with `CURSOR_DEST` when a dest has no healthy override and still has a relative command or a literal `${PLUGIN_*}` token. `python3 scripts/install_cursor_dest.py` repairs those pre-override dests to the absolute launcher (`cwd` `./`) and leaves a healthy override untouched. Do not put absolute interpreter paths in source `mcp.json`. Optional override: `CONTEXT_LEDGER_PYTHON`.
4. Client lists four tools: `find`, `get`, `record`, `append_event`. One server name, `context-ledger`. `serve` (and `--data DIR serve`) uses local stdio when `DATA/binding.json` exists (`--data`, else `CONTEXT_LEDGER_DATA`, else `~/.context-ledger`). With no binding and a non-empty `CONTEXT_LEDGER_MCP_TOKEN`, it bridges to `serve-http`. With neither, it exits nonzero with `LEDGER_UNBOUND` and does not create a data directory. Cloud agents do not add a second url-type entry. Only the ledger helper subagent uses the MCP; the parent only exchanges JSON with it. Owner CLI (`doctor`, `export`, `import`, `attest`, `purge`, `rebuild`, `migrate`, `resume-maintenance`, `scope-add`, `ensure-global-triggers`) is not an MCP tool.
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
