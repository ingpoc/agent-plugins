# context-ledger — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.codex-plugin/`. Cursor stdio uses `.cursor-plugin/plugin.json` and `.cursor-plugin/mcp.json`. Claude Code uses `.claude-plugin/plugin.json` and `.claude-plugin/mcp.json`.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## Install, refresh, and access

Run these from this directory. They update the Cursor, Codex, and Grok copies of this package. They do not create a ledger database, data root, or server, and they do not reindex a marketplace.

```sh
bash scripts/install-client.sh cursor
bash scripts/install-client.sh codex
bash scripts/install-client.sh grok
bash scripts/refresh-client.sh cursor
bash scripts/refresh-client.sh codex
bash scripts/refresh-client.sh grok
bash scripts/check-ledger-access.sh
```

`install-client.sh` copies this package into the client only when that client has no copy, and replaces every older copy in place. `refresh-client.sh` does that replacement and exits nonzero when the client has no copy or any copy stays older than this package. `check-ledger-access.sh` checks the one ledger. The default data root is `~/.context-ledger/` (mode `0700`). A shared root is explicit `--data ABSOLUTE_DIR` or `CONTEXT_LEDGER_DATA`. `PLUGIN_DATA` does not select it. Clients share that one root and `ledger_id`. Tools cannot choose either path.

There is one ledger, and it stays on the user's own machine. Do not start a second database, data root, or server for a cloud agent. Local use is `binding.json` in that one root. Cloud use is the same ledger through `CONTEXT_LEDGER_MCP_TOKEN` when no binding exists. The access check probes that endpoint, retries once, and exits nonzero if it cannot be reached. It does not start a local ledger. Do not print the token.

`find` is entity-scoped. A missing id is not proof of the wrong ledger. Confirm with `get` of the full decision id.

On the machine that hosts the ledger, and only when the access check reports no binding, the owner runs `python bin/context_ledger.py setup` (repeat when `requirements.lock` changes) and `python bin/context_ledger.py init --scope SCOPE --actor ACTOR`. Do not run those when a binding already exists. On macOS, setup installs `pyobjc-core`, `pyobjc-framework-Cocoa`, and `pyobjc-framework-NaturalLanguage` into that runtime. The helper uses that interpreter when the current one cannot import `objc`.

## After the client has loaded this package

1. MCP is loaded from the client manifest. Do not hand-add a server entry. All three start `bin/context-ledger-mcp serve`:
   - Portable `mcp.json` (Codex, Grok Build, other spec clients, and a Cursor surface that reads the root manifest): `command` `sh`, `args` `["-c", <resolver>, "context-ledger-mcp", "${PLUGIN_ROOT}", "${CURSOR_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}", "serve"]`, `env` `CONTEXT_LEDGER_MCP_CLIENT_TOKEN`=`${env:CONTEXT_LEDGER_MCP_TOKEN}`, no `cwd` (omitted cwd is the plugin root). The resolver contains no `${`. It tries `$1`, `$2`, `$3` (skip when empty or starting with `$`), then `$PLUGIN_ROOT`, `$CLAUDE_PLUGIN_ROOT`, `$CURSOR_PLUGIN_ROOT`, then `$PWD`. A root is executed only when its `plugin.json` version is at least this package version. An older root is skipped. The first current root does `shift 3` and execs `$root/bin/context-ledger-mcp`. If none match, it uses the same cache search and pinned-tag bootstrap as the Cursor manifest. An older cache copy is not executed. Bootstrap failure is stderr `PLUGIN_BOOTSTRAP_FAILED`.
   - Cursor: `.cursor-plugin/plugin.json` → `.cursor-plugin/mcp.json`. `command` `sh`, `args` `["-c", <resolver>, "context-ledger-mcp", "${CURSOR_PLUGIN_ROOT}", "serve"]`, no `cwd`, same client-token `env`. The resolver contains no `${`. A real `$1` is executed only when its `plugin.json` version is at least this package version. Otherwise the highest `context-ledger` launcher under `~/.cursor/plugins/cache` whose version is at least that pinned version (newest mtime on a tie). An older materialized copy is not executed. Otherwise tag `context-ledger-v<version>` in `${XDG_CACHE_HOME:-~/.cache}/context-ledger/plugin-<version>`. Each release must push that tag after merge. If the tag cannot be downloaded or its version does not match, stderr is `PLUGIN_BOOTSTRAP_FAILED` and the older copy is not executed.
   - Claude Code: `.claude-plugin/plugin.json` → `.claude-plugin/mcp.json`. `command` `${CLAUDE_PLUGIN_ROOT}/bin/context-ledger-mcp`, `args` `["serve"]`. No `env` interpolation.
   Cursor expands `${CURSOR_PLUGIN_ROOT}`. A root `./` command resolves against the workspace (ENOENT) and is not shipped. `doctor` exits nonzero with `CURSOR_DEST` when a dest has no healthy override and still has a relative command or a literal `${PLUGIN_*}` token. `python3 scripts/install_cursor_dest.py` repairs those pre-override dests to the absolute launcher (`cwd` `./`) and leaves a healthy override untouched. Do not put absolute interpreter paths in source `mcp.json`. Optional override: `CONTEXT_LEDGER_PYTHON`.
