#!/usr/bin/env bash
# Start the user's logged-in Comet profile. This script never addresses another browser.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMET="/Applications/Comet.app/Contents/MacOS/Comet"

[[ -x "$COMET" ]] || { echo "Comet not found: $COMET" >&2; exit 1; }
"$ROOT/scripts/ensure-wip-broker.sh" start
open -a "Comet"

for _ in {1..60}; do
  if "$ROOT/scripts/ensure-wip-broker.sh" probe --json >/dev/null 2>&1; then
    echo "Comet Control is ready in the logged-in Comet profile"
    exit 0
  fi
  sleep 0.5
done

echo "Comet opened, but its Comet Control extension did not connect." >&2
echo "Reload the unpacked extension from $ROOT/plugin/comet_control/extension in Comet, then retry." >&2
exit 2
