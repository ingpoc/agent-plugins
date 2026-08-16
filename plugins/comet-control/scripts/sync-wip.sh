#!/usr/bin/env bash
# Sync WIP plugin source → WIP deploy only. Never touches Codex live paths.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/plugin/comet_control"
DEPLOY="$ROOT/deploy"

# Guardrails
for forbidden in \
  "$HOME/.codex/plugin/comet_control" \
  "$HOME/.codex/skills/comet-control"
do
  if [[ "$SRC" -ef "$forbidden" ]] || [[ "$DEPLOY" -ef "$forbidden" ]]; then
    echo "✗ Refusing sync: path aliases Codex live tree ($forbidden)"
    exit 1
  fi
done

mkdir -p "$DEPLOY/extension" "$DEPLOY/native" "$ROOT/run"
rsync -a --delete --exclude='*.Zone.Identifier' "$SRC/extension/" "$DEPLOY/extension/"
rsync -a --delete --exclude='*.Zone.Identifier' "$SRC/native/" "$DEPLOY/native/"
chmod +x "$DEPLOY/native/broker.py" 2>/dev/null || true

echo "✓ WIP deploy updated:"
echo "  extension: $DEPLOY/extension"
echo "  broker:    $DEPLOY/native/broker.py"
echo "Launch the attested logged-in Comet profile with: $ROOT/scripts/launch-wip-comet.sh"
echo "Install Comet Control only in Comet."
echo "Do not run Codex sync.sh against this tree."
