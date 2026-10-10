# Remote Context Ledger MCP

One ledger on the owner's Mac Mini, reachable by remote agents (e.g. Cursor cloud agents) over Streamable HTTP. Same four tools as stdio: `find`, `get`, `record`, `append_event`. The skill's rules apply unchanged.

The MCP is wired for the retained ledger helper subagent only. Under the context-ledger skill the helper is the only caller of `find`, `get`, `record`, and `append_event`; the parent (main) agent never calls ledger tools and only exchanges JSON with the helper. Configure the MCP wherever the helper runs: the Cursor or Codex subagent on the Mini, and the cloud agent's helper thread.

## One ledger (local and cloud)

Every client reads and writes the same SQLite file. Local clients open it over stdio; remote clients go through `serve-http`. Concurrent access is safe because the store uses SQLite on local disk with `begin immediate` and `busy_timeout=5000`, so no local client needs to proxy through HTTP.

- Data root `~/.context-ledger` (`--data`, or `CONTEXT_LEDGER_DATA`). Its `binding.json` fixes `ledger_dir` and `ledger_id`. Every binding on the Mini (`~/.context-ledger`, `~/.agents/plugin-data/context-ledger`, and Codex's plugin data dir) must name the same `ledger_dir` and `ledger_id`.
- Cursor IDE and Cursor CLI load the marketplace cache copy `~/.cursor/plugins/cache/ingpoc-agent-plugins/context-ledger/<hash>/`. After every marketplace install or refresh, run `python3 scripts/install_cursor_dest.py` so the dest `mcp.json` has the absolute launcher and `--data ~/.context-ledger`, then reload the MCP server in Cursor. `bin/context_ledger.py doctor` exits 2 with `CURSOR_DEST` while a cache copy is still relative or still has `${PLUGIN_*}`.
- Codex loads `~/.codex/plugins/cache/personal/context-ledger/<version>/` over stdio (`codex mcp list` shows it).
- Remote: the launchd `serve-http` uses the default data root `~/.context-ledger`.
- Check: run one `find` (same query, `limit` 3) through each client's MCP: Cursor IDE, `cursor agent -p --approve-mcps`, `codex exec`, and the remote URL. All four must return the same `decision_id`s in the same order.

## Connect (remote agent)

- URL: `https://gurusharan-mac-codex.taild2e98e.ts.net/mcp` (Tailscale Funnel, stable). On the Mini, `~/.context-ledger/remote-url` holds the live URL and exists only while the tunnel is reachable.
- Auth header on every request: `Authorization: Bearer <token>`. The token lives in the Mini's login keychain: service `context-ledger.mcp`, account `bearer_token`. Give it to the cloud agent as a secret (for example `CONTEXT_LEDGER_MCP_TOKEN`). Never paste it into a repo, chat, or log.
- Cursor `mcp.json` entry:

```json
{
  "mcpServers": {
    "context-ledger": {
      "url": "https://<host>/mcp",
      "headers": { "Authorization": "Bearer ${env:CONTEXT_LEDGER_MCP_TOKEN}" }
    }
  }
}
```

- Server mode: stateless. `Accept: application/json` (or both JSON and `text/event-stream`) returns one JSON body. `Accept: text/event-stream` alone returns one SSE `event: message`. `notifications/initialized` is HTTP 202. `GET /mcp` and `DELETE /mcp` are 405 (no listen stream, no session to terminate). Unauthenticated `/mcp` is 401. `/.well-known/*` and `POST /register` are 404 with no `WWW-Authenticate` challenge.

## Enable a Cursor cloud agent or Project

1. Secret: Cursor dashboard, Cloud Agents, My Secrets (`cursor.com/dashboard/cloud-agents?view=my-secrets`) must list `CONTEXT_LEDGER_MCP_TOKEN` with scope All Repositories, type Runtime Secret. Guru's account already has it, so new agents get it automatically. If it's missing, the owner adds it by copying the value with `security find-generic-password -s context-ledger.mcp -a bearer_token -w | tr -d '\n' | pbcopy`, pasting it into the form, then clearing the clipboard with `printf '' | pbcopy`. Bots can't forward secrets with `secret_names` on this account.
2. Brief: in the launch or reply prompt, say the MCP is for the agent's ledger helper thread (the parent only exchanges JSON with it), and give the URL `https://gurusharan-mac-codex.taild2e98e.ts.net/mcp`, the header `Authorization: Bearer $CONTEXT_LEDGER_MCP_TOKEN`, the four tools, and the rule "follow the context-ledger skill: look up at task start, capture and close out at the end; if the token is missing or the ledger is unreachable, fail loud and never fall back to a local copy". Either the agent adds the `mcp.json` entry above, or it calls over HTTP.
3. Project: put that same rule in the Project's shared context (`docs/project-context.md` in its Agent Store) so every thread inherits it.
4. Verify: the agent's first run reports `initialize` succeeding and `tools/list` returning exactly `append_event`, `find`, `get`, `record`. Treat an empty or missing result as a failure.

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

4. LaunchAgents `com.gurusharan.context-ledger-mcp` (runs `serve-http.sh`) and `com.gurusharan.context-ledger-funnel` (runs `tunnel.sh`). Each has `RunAtLoad` and `KeepAlive`, runs `/bin/sh <script>`, and logs to `~/Library/Logs/context-ledger/{mcp-http,funnel}.log`. Load each job with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

`serve-http.sh` reads the token from the keychain and runs `bin/context-ledger-mcp serve-http --port $PORT`, which listens on 127.0.0.1 only. `tunnel.sh` writes `remote-url` once a request through the tunnel gets a 401 from the server.

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
