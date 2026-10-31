# scripts/install-mac.sh spec (from Agent Plugins Installer, 2026-10-31 20:13 IST)

## Invocation
- `install-mac.sh install|finish|remove [--plugins a,b] [--allow-quit] [--dry-run]`
- `install` covers fresh installs and updates. Idempotent: a rerun on an up-to-date Mac does nothing and exits 0.
- Published as a release asset at releases/latest/download/install-mac.sh and listed in SHA256SUMS. The bot checks its SHA, then runs it with bash (never curl|bash).
- Plain bash plus macOS built-ins only: no brew, no jq (use plutil or osascript -l JavaScript for JSON), no sudo.

## Pinned constants
REPO=ingpoc/agent-plugins
TEAM_ID=9UPQL479Z5 (never read from the manifest)
MANIFEST=https://github.com/$REPO/releases/latest/download/manifest.json

## install
1. Preflight: macOS >= manifest minMacOS; `uname -m` is arm64, else exit 12. Admin check `id -Gn | grep -qw admin` picks /Applications, else ~/Applications.
2. Detect harnesses: command -v codex, agent/cursor-agent, grok, claude, plus their config dirs. Report Grok Bot as handled-by-bot.
3. Plugins: agent-computer-use for every detected harness. comet-control only if /Applications/Comet.app exists. context-ledger only via --plugins. Each harness only gets plugins its own catalog publishes (Claude Code's has agent-computer-use and context-ledger); others are reported as `skipped: not in catalog`. Grok is read with `grok plugin list --json` and never installed by the script (its trust prompt belongs to the user): current is `skip`, otherwise `manual`. Install through each harness's documented CLI (marketplace add/update, then install/update), with the doc cited in a comment for each command. Cursor needs the remove/re-add-or-update quirk for new catalog rows.
4. Manifest: fetch with `curl -fsSL --proto =https`. Cross-check sha256 against SHA256SUMS; a mismatch exits 10.
5. Runtime skip: if the installed app (found by bundleId via mdfind) has CFBundleShortVersionString+CFBundleVersion equal to the manifest's, skip it.
6. Download to ~/Library/Caches/agent-plugins/, then verify in order: shasum -a 256; `spctl -a -t open --context context:primary-signature -vv` on the DMG; `hdiutil attach -nobrowse -readonly`; `codesign --verify --deep --strict`; TeamIdentifier == TEAM_ID; `spctl -a -vv -t exec` reports Notarized Developer ID. On any failure: detach, move the download to the Trash, exit 10.
7. Swap: if the app is running (pgrep -x), quit it via osascript only with --allow-quit, else exit 11. Move the old copy to ~/.Trash; lsregister -u any stray copies with the same bundleId; ditto the new app in; lsregister -f; detach; delete the DMG; then `open` (not -g).

## finish (second run, after the user has clicked through onboarding)
Wait up to 10 minutes on an ACU status command (e.g. `acu status --json`; use the real one) until the permissions are granted. Run one proof call per detected harness, open the acu-island:// unlock URL, and print the summary.

## remove
Uninstall the plugins per harness, quit the app (with --allow-quit), lsregister -u, and move the app to the Trash. Keep user data and Keychain items.

## Output
Human-readable logs go to stderr and to ~/Library/Logs/agent-plugins/install.log (overwritten each run). The last stdout line is JSON:
{"phase","harnesses":[{"name","plugins":[{"name","version","action"}]}],"runtime":{"version","build","path","action"},"permissions":{...},"proof":[{"harness","ok","detail"}],"unlock_opened"}

## Exit codes
0 ok, 10 verification failed, 11 needs quit consent, 12 unsupported Mac, 13 network or manifest error, 14 a real harness install failed (partial result still in the JSON); catalog skips and manual steps never exit 14.

## Hard rules
Never remove quarantine, use spctl --master-disable, write to TCC, or script System Settings. No secrets in args or env. No ps/pgrep flags that show command lines (only pgrep -x). No building from source.

## Tests before merge
- shellcheck.
- --dry-run on the Mini.
- A tampered SHA gives exit 10.
- A DMG signed by the wrong team gives exit 10.
- A rerun on an up-to-date Mac gives exit 0 with every action skip.
- The JSON output parses.
