#!/bin/bash
# Signed runtime installer; contract: docs/install-mac-SPEC.md.
# Verification guards deliberately fail if any conjunct fails.
# shellcheck disable=SC2015
set -u
set -o pipefail
readonly REPO=ingpoc/agent-plugins
readonly TEAM_ID=9UPQL479Z5
readonly MANIFEST="https://github.com/$REPO/releases/latest/download/manifest.json"
phase=${1:-install}
[ "$#" -eq 0 ] || shift
dry=0; allow_quit=0; requested=""; parse_error=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry=1 ;;
        --allow-quit) allow_quit=1 ;;
        --plugins) requested=pending ;;
        *) if [ "$requested" = pending ]; then requested=$arg; else parse_error="Unknown argument: $arg"; fi ;;
    esac
done
case "$phase" in install|finish|remove) ;; *) parse_error="Unknown phase: $phase" ;; esac
cache="$HOME/Library/Caches/agent-plugins"
logfile="$HOME/Library/Logs/agent-plugins/install.log"
appdir=/Applications
if ! id -Gn | grep -qw admin; then appdir="$HOME/Applications"; fi
manifest_url=$MANIFEST
if [ "${AGENT_PLUGINS_TEST:-0}" = 1 ]; then
    cache=${AGENT_PLUGINS_CACHE_DIR:-$cache}
    appdir=${AGENT_PLUGINS_APP_DIR:-$appdir}
    logfile=${AGENT_PLUGINS_LOG:-$logfile}
    manifest_url=${AGENT_PLUGINS_MANIFEST_URL:-$MANIFEST}
fi
mkdir -p "$cache" "$(dirname "$logfile")" || exit 13
: > "$logfile"
log() { printf '%s\n' "$*" | tee -a "$logfile" >&2; }
# JXA supplies JSON parsing/escaping without third-party tools.
json() {
    /usr/bin/osascript -l JavaScript - "$@" <<'JXA'
ObjC.import('Foundation');
function run(a) {
 if(a[0]==='quote') return JSON.stringify(a[1]);
 var s=$.NSString.stringWithContentsOfFileEncodingError(a[1],$.NSUTF8StringEncoding,null);
 if(!s) throw Error('Cannot read JSON');
 var v=JSON.parse(ObjC.unwrap(s));
 if(a[0]==='valid') return 'ok';
 for(var k of a[2].split('.')) { if(v===null || v[k]===undefined) throw Error('Missing '+k); v=v[k]; }
 return typeof v==='object' ? JSON.stringify(v) : String(v);
}
JXA
}
quote() { json quote "$1"; }
harnesses=''; proof=''; permissions='{}'; unlock=false
version=''; build=''; app=''; runtime_action=skip
mountpoint=''; download=''; scratch=$(mktemp -d "$cache/run.XXXXXX") || exit 13
# ShellCheck cannot see the EXIT-trap invocation.
# shellcheck disable=SC2317
cleanup() {
    if [ -n "$mountpoint" ]; then hdiutil detach "$mountpoint" >> "$logfile" 2>&1 || return; fi
    /bin/rm -r "$scratch"
}
trap cleanup EXIT
summary() {
    printf '{"phase":%s,"harnesses":[%s],"runtime":{"version":%s,"build":%s,"path":%s,"action":%s},"permissions":%s,"proof":[%s],"unlock_opened":%s}\n' \
        "$(quote "$phase")" "$harnesses" "$(quote "$version")" "$(quote "$build")" "$(quote "$app")" "$(quote "$runtime_action")" "$permissions" "$proof" "$unlock"
}
fail() { log "$2"; runtime_action=failed; summary; exit "$1"; }
trash_download() {
    if [ -n "$mountpoint" ]; then hdiutil detach "$mountpoint" >> "$logfile" 2>&1 || true; mountpoint=''; fi
    if [ -f "$download" ]; then
        mkdir -p "$HOME/.Trash"
        mv "$download" "$HOME/.Trash/$(basename "$download").$(date +%s).$$" || true
    fi
}
verify_fail() { trash_download; fail 10 "$1"; }
fetch() {
    case "$1" in
        https://*) curl -fsSL --connect-timeout 15 --max-time 300 --proto '=https' --proto-redir '=https' "$1" -o "$2" >> "$logfile" 2>&1 ;;
        file://*) [ "${AGENT_PLUGINS_TEST:-0}" = 1 ] && cp "${1#file://}" "$2" ;;
        *) return 1 ;;
    esac
}
plist() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null; }
find_app() {
    local candidate
    if [ "${AGENT_PLUGINS_TEST:-0}" = 1 ] && [ -n "${AGENT_PLUGINS_APP_DIR:-}" ]; then
        candidate="$appdir/Agent Computer Use.app"
        if [ -d "$candidate" ] && [ "$(plist "$candidate" CFBundleIdentifier)" = "$bundle" ]; then printf '%s\n' "$candidate"; fi
        return
    fi
    while IFS= read -r candidate; do
        if [ -d "$candidate" ] && [ "$(plist "$candidate" CFBundleIdentifier)" = "$bundle" ]; then printf '%s\n' "$candidate"; return; fi
    done < <( { printf '%s\n' "$appdir/Agent Computer Use.app" '/Applications/Agent Computer Use.app' "$HOME/Applications/Agent Computer Use.app"; mdfind "kMDItemCFBundleIdentifier == '$bundle'"; } 2>/dev/null)
}
quit_app() {
    if pgrep -x CUAService >/dev/null; then
        [ "$allow_quit" = 1 ] || fail 11 'CUAService is running; --allow-quit is required.'
        osascript -e "tell application id \"$bundle\" to quit" >> "$logfile" 2>&1 || fail 11 'Could not quit CUAService.'
        local n=0
        while pgrep -x CUAService >/dev/null && [ "$n" -lt 20 ]; do sleep 1; n=$((n+1)); done
        pgrep -x CUAService >/dev/null && fail 11 'CUAService did not quit.'
    fi
    return 0
}
[ -z "$parse_error" ] || fail 13 "$parse_error"
[ "$requested" != pending ] || fail 13 '--plugins requires a comma-separated value.'
# Detect CLIs and configuration directories, including GUI-only installations.
names=''
for name in codex cursor claude grok; do
    cli=$name; config="$HOME/.$name"
    if [ "$name" = cursor ]; then
        cli=agent; command -v agent >/dev/null || cli=cursor-agent
    fi
    if command -v "$cli" >/dev/null || [ -d "$config" ]; then names="$names $name"; fi
