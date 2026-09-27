# context-ledger — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.cursor-plugin/` or `.codex-plugin/` to this package.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. Run `python bin/context_ledger.py setup` from this directory (network, or `--wheelhouse ABSOLUTE_DIR` for offline). The default data root is `~/.context-ledger/` (created mode `0700`), even when the client sets `PLUGIN_DATA`. Override it with `--data ABSOLUTE_DIR` or `CONTEXT_LEDGER_DATA` only when selecting another root. Repeat setup when `requirements.lock` changes.
2. Run `python bin/context_ledger.py init --scope SCOPE --actor ACTOR`. Optional `--ledger-dir ABSOLUTE_DIR` (default `~/.context-ledger/ledger`). Binding is owner-selected; tools cannot choose paths. To share Cursor and Codex, bind both to the same `--ledger-dir` and `ledger_id`. Sharing is the bind, not one MCP process.
3. Source `mcp.json` uses `command` `./bin/context-ledger-mcp` (`#!/bin/sh` launcher; Agent Plugins relative path). Codex / Agents keep that relative command. If Cursor: `python3 scripts/install_cursor_dest.py` from this directory (or from the installed Cursor copy). Dest `mcp.json` `command` must be the absolute dest launcher; `cwd` stays `./`. Do not put absolute interpreter paths in source `mcp.json`. Optional override: `CONTEXT_LEDGER_PYTHON`.
4. Client lists four tools: `find`, `get`, `record`, `append_event`. Owner CLI (`doctor`, `export`, `import`, `attest`, `purge`, `rebuild`, `migrate`, `resume-maintenance`, `scope-add`, `ensure-global-triggers`) is not an MCP tool.
5. Ensure always-on ledger triggers in `~/.codex/AGENTS.md` if that file exists:
   `python bin/context_ledger.py ensure-global-triggers`
   Idempotent. Inserts the BEFORE lookup and AFTER save lines if missing. Then `workflow lint`. Do not rewrite other rules. Skip if the file is absent.
6. Before uninstall, export the owner ledger. The default `~/.context-ledger/` is independent of the client cache; a client-managed custom data root may not persist.

## Support (this pass)

Exercised: macOS arm64 with the local CPython that ran contract tests. Release-blocked until run: macOS x86_64 (cryptography 50.0.1 has no macOS x86_64 wheel), Ubuntu 22.04/24.04, Windows 11, CPython versions not used in this pass. Windows arm64 is deferred.

A hostile local user with write access to plugin, runtime, or binding can subvert the service. Local storage does not prevent disclosure to the model provider.

Published scores: this plugin's `README.md`. Leave `unmeasured` until a suite exists.

Behavior: `skills/context-ledger/SKILL.md`.
