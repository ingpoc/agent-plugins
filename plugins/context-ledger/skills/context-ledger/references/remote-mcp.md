# Remote Context Ledger MCP

One `context-ledger` server everywhere. The same four tools (`find`, `get`, `record`, `append_event`) run as local stdio when a binding exists, and as a bridge to the Mini's `serve-http` when it does not. The skill's rules apply unchanged.

Only the ledger helper subagent calls the MCP. The parent exchanges JSON with that helper and does not call ledger tools.

## How `serve` chooses

Every shipped manifest starts `bin/context-ledger-mcp serve`. The launcher picks the transport:

- Data dir is `--data DIR` when that precedes `serve`, otherwise `CONTEXT_LEDGER_DATA`, otherwise `~/.context-ledger`.
- `DATA/binding.json` exists: local stdio.
- No binding, and `CONTEXT_LEDGER_MCP_TOKEN` is non-empty: stdio bridge to `${CONTEXT_LEDGER_MCP_URL:-https://gurusharan-mac-codex.taild2e98e.ts.net/mcp}`.
- No binding and no env token, and `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` is non-empty and does not start with `${`: that value is passed only to the bridge as `CONTEXT_LEDGER_MCP_TOKEN`. A leftover `${env:CONTEXT_LEDGER_MCP_TOKEN}` is ignored. The launcher unsets `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` before local stdio or the bridge.
- No binding and no env token, and `security find-generic-password -s context-ledger.mcp -a bearer_token -w` prints a token (`CONTEXT_LEDGER_SECURITY_BIN` overrides the tool): that value is passed only to the bridge process.
- Otherwise: stderr JSON `LEDGER_UNBOUND` naming `bin/context_ledger.py init`, `CONTEXT_LEDGER_MCP_TOKEN`, the client token, and the keychain item, nonzero exit, and no data directory is created.

Cloud agents do not add a url-type `mcp.json` entry. The server name stays `context-ledger`.

A dest whose `.cursor-plugin/mcp.json` uses `${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp`, or `sh` with the resolver below and no `cwd`, is valid. `doctor` exits nonzero with `CURSOR_DEST` when a dest has no healthy override and still has a relative command or a literal `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`. `python3 scripts/install_cursor_dest.py` only repairs those pre-override dests (absolute launcher) and leaves a healthy override untouched.

## Cloud secret

`CONTEXT_LEDGER_MCP_TOKEN` is the bearer token for the Mini's `serve-http`. On Cursor cloud agents it is a Runtime Secret, scope All Repositories. The owner copies it from the Mini login keychain (service `context-ledger.mcp`, account `bearer_token`) into that secret. Do not paste the token into a repo, chat, or log.

A missing token or an unreachable ledger fails loud. The bridge does not create a local ledger. Before the first `tools/call` named `find`, the bridge probes the ledger URL, retries that probe once, and on failure returns an error naming that URL. It does not forward the find and does not open a local ledger.

## Enable a Cursor cloud agent or Project

1. Secret: Cursor dashboard, Cloud Agents, My Secrets (`cursor.com/dashboard/cloud-agents?view=my-secrets`) lists `CONTEXT_LEDGER_MCP_TOKEN` (Runtime Secret, All Repositories). Guru's account already has it. If it is missing, the owner runs `security find-generic-password -s context-ledger.mcp -a bearer_token -w | tr -d '\n' | pbcopy`, pastes it into the form, then clears the clipboard with `printf '' | pbcopy`.
2. Plugin: the account-installed context-ledger plugin starts the one `context-ledger` server and bridges automatically. Nothing else to configure.
3. Brief: the MCP is for the agent's ledger helper thread; the parent only exchanges JSON with it. Rule: "follow the context-ledger skill: look up at task start, capture and close out at the end; if the token is missing or the ledger is unreachable, fail loud and never fall back to a local copy". For a Project, put that rule in its shared context (`docs/project-context.md`).
4. Verify: the helper's first run reports `initialize` succeeding and `tools/list` returning exactly `append_event`, `find`, `get`, `record`. Treat an empty or missing result as a failure.

## One ledger

Clients with a binding read the SQLite file over stdio. Unbound clients go through the bridge to the Mini, which is the `serve-http` process for that ledger. Concurrent local access uses SQLite `begin immediate` and `busy_timeout=5000`.

