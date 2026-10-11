#!/bin/bash
set -euo pipefail
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PACKAGE_ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
data=${CONTEXT_LEDGER_DATA:-$HOME/.context-ledger}
if [ "${1:-}" = "--data" ]; then
  if [ -z "${2:-}" ]; then
    printf '%s\n' '{"ok":false,"error":{"code":"INVALID_ARGUMENT","retryable":false,"message":"--data requires a directory"}}' >&2
    exit 1
  fi
  data=$2
fi
case "$data" in
  /*) ;;
  *)
    printf '%s\n' '{"ok":false,"error":{"code":"INVALID_ARGUMENT","retryable":false,"message":"ledger data root must be absolute"}}' >&2
    exit 1
    ;;
esac
if [ -e "$data" ] && [ ! -d "$data" ]; then
  printf '%s\n' '{"ok":false,"error":{"code":"LEDGER_UNBOUND","retryable":false,"message":"ledger data root is not a directory"}}' >&2
  exit 1
fi
if [ -f "$data/binding.json" ]; then
  CL_BINDING="$data/binding.json" python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

binding = Path(os.environ["CL_BINDING"])
try:
    payload = json.loads(binding.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    sys.stderr.write("binding.json is not readable JSON\n")
    sys.exit(1)
ledger = payload.get("ledger_dir") if isinstance(payload, dict) else None
if not isinstance(ledger, str) or not ledger.startswith("/"):
    sys.stderr.write("ledger_dir is not an absolute path\n")
    sys.exit(1)
db = Path(ledger) / "ledger.sqlite3"
if db.is_symlink() or not db.is_file():
    sys.stderr.write(f"ledger unreachable at {db}\n")
    sys.exit(1)
try:
    with db.open("rb") as handle:
        handle.read(1)
except OSError:
    sys.stderr.write(f"ledger unreachable at {db}\n")
    sys.exit(1)
sys.stdout.write("ok local\n")
PY
  exit 0
fi
if [ -z "${CONTEXT_LEDGER_MCP_TOKEN:-}" ]; then
  printf '%s\n' '{"ok":false,"error":{"code":"LEDGER_UNBOUND","retryable":false,"message":"no binding and CONTEXT_LEDGER_MCP_TOKEN is unset"}}' >&2
  exit 1
fi
CL_PACKAGE_ROOT="$PACKAGE_ROOT" python3 - <<'PY'
import importlib.util
import os
import sys

root = os.environ["CL_PACKAGE_ROOT"]
path = os.path.join(root, "bin", "remote_bridge.py")
spec = importlib.util.spec_from_file_location("context_ledger_remote_bridge", path)
if spec is None or spec.loader is None:
    sys.stderr.write("remote bridge missing\n")
    sys.exit(1)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
url = module._url()
if url is None:
    sys.exit(2)
timeout = module._timeout()
if timeout is None:
    sys.exit(2)
token = os.environ.get("CONTEXT_LEDGER_MCP_TOKEN", "")
failure = module._probe_ledger(url, token, timeout, {})
if failure is not None:
    failure = module._probe_ledger(url, token, timeout, {})
if failure is not None:
    sys.stderr.write(failure + "\n")
    sys.exit(1)
sys.stdout.write("ok remote\n")
PY
