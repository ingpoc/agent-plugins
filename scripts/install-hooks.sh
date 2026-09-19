#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -d "$ROOT/.git" ]; then
  echo "install-hooks: $ROOT is not a git checkout" >&2
  exit 1
fi
mkdir -p "$ROOT/.git/hooks"
cp "$ROOT/.githooks/pre-commit" "$ROOT/.git/hooks/pre-commit"
chmod +x "$ROOT/.git/hooks/pre-commit" "$ROOT/.githooks/pre-commit" "$ROOT/scripts/check.py"
echo "installed $ROOT/.git/hooks/pre-commit -> scripts/check.py --fast"