2. Client lists `find`, `get`, `record`, `append_event`, and `browse`. `browse` is the ledger view (`ui://context-ledger/browse.html`). `append_event` accepts `applied` (`task_id`, `decision_ids`); `get` returns applied and outcome events. Before each find, the server probes the ledger, retries once, and on failure names the URL or ledger file and does not fall back. A previous success is not reused. One server name, `context-ledger`. `serve` (and `--data DIR serve`) uses local stdio when `DATA/binding.json` exists (`--data`, else `CONTEXT_LEDGER_DATA`, else `~/.context-ledger`). With no binding and a non-empty `CONTEXT_LEDGER_MCP_TOKEN`, it bridges to `serve-http`. Else a non-empty `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` that does not start with `${` is exported as `CONTEXT_LEDGER_MCP_TOKEN` for the bridge only. Else a macOS Keychain item (service `context-ledger.mcp`, account `bearer_token`; tool `security`, override `CONTEXT_LEDGER_SECURITY_BIN`) supplies that token to the bridge process only. The launcher unsets `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` before exec of the local server or the bridge. Otherwise it exits nonzero with `LEDGER_UNBOUND` (the message names `bin/context_ledger.py init`, `CONTEXT_LEDGER_MCP_TOKEN`, the client token, and that keychain item) and does not create a data directory. No HTTP or SSE `mcp.json` entry. Only the ledger helper subagent uses the MCP; the parent only exchanges JSON with it. Owner CLI (`doctor`, `export`, `import`, `attest`, `purge`, `rebuild`, `migrate`, `resume-maintenance`, `scope-add`, `ensure-global-triggers`) is not an MCP tool.
   Remote (owner-run): `./bin/context-ledger-mcp serve-http [--port 8787] [--allowed-host HOST]` serves those tools over Streamable HTTP at `http://127.0.0.1:PORT/mcp` (loopback only). It refuses to start unless `CONTEXT_LEDGER_MCP_TOKEN` (32+ chars) is set and answers 401 to any request without `Authorization: Bearer <token>`. Tunnel and launchd: `skills/context-ledger/references/remote-mcp.md`.
3. Ensure always-on ledger triggers in `~/.codex/AGENTS.md` if that file exists:
   `python bin/context_ledger.py ensure-global-triggers`
   Idempotent. Inserts missing BEFORE lookup and AFTER save lines or updates existing marked lines. Then `workflow lint`. Do not rewrite other rules. Skip if the file is absent.
4. Before uninstall, export the owner ledger. The default `~/.context-ledger/` is independent of the client cache; a client-managed custom data root may not persist.

## Support (this pass)

Exercised: macOS arm64 with the local CPython that ran contract tests. Release-blocked until run: macOS x86_64 (cryptography 50.0.1 has no macOS x86_64 wheel), Ubuntu 22.04/24.04, Windows 11, CPython versions not used in this pass. Windows arm64 is deferred.

A hostile local user with write access to plugin, runtime, or binding can subvert the service. Local storage does not prevent disclosure to the model provider.

Published scores: this plugin's `README.md`. Leave `unmeasured` until a suite exists.

Behavior: `skills/context-ledger/SKILL.md`.
