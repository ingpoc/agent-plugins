#!/usr/bin/env bash
# Point the user-scoped ingpoc-agent-plugins marketplace at one commit.
# Auto Refresh is a team GitHub-import setting (cursor.com/docs/plugins#keep-plugins-up-to-date).
# Staff workaround for a stale CLI index: marketplace remove, then add with no --git-ref
# (forum.cursor.com/t/169587). This script does not call marketplace update.
# Usage: MARKETPLACE_GIT_DIR=<bare-or-git-dir> refresh_cursor_marketplace_index.sh <full-sha>
set -euo pipefail

NAME="ingpoc-agent-plugins"
URL="https://github.com/ingpoc/agent-plugins"
AGENT_BIN="${AGENT_BIN:-agent}"
INDEX_ROOT="${MARKETPLACE_INDEX_ROOT:-$HOME/.cursor/plugins/marketplaces/github.com/ingpoc/agent-plugins}"
EXPECTED="${1:-}"
removed=0

if [[ ! "$EXPECTED" =~ ^[0-9a-f]{40}$ ]]; then
  echo "FAIL: usage: MARKETPLACE_GIT_DIR=DIR $0 <40-hex-sha>" >&2
  exit 2
fi
if [[ -z "${MARKETPLACE_GIT_DIR:-}" ]]; then
  echo "FAIL: MARKETPLACE_GIT_DIR is unset." >&2
  exit 2
fi
if ! command -v "$AGENT_BIN" >/dev/null 2>&1; then
  echo "FAIL: $AGENT_BIN is missing." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 is missing." >&2
  exit 1
fi

load_cursor_key() {
  if [[ -n "${CURSOR_API_KEY:-}" ]]; then
    return 0
  fi
  local key
  if ! key="$(security find-generic-password -s cursor.cli -a api_key -w 2>/dev/null)"; then
    echo "FAIL: login keychain has no cursor.cli api_key." >&2
    exit 1
  fi
  if [[ -z "$key" ]]; then
    echo "FAIL: cursor.cli api_key is empty." >&2
    exit 1
  fi
  export CURSOR_API_KEY="$key"
  unset key
}

sanitize() {
  python3 -c 'import os,sys; t=sys.stdin.read(); k=os.environ.get("CURSOR_API_KEY",""); sys.stdout.write(t.replace(k,"[redacted]") if k else t)'
}

run_agent() {
  local err out rc
  err="$(mktemp)"
  out="$(mktemp)"
  set +e
  "$AGENT_BIN" "$@" >"$out" 2>"$err"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    sanitize <"$err" >&2 || true
    rm -f "$err" "$out"
    return "$rc"
  fi
  cat "$out"
  rm -f "$err" "$out"
}