- Every binding on the Mini names the same `ledger_dir` and `ledger_id`.
- Codex on the Mini loads the portable stdio command.
- The launchd `serve-http` uses `~/.context-ledger`.
- Check: one `find` (same query, `limit` 3) through each client's MCP (Cursor IDE, `cursor agent -p --approve-mcps`, `codex exec`, a cloud helper) returns the same `decision_id`s in the same order.

## Owner setup (Mini)

1. Token: `security add-generic-password -s context-ledger.mcp -a bearer_token -l context-ledger.mcp.bearer_token -w "$(python3 -c 'import secrets;print(secrets.token_hex(32))')" -U`
2. `mkdir -p ~/.context-ledger/remote` and copy `scripts/remote/serve-http.sh` and `scripts/remote/tunnel.sh` there. Copy this package to `~/.context-ledger/remote/plugin`, because launchd cannot read `~/Documents` (TCC).
3. Create `~/.context-ledger/remote/remote.env` (mode 600):

```sh
PLUGIN_ROOT="$HOME/.context-ledger/remote/plugin"
LEDGER_PYTHON="/path/to/python3.11+"
PORT=8787
ALLOWED_HOSTS=""          # Host headers to accept besides loopback (stable tunnels)
TUNNEL_MODE=quick         # quick | command
CLOUDFLARED=/opt/homebrew/bin/cloudflared
PUBLIC_URL=""             # command mode only
TUNNEL_CMD=""             # command mode only, foreground
```

1. LaunchAgents `com.gurusharan.context-ledger-mcp` (runs `serve-http.sh`) and `com.gurusharan.context-ledger-funnel` (runs `tunnel.sh`). Each has `RunAtLoad` and `KeepAlive`, runs `/bin/sh <script>`, and logs to `~/Library/Logs/context-ledger/{mcp-http,funnel}.log`. Load each job with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

`serve-http.sh` reads the token from the keychain and runs `bin/context-ledger-mcp serve-http --port $PORT`, which listens on 127.0.0.1 only. `tunnel.sh` writes `remote-url` once a request through the tunnel gets a 401 from the server.

`serve-http` is stateless. `Accept` of `application/json` (or JSON and `text/event-stream`) returns one JSON body. `Accept: text/event-stream` alone returns one SSE `event: message`. `notifications/initialized` is HTTP 202. Unauthenticated `/mcp` is 401.

## Tunnel modes (config change only)

Current: Tailscale Funnel on 443, mounted at `/mcp`, through the codex-mac-bridge userspace `tailscaled`:

```sh
TUNNEL_MODE=command
PUBLIC_URL="https://gurusharan-mac-codex.taild2e98e.ts.net"
TUNNEL_CMD="/opt/homebrew/bin/tailscale --socket=$HOME/.local/share/codex-mac-bridge/tailscaled.sock funnel --yes --https=443 --set-path=/mcp http://127.0.0.1:8787/mcp"
ALLOWED_HOSTS="gurusharan-mac-codex.taild2e98e.ts.net"
```

- The funnel runs in the foreground under launchd `KeepAlive`. It is re-applied after a reboot, a job restart, or a `tailscaled` restart. It adds a foreground serve entry and leaves the tailnet `tcp:80 → 127.0.0.1:8765` serve alone.
- HTTPS certs need a cert store: that `tailscaled` runs with `--statedir=$HOME/.local/share/codex-mac-bridge/tailscaled-statedir` alongside its `--state` file.
- The tailnet must allow Funnel for the node (ports 443, 8443, 10000) and have HTTPS certs on.

Other modes: a named Cloudflare tunnel uses `TUNNEL_CMD="cloudflared tunnel run <name>"` with its own `PUBLIC_URL` and `ALLOWED_HOSTS`. A quick tunnel (`TUNNEL_MODE=quick`) gets a random trycloudflare URL that changes on every restart. Restart the tunnel job after editing `remote.env`.

## Restart and check

```sh
launchctl kickstart -k gui/$(id -u)/com.gurusharan.context-ledger-mcp
launchctl kickstart -k gui/$(id -u)/com.gurusharan.context-ledger-funnel
cat ~/.context-ledger/remote-url
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$(cat ~/.context-ledger/remote-url)"   # 401
```

After changing package code, re-sync `~/.context-ledger/remote/plugin` and restart the server job.

## Design: out-of-the-box MCP per client

