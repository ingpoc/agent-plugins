#!/bin/sh
# Frozen retrieval eval for one ledger: the BEFORE/AFTER gate for find and select quality.
#
#   scripts/bench.sh <case-file.json> [--label BEFORE|AFTER|...]
#
# Read-only. `retrieval_eval.py` opens the ledger `mode=ro` with `query_only=ON`; nothing is
# written to the ledger, so a bench run never disturbs an A/B window.
#
# Aggregates only. The runnable case file holds real decision ids and query text, so it lives
# outside this repo (mode 0600) and is passed in. Its path is not baked in here.
set -eu

usage() { echo "usage: $0 <case-file.json> [--label LABEL]" >&2; exit 2; }

[ $# -ge 1 ] || usage
CASE=$1
shift
LABEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --label)
      [ $# -ge 2 ] || usage
      LABEL=$2
      shift 2
      ;;
    *) usage ;;
  esac
done

PLUGIN=$(cd "$(dirname "$0")/.." && pwd)
DATA=${CONTEXT_LEDGER_DATA:-$HOME/.context-ledger}
SCOPE=${CL_BENCH_SCOPE:-agent-plugins}
DB="$DATA/ledger/ledger.sqlite3"

[ -f "$CASE" ] || { echo "bench: case file not found: $CASE" >&2; exit 2; }
[ -f "$DB" ] || { echo "bench: ledger not found: $DB (set CONTEXT_LEDGER_DATA)" >&2; exit 2; }

if [ -n "$LABEL" ]; then
  exec python3 "$PLUGIN/scripts/retrieval_eval.py" \
    --db "$DB" --scope "$SCOPE" --fixture "$CASE" --plugin "$PLUGIN" --label "$LABEL"
fi
exec python3 "$PLUGIN/scripts/retrieval_eval.py" \
  --db "$DB" --scope "$SCOPE" --fixture "$CASE" --plugin "$PLUGIN"
