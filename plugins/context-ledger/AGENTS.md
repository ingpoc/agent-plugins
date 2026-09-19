# context-ledger — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.cursor-plugin/` or `.codex-plugin/` to this package.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. Run `python bin/context_ledger.py --data "${PLUGIN_DATA}" setup` from this directory (network, or `--wheelhouse ABSOLUTE_DIR` for offline). Repeat setup when `requirements.lock` changes.
2. Run `python bin/context_ledger.py --data "${PLUGIN_DATA}" init --scope SCOPE --actor ACTOR`. Optional `--ledger-dir ABSOLUTE_DIR` (default `${PLUGIN_DATA}/ledger`). Binding is owner-selected; tools cannot choose paths. To share Cursor and Codex, bind both to the same `--ledger-dir` and `ledger_id`. Sharing is the bind, not one MCP process.
3. Source `mcp.json` uses `command` `python`. Cursor dest may use an absolute interpreter. Agents/Codex dest must stay package-valid: bare `python` or `python3`, never an absolute path. Never copy a dest-absolute `command` back into source `mcp.json`. `cwd` stays `${PLUGIN_ROOT}`.
4. Client lists four tools: `find`, `get`, `record`, `append_event`. Owner CLI (`doctor`, `export`, `import`, `attest`, `purge`, `rebuild`, `migrate`, `resume-maintenance`, `scope-add`, `ensure-global-triggers`) is not an MCP tool.
5. Ensure always-on ledger triggers in `~/.codex/AGENTS.md` if that file exists:
   `python bin/context_ledger.py --data "${PLUGIN_DATA}" ensure-global-triggers`
   Idempotent. Inserts the BEFORE lookup and AFTER save lines if missing. Then `workflow lint`. Do not rewrite other rules. Skip if the file is absent.
6. Before uninstall, export the owner ledger. Client uninstall does not preserve `${PLUGIN_DATA}` or backups.

## Support (this pass)

Exercised: macOS arm64 with the local CPython that ran contract tests. Release-blocked until run: macOS x86_64 (cryptography 50.0.1 has no macOS x86_64 wheel), Ubuntu 22.04/24.04, Windows 11, CPython versions not used in this pass. Windows arm64 is deferred.

A hostile local user with write access to plugin, runtime, or binding can subvert the service. Local storage does not prevent disclosure to the model provider.

Published scores: this plugin's `README.md`. Leave `unmeasured` until a suite exists.

Behavior: `skills/context-ledger/SKILL.md`.
