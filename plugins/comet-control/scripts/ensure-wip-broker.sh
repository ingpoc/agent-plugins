#!/usr/bin/env bash
# Comet-only broker lifecycle and readiness probe.
set -euo pipefail

ROOT="${COMET_CONTROL_WIP_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
MODE="${1:-probe}"
[[ $# -gt 0 ]] && shift
[[ "${1:-}" == "--json" ]] && shift
[[ $# -eq 0 ]] || { echo "Usage: ensure-wip-broker.sh [probe|start] [--json]" >&2; exit 2; }

BROKER="$ROOT/plugin/comet_control/native/broker.py"
RUN="$ROOT/run"
SOCKET="$RUN/comet-control.sock"
PID_FILE="$RUN/comet-control-broker.pid"
LOG="$RUN/comet-control-broker.log"
LABEL="local.comet-control.broker"
PLIST="${COMET_CONTROL_USER_HOME:-$HOME}/Library/LaunchAgents/$LABEL.plist"
COMET_EXECUTABLE="${COMET_CONTROL_EXPECTED_BROWSER_EXECUTABLE:-/Applications/Comet.app/Contents/MacOS/Comet}"
COMET_PROFILE="${COMET_CONTROL_USER_DATA_DIR:-${COMET_CONTROL_USER_HOME:-$HOME}/Library/Application Support/Comet}"
EXTENSION_ID="${COMET_CONTROL_EXTENSION_ID:-iknnjffofidficdmmkimcjbceookglgi}"
PYTHON="${COMET_CONTROL_PYTHON:-$(python3 -c 'import sys; print(sys.executable)')}"
PYTHON="$("$PYTHON" -c 'import pathlib,sys; print(pathlib.Path(sys.executable).resolve())')"

"$PYTHON" -c 'import importlib.metadata as m; version=m.version("websockets"); assert version == "16.0", f"websockets 16.0 required, found {version}"'

export COMET_CONTROL_BRIDGE_SOCKET="$SOCKET"
export COMET_CONTROL_EXPECTED_BROWSER_EXECUTABLE="$COMET_EXECUTABLE"
export COMET_CONTROL_EXPECTED_USER_DATA_DIR="$COMET_PROFILE"
export COMET_CONTROL_EXPECTED_EXTENSION_ORIGIN="chrome-extension://$EXTENSION_ID/"

probe() {
  [[ -f "$BROKER" ]] || {
    printf '{"success":false,"error_code":"BROKER_NOT_DEPLOYED"}\n'
    return 2
  }
  "$PYTHON" "$BROKER" probe
}

session_count() {
  "$PYTHON" - "$SOCKET" <<'PY'
import json
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(8)
try:
    client.connect(sys.argv[1])
    client.sendall(json.dumps({"type": "sessions", "timeoutSeconds": 5}).encode())
    client.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    payload = json.loads(b"".join(chunks))
    if payload.get("success") is not True or not isinstance(payload.get("sessions"), list):
        raise RuntimeError(payload.get("error") or "session inventory unavailable")
    print(len(payload["sessions"]))
finally:
    client.close()
PY
}

start() {
  mkdir -p "$RUN"
  if [[ "${COMET_CONTROL_BROKER_DIRECT:-0}" == "1" ]]; then
    nohup "$PYTHON" "$BROKER" >>"$LOG" 2>&1 </dev/null &
    local direct_pid=$!
    printf '%s\n' "$direct_pid" > "$PID_FILE"
    for _ in {1..30}; do
      [[ -S "$SOCKET" ]] && { echo "Comet Control broker started (pid $direct_pid)"; return 0; }
      kill -0 "$direct_pid" 2>/dev/null || break
      sleep 0.1
    done
    echo "Comet Control broker failed to start; see $LOG" >&2
    exit 2
  fi
  local python uid target desired_hash status loaded_hash loaded_python sessions
  python="$PYTHON"
  uid="$(id -u)"
  target="gui/$uid/$LABEL"
  desired_hash="$(shasum -a 256 "$BROKER" | awk '{print $1}')"
  mkdir -p "$(dirname "$PLIST")"
  "$PYTHON" - "$PLIST" "$LABEL" "$python" "$BROKER" "$LOG" \
    "$SOCKET" "$COMET_EXECUTABLE" "$COMET_PROFILE" "$EXTENSION_ID" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

path, label, python, broker, log, sock, executable, profile, extension_id = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [python, broker],
    "EnvironmentVariables": {
        "COMET_CONTROL_BRIDGE_SOCKET": sock,
        "COMET_CONTROL_EXPECTED_BROWSER_EXECUTABLE": executable,
        "COMET_CONTROL_EXPECTED_USER_DATA_DIR": profile,
        "COMET_CONTROL_EXPECTED_EXTENSION_ORIGIN": f"chrome-extension://{extension_id}/",
    },
    "KeepAlive": True,
    "RunAtLoad": True,
    "ThrottleInterval": 2,
    "StandardOutPath": log,
    "StandardErrorPath": log,
}
destination = Path(path)
temporary = destination.with_suffix(".tmp")
with temporary.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
os.chmod(temporary, 0o600)
os.replace(temporary, destination)
PY
  if launchctl print "$target" >/dev/null 2>&1; then
    if [[ -S "$SOCKET" ]]; then
      status="$(probe 2>/dev/null || true)"
      loaded_hash="$(printf '%s' "$status" | "$PYTHON" -c 'import json,sys; print((json.load(sys.stdin).get("broker") or {}).get("broker_build_sha256") or "")' 2>/dev/null || true)"
      loaded_python="$(printf '%s' "$status" | "$PYTHON" -c 'import json,sys; print((json.load(sys.stdin).get("broker") or {}).get("python_executable") or "")' 2>/dev/null || true)"
      if [[ "$loaded_hash" == "$desired_hash" && "$loaded_python" == "$python" ]]; then
        echo "Comet Control broker already supervised by $LABEL"
        return 0
      fi
      sessions="$(session_count 2>/dev/null || true)"
      if [[ ! "$sessions" =~ ^[0-9]+$ ]]; then
        echo "Comet Control broker is stale but the lease inventory is unavailable; refusing restart" >&2
        exit 3
      fi
      if (( sessions > 0 )); then
        echo "Comet Control broker is stale with $sessions active lease(s); close them before restart" >&2
        exit 4
      fi
      launchctl kickstart -k "$target"
    else
      launchctl kickstart -k "$target"
    fi
  else
    launchctl bootstrap "gui/$uid" "$PLIST"
  fi
  for _ in {1..300}; do
    if [[ -S "$SOCKET" ]]; then
      status="$(probe 2>/dev/null || true)"
      loaded_hash="$(printf '%s' "$status" | "$PYTHON" -c 'import json,sys; print((json.load(sys.stdin).get("broker") or {}).get("broker_build_sha256") or "")' 2>/dev/null || true)"
      loaded_python="$(printf '%s' "$status" | "$PYTHON" -c 'import json,sys; print((json.load(sys.stdin).get("broker") or {}).get("python_executable") or "")' 2>/dev/null || true)"
      if [[ "$loaded_hash" == "$desired_hash" && "$loaded_python" == "$python" ]]; then
        echo "Comet Control broker supervised by $LABEL"
        return 0
      fi
    fi
    sleep 0.1
  done
  echo "Comet Control broker failed to start; see $LOG" >&2
  exit 2
}

case "$MODE" in
  probe) probe ;;
  start) start ;;
  *) echo "Usage: ensure-wip-broker.sh [probe|start] [--json]" >&2; exit 2 ;;
esac
