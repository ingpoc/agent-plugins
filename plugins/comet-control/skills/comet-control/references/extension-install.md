# Extension install (Agent Computer Use)

Owner: first install, reload after `plugin/comet_control/` edits, or
`EXTENSION_NOT_CONNECTED` after broker start. Comet shell + OS file sheet only —
not page DOM. Zero leases required (`comet-admin`). Never inspect or copy the
Comet profile / credentials.

## Paths

| Role | Path |
| --- | --- |
| Runtime root | directory with `plugin.json` |
| Unpacked extension | `$ROOT/plugin/comet_control/extension` |
| Absolute (Cursor local plugin) | `~/.cursor/plugins/local/comet-control/plugin/comet_control/extension` |

Always load **that** directory (folder that contains `manifest.json`). Do not
Select a stale WIP/deploy twin even if its folder is also named `extension`.

## Preconditions

```bash
cd "$ROOT"
test -f plugin/comet_control/extension/manifest.json
./scripts/ensure-broker.sh start
# Prefer zero leases. If any exist, closeout first.
./scripts/ensure-broker.sh probe --json   # may be EXTENSION_NOT_CONNECTED — ok pre-install
```

Use **Agent Computer Use** (`$macos-cua` MCP: `start_session` → `state` / `act` /
`verify` → `end_session`). Do not drive this through Comet Control leases.

## Fast install (proven)

1. **Open the Extensions page in a dedicated Comet window** (do not trust typing
   into an unrelated tab’s address bar alone — the omnibox can show
   `comet://extensions/` while the old WebArea remains):

   ```bash
   open -a Comet "chrome://extensions/"
   # Comet may rewrite to comet://extensions/ — both are fine once the window title is Extensions
   ```

2. **`state` Comet** until all of these are true:
   - Window title contains `Extensions`
   - Address is `comet://extensions/` or `chrome://extensions/`
   - Query hits `Developer mode` (checkbox on) and **`Load unpacked`**

   If Developer mode is off: click the `Developer mode` checkbox, re-`state`,
   then continue. Prefer AX **label/element** clicks. Do not waste retries on
   pixel `click-point` for `Load unpacked` — geometry drift is common on multi-
   display Comet layouts.

3. **Click `Load unpacked`** (`act` label or fresh element index). Expect the
   open sheet: `Select the extension directory.` / `AXSheet "open"` with
   `Select` + `Cancel`.

4. **Navigate to the exact extension directory**, then Select:

   Prefer **Go to Folder** on the sheet:

   ```text
   act: key cmd+shift+g   (expect Go to the folder / path field)
   act: type  <absolute path to plugin/comet_control/extension>
   act: key Return        (expect the folder name or its children)
   act: click Select      (expect not_text: Select the extension directory)
   ```

   If `cmd+shift+g` delivery fails (`escalation.recommended=foreground`),
   foreground Comet once and retry the same Go-to-Folder slice — do not invent
   a second install path.

   Only click `Select` without Go-to-Folder when `Where:` already shows this
   plugin’s `…/comet_control/extension` (not a different `…/extension` tree).

5. **UI proof:** `verify` Comet `expect: Comet Control` (card and/or toolbar
   popup). Sheet must be gone.

## Validate Comet Control (required after install/reload)

Probe is the authority — not Preferences, Secure Preferences, or window focus.

```bash
cd "$ROOT"
./scripts/ensure-broker.sh start
./scripts/ensure-broker.sh probe --json
```

Require all of:

- `success: true`
- `runtime_verified: true`
- `extension_connected: true`
- `pairing_established: true` (when the probe reports it)

Then one leased smoke (same campaign id; durable controller):

```bash
SESSION_ID="install-smoke-$(date +%s)"
WORK=/tmp/comet-control-$SESSION_ID
python3 skills/comet-control/scripts/durable_lease_controller.py start \
  --session-id "$SESSION_ID" \
  --label "install-smoke" \
  --url "https://example.com/" \
  --workdir "$WORK" \
  --ttl-seconds 300
# wait ready.json ok:true
python3 skills/comet-control/scripts/durable_lease_controller.py send --workdir "$WORK" \
  '{"actions":[{"type":"page_context"}]}'
# require final_url https://example.com/ and title Example Domain
python3 skills/comet-control/scripts/durable_lease_controller.py closeout --workdir "$WORK"
# require verified_absent: true
```

Pass = probe gates + smoke `page_context` + closeout `verified_absent`. Skip the
smoke only when the caller asked for install-only and probe gates already pass.

## Reload after source edits

1. Close every lease (`verified_absent` / empty sessions inventory).
2. On Extensions: remove or **Reload** the existing Comet Control card, or
   Load unpacked again from `$ROOT/plugin/comet_control/extension`.
3. Re-run **Validate Comet Control** above.

Never reload or administer the extension while leases are active.

## Failure shortcuts

| Symptom | Fix |
| --- | --- |
| Omnibox says extensions URL; WebArea still old site | `open -a Comet "chrome://extensions/"` or click Reload on a true Extensions window |
| No `Load unpacked` in AX | Wait for `AXWebArea "Extensions"` + Developer mode on; re-`state` |
| Open sheet picks wrong `extension` folder | Go to Folder → absolute `$ROOT/plugin/comet_control/extension` before Select |
| Probe `EXTENSION_NOT_CONNECTED` after Select | Confirm card enabled; Reload card; broker `start`; re-probe |
| `COMET_CONTROL_RUNTIME_UNAVAILABLE` on coexistence check pre-install | Expected before pairing; finish install + probe — do not block first load on that claim |
| Pixel click geometry mismatch | Use AX `Load unpacked` / `Select` labels instead |

## Do not

- Install into Google Chrome or any non-Comet profile
- Use `--load-extension` launch flags (forbidden by source contracts)
- Write Secure Preferences / profile JSON by hand
- Route through `~/.comet-control`
- Start a lease to perform extension admin