One ledger. Each client reads its own manifest and spawns the same launcher. `serve` then chooses: binding → local stdio; else a non-empty `CONTEXT_LEDGER_MCP_TOKEN` → bridge; else a non-placeholder `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` → bridge; else the macOS Keychain item service `context-ledger.mcp` account `bearer_token` → bridge; else stderr `LEDGER_UNBOUND`. No hand-added MCP entry and no HTTP/SSE server block.

| Client | Manifest | Root variable | Runtime |
| --- | --- | --- | --- |
| Cursor IDE / CLI local | `.cursor-plugin/plugin.json` → `.cursor-plugin/mcp.json` | root arg `${CURSOR_PLUGIN_ROOT}`; no `cwd` | Binding on this machine → local; else env token; else Keychain → bridge; else `LEDGER_UNBOUND` |
| Cursor cloud VM | `.cursor-plugin/mcp.json` `sh` resolver | literal `${CURSOR_PLUGIN_ROOT}` falls through to a cache copy at least the pinned version, else pinned tag bootstrap | No binding and no Keychain. `cloudAgentEnvLookup` → `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` → bridge; else `LEDGER_UNBOUND` |
| Cursor cloud on Mini worker | `.cursor-plugin/mcp.json` `sh` resolver | cache copy at least the pinned version when present, else pinned tag bootstrap | Mini home binding → local; else env token or `CONTEXT_LEDGER_MCP_CLIENT_TOKEN`; else the Mini Keychain item → bridge |
| Codex | portable `mcp.json` | `${PLUGIN_ROOT}` expanded in root arg 1; args 2 and 3 stay literal and are skipped; `PLUGIN_ROOT` exported; omitted cwd is the plugin root | Same order. The `sh` resolver skips a leftover root arg that still starts with `$` and uses the exported root |
| Claude Code | `.claude-plugin/plugin.json` `mcpServers` path → `.claude-plugin/mcp.json` | `${CLAUDE_PLUGIN_ROOT}` expanded in the command and exported | Same order |
| Grok Build / other spec clients | portable `mcp.json` | `${PLUGIN_ROOT}`, same as Codex | Same order |

Cursor's manifest precedence is `.cursor-plugin/plugin.json`, then `.claude-plugin/plugin.json`, then `plugin.json`, so the Claude package does not change Cursor spawn. Codex accepts a bare command or a contained `./` command and rejects `${CURSOR_PLUGIN_ROOT}` in `command`. A root `./` command is resolved by Cursor against the workspace (ENOENT), so the portable manifest uses bare `sh`. The resolver script contains no `${` sequence: Cursor's interpolator would rewrite `${X:-default}` inside `args` to the default when `X` is unset.

## Cloud: plugin not materialized

Cursor cloud sends `.cursor-plugin/mcp.json` even when the VM has no plugin directory, so `${CURSOR_PLUGIN_ROOT}` stays literal and a `cwd` of that token would fail spawn. The Cursor manifest is `command` `sh` with no `cwd`. Its resolver, and the portable resolver after the root checks above:

1. Exec `$1/bin/context-ledger-mcp` when `$1` is non-empty, does not start with `$`, and that launcher is executable. The portable resolver still tries its three root args and the exported root variables first.
2. Else exec the highest `plugin.json` `version` among executable `context-ledger` launchers under `~/.cursor/plugins/cache` (`cache/*/context-ledger/*/bin/context-ledger-mcp` and `cache/*/*/*/bin/context-ledger-mcp`) whose version is at least the pinned version. Equal versions use the newest mtime. An older materialized copy is not executed.
3. Else download tag `context-ledger-v<version>` into `${XDG_CACHE_HOME:-~/.cache}/context-ledger/plugin-<version>` and exec it. The version literal appears once per manifest and matches `plugin.json`. Failure is one stderr JSON line, `PLUGIN_BOOTSTRAP_FAILED`, and a nonzero exit. The older copy is not executed. Stdout stays empty until the launcher runs.

Each release must push tag `context-ledger-v<version>` after merge.

HTTP entries are not shipped. Cursor CLI expands `${env:NAME}` in HTTP headers, and a missing variable fails with `HTTP 404 Invalid OAuth error response`. Codex and the Agent Plugins spec keep headers literal, and headers must not hold secrets. The Mini GUI has no header env. Remote mode stays inside the stdio launcher's bridge.
