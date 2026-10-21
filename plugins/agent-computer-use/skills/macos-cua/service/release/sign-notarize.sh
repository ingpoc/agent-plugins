#!/bin/bash
# sign-notarize.sh — Developer ID sign (hardened runtime), notarize, staple and
# Gatekeeper-assess a macOS .app bundle, inside-out.
#
# Usage: sign-notarize.sh [options] /path/to/App.app
#   --identity NAME     codesign identity (default: $SIGN_IDENTITY or
#                       "Developer ID Application: Gurusharan Gupta (9UPQL479Z5)")
#   --profile NAME      notarytool keychain profile (default: $NOTARY_PROFILE or acu-notary)
#   --entitlements DIR  dir of <name>.entitlements files (default: <script dir>/../Resources)
#                       <name> = bundle name without extension, its CFBundleExecutable, or bare
#                       executable name ("Agent Computer Use.app" -> CUAService.entitlements).
#                       Items without a matching file are signed with no entitlements.
#   --work DIR          scratch dir for zips/logs (default: ~/.cache/macos-cua/release-build)
#   --sign-only         stop after signing + verification (no notarization)
#   --force             re-sign/re-notarize even if the app already passes all checks
#
# Release step of install_service.py (--release): sign a STAGED build here, then install it.
# Never prints secrets: notary credentials come only from the keychain profile.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IDENTITY="${SIGN_IDENTITY:-Developer ID Application: Gurusharan Gupta (9UPQL479Z5)}"
PROFILE="${NOTARY_PROFILE:-acu-notary}"
ENT_DIR="$SCRIPT_DIR/../Resources"
WORK="${ACU_RELEASE_WORK:-$HOME/.cache/macos-cua/release-build}"
SIGN_ONLY=0
FORCE=0
CODESIGN_TIMEOUT=180   # seconds; a hang usually means a Keychain access prompt

die()  { echo "ERROR: $*" >&2; exit 1; }
log()  { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --identity) IDENTITY="${2:?--identity needs a value}"; shift 2;;
    --profile) PROFILE="${2:?--profile needs a value}"; shift 2;;
    --entitlements) ENT_DIR="${2:?--entitlements needs a value}"; shift 2;;
    --work) WORK="${2:?--work needs a value}"; shift 2;;
    --sign-only) SIGN_ONLY=1; shift;;
    --force) FORCE=1; shift;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    -*) die "unknown option: $1";;
    *) [ -z "${APP:-}" ] || die "only one app path allowed"; APP="$1"; shift;;
  esac
done
[ -n "${APP:-}" ] || die "usage: $(basename "$0") [options] /path/to/App.app"
[ -d "$APP" ] && [ -f "$APP/Contents/Info.plist" ] || die "not an app bundle: $APP"
APP="$(cd "$APP" && pwd -P)"
case "$APP" in */.cache/macos-cua/CUAService.app|"/Applications/Agent Computer Use.app") die "refusing to modify the live install ($APP); sign a staged copy first";; esac
APPNAME="$(basename "$APP" .app)"
mkdir -p "$WORK"