inspect() {
  local list_json parsed line
  list_json="$(mktemp)"
  parsed="$(mktemp)"
  if ! run_agent plugin marketplace list --format json >"$list_json"; then
    rm -f "$list_json" "$parsed"
    echo "FAIL: marketplace list failed." >&2
    exit 1
  fi
  if ! python3 - "$EXPECTED" "$list_json" "$MARKETPLACE_GIT_DIR" "$INDEX_ROOT" >"$parsed" <<'PY'
import json, subprocess, sys
from pathlib import Path

expected_sha, list_path, git_dir, index_root = sys.argv[1:5]
name = "ingpoc-agent-plugins"
target = "https://github.com/ingpoc/agent-plugins"

def emit(key, value):
    print(f"{key}={value}")

show = subprocess.run(
    ["git", "--git-dir", git_dir, "show", f"{expected_sha}:plugins/context-ledger/plugin.json"],
    capture_output=True,
    text=True,
)
if show.returncode != 0:
    sys.stderr.write(f"FAIL: plugins/context-ledger/plugin.json missing at {expected_sha}\n")
    sys.exit(1)
try:
    expected_version = json.loads(show.stdout)["version"]
except (json.JSONDecodeError, KeyError) as exc:
    sys.stderr.write(f"FAIL: plugin.json at {expected_sha} has no version ({exc})\n")
    sys.exit(1)
if not isinstance(expected_version, str) or not expected_version:
    sys.stderr.write(f"FAIL: plugin.json version at {expected_sha} is empty\n")
    sys.exit(1)

try:
    data = json.loads(Path(list_path).read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    sys.stderr.write(f"FAIL: marketplace list was not JSON ({exc})\n")
    sys.exit(1)
if not isinstance(data, list):
    sys.stderr.write("FAIL: marketplace list was not a JSON array\n")
    sys.exit(1)

def norm(url):
    url = (url or "").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url

rows = []
for item in data:
    if not isinstance(item, dict):
        continue
    if item.get("name") == name or norm(item.get("gitUrl")) == target:
        rows.append(item)
foreign = 0 if rows and all(item.get("name") == name and norm(item.get("gitUrl")) == target for item in rows) else (1 if rows else 0)
git_ref = ""
plugins = 0
version = ""
if len(rows) == 1 and foreign == 0:
    git_ref = str(rows[0].get("gitRef") or "")
    plugin_dir = Path(index_root) / git_ref / "plugins"
    found = sorted(plugin_dir.glob("*/plugin.json")) if plugin_dir.is_dir() else []
    plugins = len(found)
    ledger = plugin_dir / "context-ledger" / "plugin.json"
    if ledger.is_file():
        try:
            version = str(json.loads(ledger.read_text(encoding="utf-8")).get("version") or "")
        except json.JSONDecodeError:
            version = ""
emit("count", len(rows))
emit("git_ref", git_ref)
emit("plugins", plugins)
emit("version", version)
emit("expected", expected_version)
emit("foreign", foreign)
PY
  then
    rm -f "$list_json" "$parsed"
    echo "FAIL: could not compare the index to $EXPECTED" >&2
    exit 1
  fi
  count=""
  git_ref=""
  plugins=""
  version=""
  expected=""
  foreign=""
  while IFS= read -r line; do
    case "$line" in
      count=*) count="${line#count=}" ;;
      git_ref=*) git_ref="${line#git_ref=}" ;;
      plugins=*) plugins="${line#plugins=}" ;;
      version=*) version="${line#version=}" ;;
      expected=*) expected="${line#expected=}" ;;
      foreign=*) foreign="${line#foreign=}" ;;
      "") ;;
      *)
        echo "FAIL: bad inspect line." >&2
        rm -f "$list_json" "$parsed"
        exit 1
        ;;
    esac
  done <"$parsed"
  rm -f "$list_json" "$parsed"
  if [[ -z "$count" || -z "$foreign" || -z "$expected" ]]; then
    echo "FAIL: inspect did not return count, foreign, and expected version." >&2
    exit 1
  fi
}

matches() {
  [[ "$count" == "1" && "$foreign" == "0" && "$git_ref" == "$EXPECTED" && "$plugins" -gt 0 && -n "$version" && "$version" == "$expected" ]]
}

report_ok() {
  echo "OK index $git_ref context-ledger $version plugins $plugins registrations $count"
}

cleanup() {
  local status=$?
  if [[ "$removed" -eq 1 && "$status" -ne 0 ]]; then
    local n="0"
    n="$(
      run_agent plugin marketplace list --format json | python3 -c '
import json,sys
data=json.load(sys.stdin)
print(sum(1 for m in data if isinstance(m, dict) and m.get("name")=="ingpoc-agent-plugins"))
' 2>/dev/null || echo 0
    )"
    if [[ "$n" == "0" ]]; then
      echo "FAIL: zero ingpoc-agent-plugins registrations; adding the marketplace back." >&2
      run_agent plugin marketplace add "$URL" >/dev/null || echo "FAIL: restore add failed." >&2
    fi
  fi
}
trap cleanup EXIT

load_cursor_key
inspect
if matches; then
  report_ok
  exit 0
fi
if [[ "$count" != "1" || "$foreign" != "0" ]]; then
  echo "FAIL: expected exactly one $NAME registration, found count=$count foreign=$foreign." >&2
  exit 1
fi

echo "REINDEX $NAME $git_ref -> $EXPECTED" >&2
if ! run_agent plugin marketplace remove "$NAME" >/dev/null; then
  echo "FAIL: marketplace remove $NAME failed." >&2
  exit 1
fi
removed=1
if ! run_agent plugin marketplace add "$URL" >/dev/null; then
  echo "FAIL: marketplace add failed; trying the documented add once more." >&2
  if ! run_agent plugin marketplace add "$URL" >/dev/null; then
    echo "FAIL: marketplace add failed again." >&2
    exit 1
  fi
fi
inspect
if ! matches; then
  echo "FAIL: index gitRef=$git_ref version=$version plugins=$plugins registrations=$count; main $EXPECTED context-ledger $expected." >&2
  exit 1
fi
removed=0
report_ok
