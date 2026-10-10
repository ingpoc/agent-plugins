#!/bin/sh
# launchd entry for `serve-http`. Token comes from the login keychain at start.
set -eu
CONF="${CONTEXT_LEDGER_REMOTE_ENV:-$HOME/.context-ledger/remote/remote.env}"
. "$CONF"
: "${PLUGIN_ROOT:?set PLUGIN_ROOT in $CONF}" "${PORT:=8787}"
TOKEN=$(/usr/bin/security find-generic-password -s "${KEYCHAIN_SERVICE:-context-ledger.mcp}" -a "${KEYCHAIN_ACCOUNT:-bearer_token}" -w 2>/dev/null) || TOKEN=""
if [ -z "$TOKEN" ]; then
  echo '{"ok":false,"error":{"code":"UNAUTHENTICATED_CONFIG","detail":"keychain item missing"}}' >&2
  exit 78
fi
export CONTEXT_LEDGER_MCP_TOKEN="$TOKEN"
unset TOKEN
export CONTEXT_LEDGER_MCP_ALLOWED_HOSTS="${ALLOWED_HOSTS:-}"
[ -n "${LEDGER_PYTHON:-}" ] && export CONTEXT_LEDGER_PYTHON="$LEDGER_PYTHON"
exec "$PLUGIN_ROOT/bin/context-ledger-mcp" serve-http --port "$PORT"
