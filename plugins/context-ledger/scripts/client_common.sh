# Shared by install-client.sh, refresh-client.sh, and check-ledger-access.sh.
# A caller sets -euo pipefail before sourcing this file.

COMMON_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PACKAGE_ROOT=$(CDPATH= cd -- "$COMMON_DIR/.." && pwd)

read_json_field() {
  if [ ! -f "$1" ]; then
    return 0
  fi
  sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$1" | head -n 1
}

PACKAGE_VERSION=$(read_json_field "$PACKAGE_ROOT/plugin.json" version)
if [ -z "$PACKAGE_VERSION" ]; then
  printf '%s\n' "package version missing in plugin.json" >&2
  exit 1
fi

ver_gt() {
  a=$1
  b=$2
  if [ -z "$a" ]; then return 1; fi
  if [ -z "$b" ]; then return 0; fi
  while true; do
    case $a in
      *.*) aa=$(printf '%s\n' "$a" | cut -d. -f1); a=$(printf '%s\n' "$a" | cut -d. -f2-) ;;
      *) aa=$a; a= ;;
    esac
    case $b in
      *.*) bb=$(printf '%s\n' "$b" | cut -d. -f1); b=$(printf '%s\n' "$b" | cut -d. -f2-) ;;
      *) bb=$b; b= ;;
    esac
    case $aa in *[!0-9]*|"") aa=0 ;; esac
    case $bb in *[!0-9]*|"") bb=0 ;; esac
    if [ "$aa" -gt "$bb" ]; then return 0; fi
    if [ "$aa" -lt "$bb" ]; then return 1; fi
    if [ -z "$a" ] && [ -z "$b" ]; then return 1; fi
  done
}

version_at_least_package() {
  if [ -z "$1" ]; then return 1; fi
  if [ "$1" = "$PACKAGE_VERSION" ]; then return 0; fi
  ver_gt "$1" "$PACKAGE_VERSION"
}

install_root() {
  dir=$1
  [ -f "$dir/plugin.json" ] || return 1
  [ -x "$dir/bin/context-ledger-mcp" ] || return 1
  name=$(read_json_field "$dir/plugin.json" name)
  [ "$name" = "context-ledger" ]
}

version_ok() {
  got=$(read_json_field "$1/plugin.json" version)
  version_at_least_package "$got"
}

walk_plugin_json() {
  base=$1
  [ -d "$base" ] || return 0
  tmp=$(mktemp)
  find "$base" -name plugin.json -type f -print >"$tmp"
  while IFS= read -r pj || [ -n "$pj" ]; do
    [ -n "$pj" ] || continue
    dir=$(dirname "$pj")
    case "$dir" in
      */.cursor-plugin|*/.claude-plugin) continue ;;
      "$HOME/.cursor/plugins/marketplaces/"*) continue ;;
    esac
    if install_root "$dir"; then
      printf '%s\n' "$dir"
    fi
  done <"$tmp"
  rm -f "$tmp"
}

list_installs() {
  case "$1" in
    cursor)
      walk_plugin_json "$HOME/.cursor/plugins/local"
      walk_plugin_json "$HOME/.cursor/plugins/cache"
      ;;
    codex)
      walk_plugin_json "$HOME/.codex/plugins/cache"
      if install_root "$HOME/.agents/plugins/context-ledger"; then
        printf '%s\n' "$HOME/.agents/plugins/context-ledger"
      fi
      ;;
    grok)
      walk_plugin_json "$HOME/.grok/plugins"
      walk_plugin_json "$HOME/.grok/installed-plugins"
      walk_plugin_json "$HOME/.grok/marketplace-cache"
      ;;
    *)
      printf '%s\n' "unknown client: $1" >&2
      exit 2
      ;;
  esac
}

canonical_dir() {
  case "$1" in
    cursor) printf '%s\n' "$HOME/.cursor/plugins/local/context-ledger" ;;
    codex) printf '%s\n' "$HOME/.agents/plugins/context-ledger" ;;
    grok) printf '%s\n' "$HOME/.grok/plugins/context-ledger" ;;
    *)
      printf '%s\n' "unknown client: $1" >&2
      exit 2
      ;;
  esac
}

replace_install() {
  dest=$1
  case "$dest" in
    */.context-ledger|*/.context-ledger/*)
      printf '%s\n' "refusing to write the plugin into the ledger data root" >&2
      exit 1
      ;;
  esac
  if [ "$dest" = "$PACKAGE_ROOT" ]; then
    return 0
  fi
  mkdir -p "$dest"
  tar -C "$PACKAGE_ROOT" -cf - . | tar -C "$dest" -xf -
  got=$(read_json_field "$dest/plugin.json" version)
  if [ "$got" != "$PACKAGE_VERSION" ]; then
    printf '%s\n' "refresh failed: install is $got, package is $PACKAGE_VERSION" >&2
    exit 1
  fi
  if [ ! -x "$dest/bin/context-ledger-mcp" ]; then
    printf '%s\n' "refresh failed: launcher missing after copy" >&2
    exit 1
  fi
}

collect_installs() {
  INSTALLS=()
  while IFS= read -r dir || [ -n "$dir" ]; do
    [ -n "$dir" ] || continue
    INSTALLS+=("$dir")
  done < <(list_installs "$1")
}

assert_none_stale() {
  client=$1
  while IFS= read -r dir || [ -n "$dir" ]; do
    [ -n "$dir" ] || continue
    if ! version_ok "$dir"; then
      printf '%s\n' "refresh failed: stale install remains for $client" >&2
      exit 1
    fi
  done < <(list_installs "$client")
}

refresh_client() {
  client=$1
  case "$client" in
    cursor|codex|grok) ;;
    *)
      printf '%s\n' "unknown client: $client" >&2
      exit 2
      ;;
  esac
  collect_installs "$client"
  if [ "${#INSTALLS[@]}" -eq 0 ]; then
    printf '%s\n' "refresh failed: no context-ledger install for $client" >&2
    exit 1
  fi
  for dir in "${INSTALLS[@]}"; do
    if ! version_ok "$dir"; then
      replace_install "$dir"
    fi
  done
  assert_none_stale "$client"
  printf 'ok %s version=%s installs=%s\n' "$client" "$PACKAGE_VERSION" "${#INSTALLS[@]}"
}

install_client() {
  client=$1
  case "$client" in
    cursor|codex|grok) ;;
    *)
      printf '%s\n' "unknown client: $client" >&2
      exit 2
      ;;
  esac
  collect_installs "$client"
  if [ "${#INSTALLS[@]}" -eq 0 ]; then
    replace_install "$(canonical_dir "$client")"
  else
    for dir in "${INSTALLS[@]}"; do
      if ! version_ok "$dir"; then
        replace_install "$dir"
      fi
    done
  fi
  assert_none_stale "$client"
  collect_installs "$client"
  if [ "${#INSTALLS[@]}" -eq 0 ]; then
    printf '%s\n' "install failed: no context-ledger install for $client" >&2
    exit 1
  fi
  printf 'ok %s version=%s installs=%s\n' "$client" "$PACKAGE_VERSION" "${#INSTALLS[@]}"
}
