# Remote Context Ledger MCP

One ledger on the owner's Mac Mini, reachable by remote agents (e.g. Cursor cloud agents) over Streamable HTTP. Same four tools as stdio: `find`, `get`, `record`, `append_event`. The skill's rules apply unchanged.

## Connect (remote agent)

- URL: the content of `~/.context-ledger/remote-url` on the Mini (the endpoint path is `/mcp`). It exists only while the tunnel is live. A quick tunnel (`*.trycloudflare.com`) gets a new URL on every restart. A stable URL is the same every time.
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

4. LaunchAgents `com.gurusharan.context-ledger-mcp` (runs `serve-http.sh`) and `com.gurusharan.context-ledger-tunnel` (runs `tunnel.sh`). Each has `RunAtLoad` and `KeepAlive`, runs `/bin/sh <script>`, and logs to `~/Library/Logs/context-ledger/{mcp-http,tunnel}.log`. Load each job with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

`serve-http.sh` reads the token from the keychain and runs `bin/context-ledger-mcp serve-http --port $PORT`, which listens on 127.0.0.1 only. `tunnel.sh` writes `remote-url` once a request through the tunnel gets a 401 from the server.

## Stable URL (config change only)

Tailscale Funnel (after the tailnet enables HTTPS and Funnel for this node):

```sh
TUNNEL_MODE=command
PUBLIC_URL="https://<node>.<tailnet>.ts.net"
TUNNEL_CMD="tailscale funnel 8787"     # add --socket=... for a userspace tailscaled
ALLOWED_HOSTS="<node>.<tailnet>.ts.net"
```

Named Cloudflare tunnel: `PUBLIC_URL="https://<hostname>"` and `TUNNEL_CMD="cloudflared tunnel run <name>"`, with `ALLOWED_HOSTS="<hostname>"`. Then restart both jobs.

## Restart and check

```sh
launchctl kickstart -k gui/$(id -u)/com.gurusharan.context-ledger-mcp
launchctl kickstart -k gui/$(id -u)/com.gurusharan.context-ledger-tunnel
cat ~/.context-ledger/remote-url
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$(cat ~/.context-ledger/remote-url)"   # 401
```

After changing package code, re-sync `~/.context-ledger/remote/plugin` and restart the server job.
