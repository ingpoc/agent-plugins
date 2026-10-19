#!/bin/sh
# Set this plugin's version everywhere it is pinned, and prove nothing was left behind.
#
#   scripts/bump-version.sh 0.1.33
#
# Only files that must equal the current version are rewritten. Historical release headings
# (CHANGELOG.md, README.md) are never touched: add the new section by hand. Exits nonzero if
# the old version survives in a pinned file, or if a pinned file did not receive the new one.
set -eu

usage() { echo "usage: $0 <new-version>" >&2; exit 2; }

[ $# -eq 1 ] || usage
NEW=$1
case "$NEW" in
  *[!0-9.]*|.*|*.) usage ;;
esac
case "$NEW" in
  *.*.*) ;;
  *) usage ;;
esac

PLUGIN=$(cd "$(dirname "$0")/.." && pwd)
OLD=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLUGIN/plugin.json" | head -n 1)
[ -n "$OLD" ] || { echo "bump: cannot read the current version from plugin.json" >&2; exit 2; }
[ "$OLD" != "$NEW" ] || { echo "bump: plugin.json is already $NEW" >&2; exit 2; }

FILES="plugin.json .cursor-plugin/plugin.json .claude-plugin/plugin.json
context_ledger/__init__.py mcp.json .cursor-plugin/mcp.json
ui-src/app.js ui/ledger-v2.html tests/test_contract.py tests/test_manifests.py"

esc=$(printf '%s' "$OLD" | sed 's/\./\\./g')

for f in $FILES; do
  [ -f "$PLUGIN/$f" ] || { echo "bump: pinned file missing: $f" >&2; exit 2; }
  tmp="$PLUGIN/$f.bump.$$"
  sed "s/$esc/$NEW/g" "$PLUGIN/$f" > "$tmp"
  mv "$tmp" "$PLUGIN/$f"
done

bad=""
for f in $FILES; do
  if grep -q -- "$OLD" "$PLUGIN/$f"; then bad="$bad $f(old)"; fi
  if ! grep -q -- "$NEW" "$PLUGIN/$f"; then bad="$bad $f(new-missing)"; fi
done
[ -z "$bad" ] || { echo "bump: incomplete:$bad" >&2; exit 1; }

echo "bump: $OLD -> $NEW across $(printf '%s\n' $FILES | wc -l | tr -d ' ') pinned files"
echo "next: python3 scripts/check.py, then add the CHANGELOG.md and README.md sections"
