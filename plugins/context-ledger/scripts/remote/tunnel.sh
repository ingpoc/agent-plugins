#!/bin/sh
# launchd entry for the public tunnel. Writes the live URL to $URL_FILE only
# after a request through the tunnel reaches serve-http (401 without a token).
set -u
CONF="${CONTEXT_LEDGER_REMOTE_ENV:-$HOME/.context-ledger/remote/remote.env}"
. "$CONF"
PORT="${PORT:-8787}"
URL_FILE="${URL_FILE:-$HOME/.context-ledger/remote-url}"
FIFO=""
CHILD=""
rm -f "$URL_FILE"
cleanup() {
  rm -f "$URL_FILE"
  if [ -n "${FIFO:-}" ]; then
    rm -f "$FIFO"
  fi
  if [ -n "${CHILD:-}" ]; then
    kill "$CHILD" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

publish() { # $1 = base URL; probe until serve-http answers 401 through the tunnel
  i=0
  while [ $i -lt 60 ]; do
    code=$(/usr/bin/curl -s -o /dev/null -m 10 -w '%{http_code}' -X POST "$1/mcp" || true)
    if [ "$code" = "000" ]; then
      # Local resolver can lag new public names (Funnel); retry via public DNS.
      host=${1#https://}; host=${host%%/*}
      ip=$(/usr/bin/dig +short "$host" A @1.1.1.1 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
      [ -n "$ip" ] && code=$(/usr/bin/curl -s -o /dev/null -m 10 -w '%{http_code}' \
        --resolve "$host:443:$ip" -X POST "$1/mcp" || true)
    fi
    if [ "$code" = "401" ]; then
      printf '%s/mcp\n' "$1" > "$URL_FILE.tmp" && mv "$URL_FILE.tmp" "$URL_FILE"
      echo "published $1/mcp"
      return 0
    fi
    i=$((i + 1)); sleep 5
  done
  echo "tunnel never reached serve-http (last HTTP $code)" >&2
  return 1
}

case "${TUNNEL_MODE:-quick}" in
  quick)
    # Random trycloudflare URL; changes on every restart. Host is rewritten to
    # loopback so serve-http needs no allow-list entry for it.
    FIFO=$(mktemp -u "${TMPDIR:-/tmp}/cl-tunnel.XXXXXX"); mkfifo "$FIFO"
    "${CLOUDFLARED:-cloudflared}" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" \
      --http-host-header "127.0.0.1:$PORT" > "$FIFO" 2>&1 &
    CHILD=$!
    URL=""
    while IFS= read -r line; do
      printf '%s\n' "$line"
      if [ -z "$URL" ]; then
        URL=$(printf '%s' "$line" | grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
        [ -n "$URL" ] && publish "$URL" &
      fi
    done < "$FIFO"
    rm -f "$FIFO"
    ;;
  command)
    # Stable URL: PUBLIC_URL plus the tunnel's own foreground command, e.g.
    # TUNNEL_CMD="tailscale funnel 8787" or "cloudflared tunnel run NAME".
    : "${PUBLIC_URL:?set PUBLIC_URL}" "${TUNNEL_CMD:?set TUNNEL_CMD}"
    sh -c "exec $TUNNEL_CMD" &
    CHILD=$!
    publish "${PUBLIC_URL%/}" &
    wait "$CHILD"
    ;;
  *) echo "unknown TUNNEL_MODE" >&2; exit 78 ;;
esac
exit 1
