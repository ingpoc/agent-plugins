# Google accountchooser in OTHER Comet window

Canonical recipe when Google Sign-In opens a **separate** Comet window (`Sign in - Google Accounts` / accountchooser) while the lease stays on the product tab. Keep [SKILL.md](../SKILL.md) thin — load this reference only then.

In-page GSI (`Continue as …` on the leased tab) still uses [multi-agent.md](multi-agent.md) § Google OAuth slice + `cua_slice`.

## Proven path (ONDC / GSI popup)

When Google opens as a **separate** Comet window/tab (`Sign in - Google Accounts`, `accounts.google.com/.../accountchooser`) while the lease stays on the product tab:

| Don't | Why |
| --- | --- |
| Lease CDP / Browser Use / `focus_tab` into the Google tab | Tab-scoped lease; "cannot target another session's tab" |
| Blind `cua_slice` / `macos-cua state pid:Comet` then coord click | Often binds the **wrong** Comet window (e.g. Cursor Agent); coords remapped; click reports ok and misses |
| Trust `~/.local/bin/cua-driver` FileNotFound as the root cause | Legacy status CLI path only. Engine is `~/.cache/macos-cua/CUAService.app` |
| cliclick / raw `CGEvent` as first try on GSI chooser | Live miss on ONDC HUF picker (2026-09-12) |

## Proven fix (AXPress the account `AXLink`)

Accessibility-trusted Python (pyobjc) on Mini:

1. Confirm `AXIsProcessTrusted()` (CUAService / acting Python). If Comet AX is empty (`axTrusted=false`, `elementCount=0`), fix Accessibility first.
2. Raise the Comet window whose title contains `Sign in - Google Accounts` (resize if Stage Manager thumb ~50×100).
3. `AXUIElement` walk that window only; find `AXLink` whose title/value contains the target account (e.g. `Gurusharan Gupta HUF` + `gupta.huf.gurusharan@gmail.com`).
4. `AXUIElementPerformAction(link, "AXPress")` — chooser advances (`accounts.google.com/signin/…`). No password / App Password typing.
5. Same-lease `page_context` on the product tab for auth proof; do not remint mid-flow.

Minimal sketch (do not paste secrets; keep lease id out of chat):

```python
# pyobjc ApplicationServices — walk Google Accounts window → AXPress AXLink matching account
from ApplicationServices import (
    AXUIElementCreateApplication, AXUIElementCopyAttributeValue, AXUIElementPerformAction,
    AXIsProcessTrusted,
)
assert AXIsProcessTrusted()
# resolve Comet pid → AXWindows → title contains "Google Accounts"
# walk AXChildren → role AXLink and needle in title/value → AXPress
```

After HUF/account press, Comet UI/extension may flap (`EXTENSION_NOT_CONNECTED`). Product bot owns portal proof; ACU owns picker miss recovery only.

**Recovery (LinkedIn SPA remount):** Pause compose; hand the same lease id + error to Agent Computer Use. Rule out an open JS dialog, then assert the first recovery batch is nav-only `reload_page`/`goto`, never identity-injecting reads or mutations. Read page state separately after recovery. Never mint a second session.

**Hand to macos-cua (comet-admin):** close every lease first →
`--browser-intent comet-admin` → re-probe → restore the same session; stay paused if restoration fails.
Extension Load unpacked / reload: [`extension-install.md`](extension-install.md).

To inspect the boundary without acquiring it:

```bash
./scripts/check-cua-coexistence.py --target-pid <pid> --intent comet-admin
```

## Leased window layout

Window-isolated leases are re-tiled whenever a host-locked request creates,
reuses, closes, or operates them. Startup restore and external target removal
only mark layout dirty; the next host-locked lifecycle/run applies it. Global
status and inventory never close or move windows. The extension selects the
largest non-primary display; if macOS exposes only one display, it uses that
display's work area and reports `display_role: "primary-only"`.

| Active leased windows | Layout |
| --- | --- |
| 1 | Full display work area |
| 2 | Left and right halves |
| 3 | Three cells of a 2×2 corner grid |
| 4 | Four quadrants |
| 5+ | Compact near-square grid |

The sessions inventory returns `display_*`, `layout_*`, requested and actual
window bounds. Use these for
geometry assertions; use a full desktop capture to prove what the operator can
see.

The isolation suite also rejects invalid IDs/timeouts/TTLs, tokenless takeover,
cross-session tab targeting, and reload during live leases. It returns a
screenshot proof for each cursor; inspect those PNGs before a visual claim.

## Visual proof

| Claim | Required evidence |
| --- | --- |
| Cursor / label visible | `screenshot` → **Read the PNG** (pointer + label in image) |
| Isolation | Distinct `window_id`/`tab_id`; suite or dual screenshots |
| All work visible | Inventory proves secondary-display non-overlap + full desktop capture |
| Closeout clean | Lease absent from sessions inventory; window closed |

`cursor_status.visible` alone is **not** operator proof.

## Hardening gate

```bash
cd <plugin-root>
# after code changes and verified-empty sessions, send host {"type":"reload"}, wait, and probe
COMET_CONTROL_BRIDGE_SOCKET="$PWD/run/comet-control.sock" \
  python3 plugin/comet_control/tests/test_multi_agent_isolation.py
# Two consecutive exit 0 required
```

The gate includes 2/3/4-window secondary-display geometry and re-tiling after
closeout, in addition to negative lease boundaries, screenshots, overlapping
clicks, continuation, and clean closeout. Require two consecutive green runs.

## Known failure modes

| Failure | Fix in tree |
| --- | --- |
| Lease vanish after preflight | Remove tab-group and active-tab fallbacks; keep the exact owned-window tab |
| Empty `agent_label` after navigation | Invalidate injection on load/URL change; reinject; re-apply identity |
| Cursor missing from proof | Verify the leased tab id and inspect its screenshot; never focus Comet |
| Agent window hides another | Serialized secondary-display tiling on every lease lifecycle change |