# Run a command with a watchdog (macOS has no coreutils timeout).
with_timeout() { local t="$1"; shift; /usr/bin/perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$t" "$@"; }

# ---------- preflight ----------
for t in codesign xcrun ditto plutil spctl /usr/libexec/PlistBuddy; do command -v "$t" >/dev/null || die "missing tool: $t"; done
IDS="$(security find-identity -v -p codesigning)"; grep -Fq "\"$IDENTITY\"" <<<"$IDS" \
  || die "signing identity not found/valid in keychain: $IDENTITY"
[ -d "$ENT_DIR" ] || die "entitlements dir missing: $ENT_DIR"
for f in "$ENT_DIR"/*.entitlements; do
  [ -e "$f" ] || continue
  plutil -lint -s "$f" || die "invalid plist: $f"
  for bad in com.apple.security.get-task-allow com.apple.security.cs.disable-library-validation; do
    if /usr/libexec/PlistBuddy -c "Print :$bad" "$f" >/dev/null 2>&1; then
      die "$f contains $bad — not allowed for a notarized release (remove it or justify and edit this check)"
    fi
  done
done

TEAM="$(printf '%s' "$IDENTITY" | sed -nE 's/.*\(([A-Z0-9]{10})\)$/\1/p')"
[ -n "$TEAM" ] || die "cannot parse team id from identity: $IDENTITY"

ent_for() { # $1 = name; prints entitlements path or nothing
  local f="$ENT_DIR/$1.entitlements"; [ -f "$f" ] && printf '%s' "$f" || true
}
ent_for_bundle() { # $1 = bundle path; by bundle name, then by CFBundleExecutable
  local b="$1" n e exe
  n="$(basename "$b")"; e="$(ent_for "${n%.*}")"
  if [ -z "$e" ] && [ -f "$b/Contents/Info.plist" ]; then
    exe="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$b/Contents/Info.plist" 2>/dev/null || true)"
    [ -n "$exe" ] && e="$(ent_for "$exe")"
  fi
  printf '%s' "$e"
}

already_good() {
  codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || return 1
  local i; i="$(codesign -dvv "$APP" 2>&1)"; grep -Fq "Authority=$IDENTITY" <<<"$i" || return 1
  [ "$SIGN_ONLY" = 1 ] && return 0
  xcrun stapler validate "$APP" >/dev/null 2>&1 || return 1
  i="$(spctl -a -vvv -t exec "$APP" 2>&1)" || return 1; grep -q "source=Notarized Developer ID" <<<"$i" || return 1
}
if [ "$FORCE" = 0 ] && already_good; then
  log "$APP already signed with '$IDENTITY'$([ "$SIGN_ONLY" = 1 ] || echo ', notarized and stapled'); nothing to do (use --force to redo)"
  spctl -a -vvv -t exec "$APP" 2>&1 || true
  exit 0
fi

# ---------- step 2: usage strings ----------
ensure_key() { # plist key value
  if ! /usr/libexec/PlistBuddy -c "Print :$2" "$1" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Add :$2 string $3" "$1"
    log "added $2 to ${1#$APP/}"
  fi
}
# Every bundle (outer app + nested apps) whose entitlements request a TCC
# resource must carry the matching usage description.
while IFS= read -r plist; do
  b="$(dirname "$(dirname "$plist")")"; n="$(basename "$b")"; n="${n%.*}"
  e="$(ent_for_bundle "$b")"; [ -n "$e" ] || continue
  if /usr/libexec/PlistBuddy -c "Print :com.apple.security.automation.apple-events" "$e" >/dev/null 2>&1; then
    ensure_key "$plist" NSAppleEventsUsageDescription "$n controls apps such as System Events, Finder and Comet so the agent can carry out tasks you ask for."
  fi
  if /usr/libexec/PlistBuddy -c "Print :com.apple.security.device.audio-input" "$e" >/dev/null 2>&1; then
    ensure_key "$plist" NSMicrophoneUsageDescription "$n listens for spoken tasks when you turn voice on."
  fi
  if /usr/libexec/PlistBuddy -c "Print :com.apple.security.personal-information.calendars" "$e" >/dev/null 2>&1; then
    ensure_key "$plist" NSCalendarsFullAccessUsageDescription "$n reads your next calendar event (read-only)."
    ensure_key "$plist" NSCalendarsUsageDescription "$n reads your next calendar event (read-only)."
  fi
done < <(find "$APP" -path '*/Contents/Info.plist' -type f | sort)

# ---------- step 3: sign inside-out ----------
log "clearing extended attributes"
xattr -cr "$APP"

depth_sort() { awk -F/ '{print NF "\t" $0}' | sort -t$'\t' -k1,1nr -k2,2 | cut -f2-; }

BUNDLES="$(find "$APP" -depth -type d \( -name '*.app' -o -name '*.framework' -o -name '*.xpc' -o -name '*.appex' -o -name '*.bundle' -o -name '*.plugin' \) | depth_sort)"
# main executables of bundles are signed as part of their bundle
MAIN_EXES=""
while IFS= read -r b; do
  [ -n "$b" ] || continue
  ip="$b/Contents/Info.plist"; [ -f "$ip" ] || ip="$b/Resources/Info.plist"; [ -f "$ip" ] || ip="$b/Versions/A/Resources/Info.plist"
  [ -f "$ip" ] || continue
  exe="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$ip" 2>/dev/null || true)"; [ -n "$exe" ] || continue
  for c in "$b/Contents/MacOS/$exe" "$b/Versions/A/$exe" "$b/$exe"; do
    [ -f "$c" ] && [ ! -L "$c" ] && MAIN_EXES+="$(cd "$(dirname "$c")" && pwd -P)/$exe"$'\n'
  done
done <<< "$BUNDLES"

do_sign() { # path [entitlements]
  local args=(--force --options runtime --timestamp --sign "$IDENTITY")
  [ -n "${2:-}" ] && args+=(--entitlements "$2")
  local rc=0
  with_timeout "$CODESIGN_TIMEOUT" codesign "${args[@]}" "$1" 2>"$WORK/codesign.err" || rc=$?
  if [ "$rc" != 0 ]; then
    cat "$WORK/codesign.err" >&2
    if [ "$rc" = 142 ]; then
      die "codesign timed out on ${1#$APP/} — probably a Keychain access prompt is waiting. Click 'Always Allow' for codesign, then re-run."
    fi
    die "codesign failed on ${1#$APP/}"
  fi
  echo "   signed ${1#$APP/}${2:+  [entitlements: $(basename "$2")]}"
}

