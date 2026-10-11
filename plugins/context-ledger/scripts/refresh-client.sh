#!/bin/bash
set -euo pipefail
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=client_common.sh
. "$HERE/client_common.sh"
if [ -z "${1:-}" ]; then
  printf '%s\n' "usage: refresh-client.sh cursor|codex|grok" >&2
  exit 2
fi
refresh_client "$1"