done
plugins=agent-computer-use
[ ! -d /Applications/Comet.app ] || plugins="$plugins comet-control"
case ",$requested," in *,context-ledger,*) plugins="$plugins context-ledger" ;; esac
if [ -n "$requested" ] && [ "$requested" != pending ]; then
    for p in ${requested//,/ }; do
        case "$p" in agent-computer-use|comet-control|context-ledger) ;; *) fail 13 "Unknown plugin: $p" ;; esac
    done
fi
bundle=com.ingpoc.cua-service
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
if [ "$phase" = install ]; then
    [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || fail 12 'Requires Apple Silicon macOS.'
    fetch "$manifest_url" "$scratch/manifest.json" || fail 13 'Manifest download failed.'
    fetch "${manifest_url%/*}/SHA256SUMS" "$scratch/SHA256SUMS" || fail 13 'Checksum list download failed.'
    expected=$(awk '$2 == "manifest.json" {print $1}' "$scratch/SHA256SUMS")
    actual=$(shasum -a 256 "$scratch/manifest.json" | awk '{print $1}')
    [ "$expected" = "$actual" ] || fail 10 'Manifest checksum mismatch.'
    [ "$(json get "$scratch/manifest.json" schema 2>/dev/null)" = 1 ] && \
        [ "$(json get "$scratch/manifest.json" repo 2>/dev/null)" = "$REPO" ] || fail 13 'Invalid manifest schema/repository.'
    field() { json get "$scratch/manifest.json" "plugins.agent-computer-use.$1" 2>/dev/null; }
    version=$(field version) && build=$(field build) && bundle=$(field bundleId) && \
        minos=$(field minMacOS) && url=$(field url) && sha=$(field sha256) || fail 13 'Incomplete manifest.'
    # Reject identifiers and versions before using them in queries or paths.
    [[ "$bundle" =~ ^[A-Za-z0-9.-]+$ ]] && [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && \
        [[ "$build" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] && [[ "$minos" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] && \
        [[ "$sha" =~ ^[a-fA-F0-9]{64}$ ]] || fail 13 'Invalid manifest metadata.'
    current=$(sw_vers -productVersion)
    oldest=$(printf '%s\n%s\n' "$minos" "$current" | sort -t . -k1,1n -k2,2n -k3,3n | head -1)
    [ "$oldest" = "$minos" ] || fail 12 "Requires macOS $minos."
    app=$(find_app)
    if [ -n "$app" ] && [ "$(plist "$app" CFBundleShortVersionString)" = "$version" ] && [ "$(plist "$app" CFBundleVersion)" = "$build" ]; then
        log 'Runtime is up to date.'
    else
        filename=${url##*/}
        [[ "$filename" =~ ^AgentComputerUse-[A-Za-z0-9.-]+\.dmg$ ]] || fail 13 'Invalid DMG filename.'
        case "$url" in
            "https://github.com/$REPO/releases/download/agent-computer-use-v$version/$filename") ;;
            file://*) [ "${AGENT_PLUGINS_TEST:-0}" = 1 ] || fail 13 'Invalid asset URL.' ;;
            *) fail 13 'Asset URL is outside the pinned repository/tag.' ;;
        esac
        expected=$(awk -v name="$filename" '$2 == name {print $1}' "$scratch/SHA256SUMS")
        [ "$expected" = "$sha" ] || fail 10 'Manifest and checksum list disagree.'
        # Local test bundles retain production URLs; resolve their DMG beside the manifest.
        source_url=$url
        case "$manifest_url" in file://*) source_url="${manifest_url%/*}/$filename" ;; esac
        download="$cache/$filename"
        fetch "$source_url" "$download" || fail 13 'DMG download failed.'
        [ "$(shasum -a 256 "$download" | awk '{print $1}')" = "$sha" ] || verify_fail 'DMG checksum mismatch.'
        spctl -a -t open --context context:primary-signature -vv "$download" >> "$logfile" 2>&1 || verify_fail 'DMG Gatekeeper verification failed.'
        mountpoint="$scratch/mount"; mkdir "$mountpoint"
        hdiutil attach -nobrowse -readonly -mountpoint "$mountpoint" "$download" >> "$logfile" 2>&1 || verify_fail 'DMG mount failed.'
        source_app=''
        for candidate in "$mountpoint"/*.app; do
            [ -d "$candidate" ] || continue
            [ -z "$source_app" ] || verify_fail 'DMG has multiple apps.'
            source_app=$candidate
        done
        [ -n "$source_app" ] || verify_fail 'DMG has no app.'
        codesign --verify --deep --strict "$source_app" >> "$logfile" 2>&1 || verify_fail 'App signature failed.'
        team=$(codesign -dv --verbose=4 "$source_app" 2>&1 | sed -n 's/^TeamIdentifier=//p')
        [ "$team" = "$TEAM_ID" ] || verify_fail 'App signing team mismatch.'
        assessment=$(spctl -a -vv -t exec "$source_app" 2>&1) || verify_fail 'App Gatekeeper verification failed.'
        log "$assessment"
        [[ "$assessment" = *'source=Notarized Developer ID'* ]] || verify_fail 'App is not notarized.'
        [ "$(plist "$source_app" CFBundleIdentifier)" = "$bundle" ] && \
            [ "$(plist "$source_app" CFBundleShortVersionString)" = "$version" ] && \
            [ "$(plist "$source_app" CFBundleVersion)" = "$build" ] && \
            [ "$(plist "$source_app" LSMinimumSystemVersion)" = "$minos" ] || verify_fail 'App metadata disagrees with manifest.'
        if [ "$dry" = 1 ]; then runtime_action=would-install
        else
            quit_app
            mkdir -p "$appdir" "$HOME/.Trash"
            staging="$appdir/.Agent Computer Use.install.$$.app"
            ditto "$source_app" "$staging" || fail 10 'App staging failed; installed app preserved.'
            previous=''
            if [ -n "$app" ]; then
                "$lsregister" -u "$app" >> "$logfile" 2>&1 || fail 10 'Could not unregister old app.'
                previous="$HOME/.Trash/Agent Computer Use.$(date +%s).$$.app"
                mv "$app" "$previous" || fail 10 'Could not preserve old app.'
            fi
            while IFS= read -r stray; do
                [ -z "$stray" ] || "$lsregister" -u "$stray" >> "$logfile" 2>&1
            done < <(mdfind "kMDItemCFBundleIdentifier == '$bundle'")
            app="$appdir/Agent Computer Use.app"
            if ! mv "$staging" "$app"; then
                if [ -n "$previous" ]; then mv "$previous" "$app" || log "Recovery required: old app is $previous"; fi
                fail 10 'App swap failed.'
            fi
            "$lsregister" -f "$app" >> "$logfile" 2>&1 || fail 10 'App registration failed.'
            hdiutil detach "$mountpoint" >> "$logfile" 2>&1 || fail 10 'DMG detach failed.'
            mountpoint=''; rm "$download"; download=''
            open "$app" >> "$logfile" 2>&1 || fail 10 'App launch failed.'
            runtime_action=installed
        fi
    fi
else
    app=$(find_app)
    if [ -n "$app" ]; then version=$(plist "$app" CFBundleShortVersionString); build=$(plist "$app" CFBundleVersion); fi
fi
# Listing evidence is intentionally labelled as such; it is not a tool-call proof.
installed_version() {
    local harness=$1 plugin=$2 listing="$scratch/$1-list.json" selector=${3:-version}
    case "$harness" in
        codex)
            # https://developers.openai.com/plugins/build/plugins
            codex plugin list --json > "$listing" 2>> "$logfile" || return 1 ;;
        claude)
            # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
            claude plugin list --json > "$listing" 2>> "$logfile" || return 1 ;;
        cursor)
            # Cursor has no documented plugin-install/list CLI: inspect its local receipt only.
            listing="$HOME/.cursor/plugins/installed_plugins.json"
            [ -f "$listing" ] || return 1 ;;
        *) return 1 ;;
    esac
    /usr/bin/osascript -l JavaScript - "$listing" "$plugin" "$selector" "$phase" <<'JXA'
ObjC.import('Foundation');
function run(a) {
 var v=JSON.parse(ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(a[0],$.NSUTF8StringEncoding,null)));
 var rows=Array.isArray(v)?v:(Array.isArray(v.installed)?v.installed:[]);
 var p=rows.find(function(x){return (x.name===a[1] || (x.id||x.pluginId||'').split('@')[0]===a[1]) && (a[3]==='remove' || x.enabled===true);});
 if(!p || !p.version) throw Error('No enabled versioned receipt'); return String(a[2]==='id' ? (p.id||p.pluginId||p.name) : p.version);
}
JXA
}
result_code=0
for name in $names; do
    entries=''
    for plugin in $plugins; do
        prior_code=$result_code; result_code=0
        pv=''; action=skip
        if [ "$name" = grok ]; then action=handled-by-bot
        elif [ "$phase" = finish ]; then action=skip
        else
            pv=$(installed_version "$name" "$plugin" 2>/dev/null) || pv=''
            wanted=$pv
            if [ "$plugin" = agent-computer-use ] && [ "$phase" = install ]; then wanted=$version; fi
            if [ "$phase" = remove ] && [ -z "$pv" ]; then action=skip
            elif [ "$phase" = install ] && [ -n "$pv" ] && [ "$pv" = "$wanted" ]; then action=skip
            elif [ "$name" = claude ] && command -v claude >/dev/null; then
                if [ "$dry" = 1 ]; then action=would-$phase
                elif [ "$phase" = remove ]; then
                    # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
                    selector=$(installed_version claude "$plugin" id 2>/dev/null) || selector=''
                    if [ -n "$selector" ] && claude plugin uninstall "$selector" >> "$logfile" 2>&1; then
                        if installed_version claude "$plugin" >/dev/null 2>&1; then action=failed; result_code=14; else action=removed; fi
                    else action=failed; result_code=14; fi
                else
                    # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
                    if ! claude plugin marketplace list --json > "$scratch/claude-marketplaces.json" 2>> "$logfile"; then
                        result_code=14
                    elif ! grep -q '"ingpoc-agent-plugins"' "$scratch/claude-marketplaces.json"; then
                        # https://code.claude.com/docs/en/discover-plugins#add-marketplaces
                        claude plugin marketplace add "https://github.com/$REPO" >> "$logfile" 2>&1 || result_code=14
                    fi
                    # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
                    if [ "$result_code" = 0 ] && claude plugin marketplace update ingpoc-agent-plugins >> "$logfile" 2>&1; then
                        if [ -n "$pv" ]; then
                            # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
                            claude plugin update "$plugin@ingpoc-agent-plugins" >> "$logfile" 2>&1 || result_code=14
                        else
                            # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
                            claude plugin install "$plugin@ingpoc-agent-plugins" >> "$logfile" 2>&1 || result_code=14
                        fi
                    else result_code=14; fi
                    if [ "$result_code" = 0 ]; then
                        pv=$(installed_version claude "$plugin" 2>/dev/null) || result_code=14
                        if [ "$plugin" = agent-computer-use ] && [ "$pv" != "$version" ]; then result_code=14; fi
                    fi
                    action=installed; [ "$result_code" = 0 ] || action=failed
                fi
            else
                action=manual; [ "$dry" = 0 ] || action=would-manual
                case "$name" in
                    codex) log 'Manual Codex install/remove: https://developers.openai.com/plugins/build/plugins' ;;
                    cursor) log 'Manual Cursor install/remove: https://cursor.com/docs/plugins (refresh/update or remove and re-add the marketplace to re-index new rows).' ;;
                    *) log "Manual $name setup required." ;;
                esac
            fi
        fi
        if [ "$result_code" = 0 ]; then result_code=$prior_code; fi
        [ -z "$entries" ] || entries="$entries,"
        entries="$entries{\"name\":$(quote "$plugin"),\"version\":$(quote "$pv"),\"action\":$(quote "$action")}"
    done
    [ -z "$harnesses" ] || harnesses="$harnesses,"
    harnesses="$harnesses{\"name\":$(quote "$name"),\"plugins\":[$entries]}"
done
if [ "$phase" = remove ] && [ -n "$app" ]; then
    if [ "$dry" = 1 ]; then runtime_action=would-remove
    else
        quit_app
        "$lsregister" -u "$app" >> "$logfile" 2>&1 || fail 10 'Could not unregister app.'
        mkdir -p "$HOME/.Trash"
        mv "$app" "$HOME/.Trash/Agent Computer Use.$(date +%s).$$.app" || fail 10 'Could not move app to Trash.'
        runtime_action=removed
    fi
fi
if [ "$phase" = finish ]; then
    [ -n "$app" ] || fail 13 'ACU app is not installed.'
    status="$scratch/status.json"
    deadline=$((SECONDS+600))
    while :; do
        "$app/Contents/MacOS/CUAService" status --json > "$status" 2>> "$logfile" || fail 10 'ACU status failed.'
        permissions=$(json get "$status" permissions) || fail 13 'Invalid ACU status JSON.'
        ax=$(json get "$status" permissions.accessibility)
        screen=$(json get "$status" permissions.screen_recording)
        mic=$(json get "$status" permissions.microphone)
        if [ "$ax" = true ] && [ "$screen" = true ] && [ "$mic" = granted ]; then break; fi
        [ "$SECONDS" -lt "$deadline" ] || fail 10 'Timed out waiting for ACU permissions.'
        [ "$dry" = 0 ] || break
        sleep 5
    done
    for name in $names; do
        ok=false; detail=''; proofout="$scratch/proof-$name"
        case "$name" in
            codex)
                # https://developers.openai.com/plugins/build/plugins
                if installed_version codex agent-computer-use > "$proofout" 2>> "$logfile"; then ok=true; detail='enabled agent-computer-use receipt (listing proof)'; else detail='plugin listing failed or plugin disabled/missing'; fi ;;
            cursor)
                cursor_cli=agent; command -v agent >/dev/null || cursor_cli=cursor-agent
                # https://cursor.com/docs/cli/reference/mcp
                if "$cursor_cli" mcp list-tools agent-computer-use > "$proofout" 2>> "$logfile" && grep -qw state "$proofout" && grep -qw act "$proofout"; then ok=true; detail='ACU MCP state and act tools enumerated'; else detail='ACU MCP tools unavailable'; fi ;;
            claude)
                # https://code.claude.com/docs/en/plugins-reference#cli-commands-reference
                if installed_version claude agent-computer-use > "$proofout" 2>> "$logfile"; then ok=true; detail='enabled agent-computer-use receipt (listing proof)'; else detail='plugin listing failed or plugin disabled/missing'; fi ;;
            grok) detail='handled-by-bot: bot must call ACU state; no documented noninteractive CLI proof' ;;
        esac
        [ -z "$proof" ] || proof="$proof,"
        proof="$proof{\"harness\":$(quote "$name"),\"ok\":$ok,\"detail\":$(quote "$detail")}"
        if [ "$ok" = false ] && [ "$name" != grok ]; then result_code=14; fi
    done
    if [ "$dry" = 0 ] && [ "$result_code" = 0 ]; then
        open 'acu-island://unlock' >> "$logfile" 2>&1 || fail 10 'Could not open island unlock.'
        unlock=true
    fi
fi
summary
exit "$result_code"