log "signing nested Mach-O files (deepest first)"
SIGNED_COUNT=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$(file -b "$f")" in *Mach-O*) ;; *) continue;; esac
  real="$(cd "$(dirname "$f")" && pwd -P)/$(basename "$f")"
  grep -Fxq "$real" <<<"$MAIN_EXES" && continue
  do_sign "$f" "$(ent_for "$(basename "$f")")"; SIGNED_COUNT=$((SIGNED_COUNT+1))
done < <(find "$APP/Contents" -type f | depth_sort)

log "signing nested bundles (deepest first)"
while IFS= read -r b; do
  [ -n "$b" ] && [ "$b" != "$APP" ] || continue
  do_sign "$b" "$(ent_for_bundle "$b")"; SIGNED_COUNT=$((SIGNED_COUNT+1))
done <<< "$BUNDLES"

log "signing app"
do_sign "$APP" "$(ent_for_bundle "$APP")"
log "signed $SIGNED_COUNT nested items + app"

log "verifying"
codesign --verify --deep --strict --verbose=2 "$APP" || die "codesign verification failed"
# every Mach-O must carry hardened runtime + our team + a secure timestamp
bad=0
while IFS= read -r f; do
  case "$(file -b "$f")" in *Mach-O*) ;; *) continue;; esac
  info="$(codesign -dvv "$f" 2>&1)" || { echo "unsigned: ${f#$APP/}" >&2; bad=1; continue; }
  grep -q 'flags=.*runtime' <<<"$info" || { echo "no hardened runtime: ${f#$APP/}" >&2; bad=1; }
  grep -q "TeamIdentifier=$TEAM" <<<"$info" || { echo "wrong team: ${f#$APP/}" >&2; bad=1; }
  grep -q '^Timestamp=' <<<"$info" || { echo "no secure timestamp: ${f#$APP/}" >&2; bad=1; }
  ents="$(codesign -d --entitlements - --xml "$f" 2>/dev/null || true)"; if grep -q get-task-allow <<<"$ents"; then echo "get-task-allow present: ${f#$APP/}" >&2; bad=1; fi
done < <(find "$APP" -type f | sort)
[ "$bad" = 0 ] || die "post-sign audit failed"
echo "designated requirement:"; codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => /  /p'
echo "entitlements (app):"; codesign -d --entitlements - "$APP" 2>/dev/null | sed 's/^/  /'

[ "$SIGN_ONLY" = 1 ] && { log "--sign-only: done"; exit 0; }

# ---------- step 4: notarize, staple, assess ----------
STAMP="$(date +%Y%m%d-%H%M%S)"
ZIP="$WORK/$APPNAME-$STAMP.zip"
RES="$WORK/notary-$STAMP.json"
log "zipping -> $ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
log "submitting to notary service (profile: $PROFILE) — this can take several minutes"
set +e
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait --output-format json > "$RES" 2>"$WORK/notary-$STAMP.err"
rc=$?
set -e
SUB_ID="$(plutil -extract id raw -o - "$RES" 2>/dev/null || true)"
STATUS="$(plutil -extract status raw -o - "$RES" 2>/dev/null || true)"
echo "submission id: ${SUB_ID:-?}  status: ${STATUS:-?}"
if [ "$STATUS" != "Accepted" ]; then
  cat "$WORK/notary-$STAMP.err" >&2
  if [ -n "$SUB_ID" ]; then
    xcrun notarytool log "$SUB_ID" --keychain-profile "$PROFILE" "$WORK/notary-$STAMP-log.json" >/dev/null 2>&1 || true
    [ -f "$WORK/notary-$STAMP-log.json" ] && { echo "notary log ($WORK/notary-$STAMP-log.json):" >&2; cat "$WORK/notary-$STAMP-log.json" >&2; }
  fi
  die "notarization not accepted (rc=$rc); zip kept at $ZIP for inspection"
fi
xcrun notarytool log "$SUB_ID" --keychain-profile "$PROFILE" "$WORK/notary-$STAMP-log.json" >/dev/null 2>&1 || true

log "stapling"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
log "Gatekeeper assessment"
SPCTL="$(spctl -a -vvv -t exec "$APP" 2>&1)" || { echo "$SPCTL" >&2; die "spctl rejected the app"; }
echo "$SPCTL"
grep -q ': accepted' <<<"$SPCTL" && grep -q 'source=Notarized Developer ID' <<<"$SPCTL" \
  || die "spctl did not report 'accepted' / 'Notarized Developer ID'"

mkdir -p "$HOME/.Trash"
mv "$ZIP" "$HOME/.Trash/$(basename "$ZIP")"
log "moved $(basename "$ZIP") to ~/.Trash"
log "SUCCESS: $APP (submission $SUB_ID)"
