#!/usr/bin/env bash
# Point this clone (and all its worktrees) at the tracked hooks in .githooks/.
# Each worktree runs its own checkout's .githooks/pre-push.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "install-hooks: $ROOT is not a git checkout" >&2
  exit 1
fi
git -C "$ROOT" config core.hooksPath .githooks
chmod +x "$ROOT/.githooks/pre-push" "$ROOT/scripts/prepush.py" "$ROOT/scripts/check.py"
echo "core.hooksPath=.githooks: pre-push runs scripts/prepush.py (format, lint, compile, manifest contract)"
