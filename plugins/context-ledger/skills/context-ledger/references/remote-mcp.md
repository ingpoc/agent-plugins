# Remote Context Ledger MCP

One `context-ledger` server everywhere. The same four tools (`find`, `get`, `record`, `append_event`) run as local stdio when a binding exists, and as a bridge to the Mini's `serve-http` when it does not. The skill's rules apply unchanged.

Only the ledger helper subagent calls the MCP. The parent exchanges JSON with that helper and does not call ledger tools.

## How `serve` chooses

The Cursor override and the portable `mcp.json` both start `bin/context-ledger-mcp serve`. The launcher picks the transport:

- Data dir is `--data DIR` when that precedes `serve`, otherwise `CONTEXT_LEDGER_DATA`, otherwise `~/.context-ledger`.
- `DATA/binding.json` exists: local stdio, unchanged.
- No binding, and `CONTEXT_LEDGER_MCP_TOKEN` is non-empty: stdio bridge to `${CONTEXT_LEDGER_MCP_URL:-https://gurusharan-mac-codex.taild2e98e.ts.net/mcp}`.
- Neither: stderr JSON `LEDGER_UNBOUND`, nonzero exit, and no data directory is created.

Cloud agents do not add a url-type `mcp.json` entry. The server name stays `context-ledger`.

Cursor loads `.cursor-plugin/mcp.json`: stdio `command` `${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp`, `args` `["serve"]`, `cwd` `${CURSOR_PLUGIN_ROOT}`. Cursor expands `${CURSOR_PLUGIN_ROOT}`. Portable root `mcp.json` stays `./bin/context-ledger-mcp` with `cwd` `./` for Codex and other spec clients.

A dest with that override is valid. `doctor` exits nonzero with `CURSOR_DEST` when a dest has no healthy override and still has a relative command or a literal `${PLUGIN_ROOT}` or `${PLUGIN_DATA}`. `python3 scripts/install_cursor_dest.py` only repairs those pre-override dests (absolute launcher) and leaves a healthy override untouched.

## Cloud secret

`CONTEXT_LEDGER_MCP_TOKEN` is the bearer token for the Mini's `serve-http`. On Cursor cloud agents it is a Runtime Secret, scope All Repositories. The owner copies it from the Mini login keychain (service `context-ledger.mcp`, account `bearer_token`) into that secret. Do not paste the token into a repo, chat, or log.

A missing token or an unreachable ledger fails loud. The bridge does not create a local ledger.

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
