# Remote Context Ledger MCP

One ledger on the owner's Mac Mini, reachable by remote agents (e.g. Cursor cloud agents) over Streamable HTTP. Same four tools as stdio: `find`, `get`, `record`, `append_event`. The skill's rules apply unchanged.

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

- Server mode: stateless with JSON responses. Send `Accept: application/json, text/event-stream` and call `initialize`, then `tools/list`.

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
