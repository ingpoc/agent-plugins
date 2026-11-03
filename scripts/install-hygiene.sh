#!/usr/bin/env bash
# Install the machine hygiene tool from this repo into ~/.agents/bin.
#
#   scripts/hygiene.py      -> ~/.agents/bin/hygiene.py      (the `hygiene` CLI)
#   scripts/hygiene_map.py  -> ~/.agents/bin/hygiene_map.py  (map page, imported by it)
#   ~/.local/bin/hygiene    -> symlink to ~/.agents/bin/hygiene.py (created if missing)
#
# An installed hygiene.py that differs from the repo copy is first kept as
# hygiene.py.bak-YYYYMMDD (never overwritten, never deleted). Idempotent.
# Override the target with AGENTS_BIN=/path for tests.
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="${AGENTS_BIN:-$HOME/.agents/bin}"
link_dir="${HYGIENE_LINK_DIR:-$HOME/.local/bin}"
stamp="$(date +%Y%m%d)"
mkdir -p "$bin"

for name in hygiene.py hygiene_map.py; do
  if [[ -f "$bin/$name" ]] && ! cmp -s "$src/$name" "$bin/$name"; then
    backup="$bin/$name.bak-$stamp"
    if [[ ! -e "$backup" ]]; then
      cp -p "$bin/$name" "$backup"
      echo "backed up $bin/$name -> $backup"
    fi
  fi
  tmp="$(mktemp "$bin/.$name.XXXXXX")"
  cp "$src/$name" "$tmp"
  chmod 755 "$tmp"
  mv -f "$tmp" "$bin/$name"
  echo "installed $bin/$name"
done

mkdir -p "$link_dir"
if [[ ! -e "$link_dir/hygiene" && ! -L "$link_dir/hygiene" ]]; then
  ln -s "$bin/hygiene.py" "$link_dir/hygiene"
  echo "linked $link_dir/hygiene -> $bin/hygiene.py"
fi
python3 "$bin/hygiene.py" --help >/dev/null
echo "hygiene: ok ($(cd "$src/.." && git rev-parse --short HEAD 2>/dev/null || echo unknown))"
