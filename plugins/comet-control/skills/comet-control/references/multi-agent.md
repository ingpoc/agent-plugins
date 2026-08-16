# comet-control WIP — Multi-agent leases

Load when two agents share the managed Comet Control browser runtime, or when a run
needs an owned window and labeled cursor.

## Architecture (locked)

- One shared Unix socket (WIP): `…/run/comet-control.sock`
- Isolation key: opaque session id + process-private token + owned window/tab
- Per-session FIFO; cross-session tasks parallel, global visual command slices serialized
- Labeled cursor below pointer (`agentLabel`)
- Silent driver renewal + idempotent `session_closeout` + host-locked TTL reaper
- Required non-empty `sessionId`; token-authenticated reuse and closeout
- User-private broker socket; failed preflight rolls back its created target
- Serialized display layout; secondary display when present; no overlap
- Never tab-group a single-tab owned window (macOS lease vanish)
- Window isolation is mandatory; claimed/tab-only sessions are rejected
- Every `run` requires a live lease; there is no active-tab fallback

Decision history stays in the source repo, not this portable package.

## Read-only host check (do this first)

```bash
../../scripts/ensure-wip-broker.sh probe --json
```

Require the probe's broker-attested `user_data_dir`; Preferences and cached
profile names are diagnostics, never ownership proof. The unpacked target is
`deploy/extension`; its transport is the repository-owned loopback broker.

## Per-agent campaign driver

Each agent starts one driver for its complete bounded browser task or testing
campaign. A campaign spans setup, all planned cases, same-window diagnosis,
retests, and final proof. The driver creates one window-isolated lease, retains
the private token in process memory, silently renews while alive, redacts private
values, and applies a bounded timeout to every command. The TTL is only orphan
cleanup grace after driver failure; it never defines campaign duration.

```bash
python3 skills/comet-control/scripts/lease_driver.py \
  --session-id "agent-$(uuidgen)" --label "Agent A" \
  --url "https://example.com/" --ttl-seconds 300
```

Send NDJSON to the persistent process:

```json
{"actions":[{"type":"page_context"}]}
{"actions":[{"type":"click_selector","selector":"#target"},{"type":"screenshot","format":"png"}]}
{"command":"sessions"}
{"command":"closeout"}
```

Reuse that process, session id, and window for navigation, reads, clicks, forms,
screenshots, test cases, and retests. Never close and reopen for a small test or
failed assertion. Diagnose a failed command through the same lease; do not
silently open a replacement window. Request logical closeout once when the
whole campaign is complete, cancelled, or genuinely unrecoverable; explicit
closeout, EOF, interrupt, or termination all use the same terminal path.
Retryable removal failures may retry that authenticated closeout internally up
to three times without creating a replacement. Concurrent agents
use distinct session ids and drivers. After closeout, require the session id to
be absent from inventory.

## Ownership split (`coexistence-v1`)

Clear owners — not a retry ladder. Hand off when the job crosses the boundary;
do not re-attempt the other skill’s surface here.

| Surface | Owner |
| --- | --- |
| Page DOM, SPA, cursor clicks, locators, screenshots of page content | **Comet Control** |
| JS `alert` / `confirm` / `prompt` / `beforeunload` (`dialog_get` / `dialog_handle`) | **Comet Control** |
| Non-Comet macOS apps (AX, keys, native UI) | **`$macos-cua`** |
| OS sheets / file choosers / print / system permission UI on the Comet Control PID | **`$macos-cua`** via `native_handoff` |
| Comet shell: `chrome://`, extension load/reload, launch/quit, window geometry | **`$macos-cua`** `--browser-intent comet-admin` after **zero** leases |

Comet Control and macos-cua may run concurrently on disjoint exact PIDs; only short
foreground/capture slices serialize. Before macos-cua targets the managed
`browser_pid`, its CLI claims that runtime and releases in `finally`. Comet Control
rejects new leases and browser mutations while the claim is live.

Shared `visual-focus-v1` lock: one brokered browser request or one CUA app
command. Never a task-wide mutex. Lease tokens never cross agents.

**Hand to macos-cua (native-dialog):** prefer the atomic slice (claim → CUA →
release) so agents cannot desync the durable controller or leave orphan claims:

```bash
python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" --ttl-seconds 45 state
python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" run @plan.json
```

Manual equivalent: finish the current Comet Control command →
`durable_lease_controller send '{"command":"native_handoff","ttlSeconds":45}'`
→ pass the short-lived claim (never the lease token) to
`macos-cua.py --browser-intent native-dialog --browser-session-id <id>
--browser-claim-token <claim> … pid:<browser_pid>` → always
`check-cua-coexistence.py --release-claim <claim>`. Keep the Comet Control driver
alive but command-idle; after CUA exits, re-observe on the **same** lease.
`native_handoff` reclaims an orphan native-dialog claim owned by the same
session (dead CUA process) instead of waiting out TTL.

### Google OAuth slice (token-tight)

Proven path for logout→Google login on X / LinkedIn (and similar GSI sites).
Keep one lease for the whole campaign.

| Step | Owner | Do | Don't |
| --- | --- | --- | --- |
| 1 | Comet Control | `goto` / logout UI → `click_text` the **in-page** Google CTA once | Retry the page CTA while an overlay is up |
| 2 | Decide | Hand off if `handoff_hint`, or `cua_slice state` shows GSI / `accounts.google.com` / `Continue as …` absent from Comet Control buttons | Dump full AX trees into chat; invent claim/release loops |
| 3 | `cua_slice` | `state` once → `run` one click with exact AX label + `expect` | Guess labels; omit `expect` on mutating plans |
| 4 | Comet Control | Same-lease `page_context` (logged-in URL / `Me` / `Account menu`) | Closeout mid-auth; comet-admin |

**Labels that worked live:**

- Overlay button: `Continue as <account name>` (short form; do not require the email suffix unless AX shows it)
- Account chooser link: use the exact AX name shown on that machine

```bash
# plan.json — one mutating click, fail-closed expect
{"actions":[{"action":"click","label":"Continue as <account name>","expect":{"not_text":"Continue as <account name>"}}]}
python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" --ttl-seconds 90 run @plan.json
python3 skills/comet-control/scripts/durable_lease_controller.py send --workdir "$WORK" \
  '{"actions":[{"type":"page_context"}]}'
```

**Recovery (LinkedIn SPA remount):** Prefer Me → Sign out over `linkedin.com/m/logout/` mid-campaign. If Comet Control returns `Content script missing after SPA remount`, wait for the page to settle (`cua_slice` `expect` on visible chrome) then `page_context` again; use `reload_page` alone only if still wedged. Do not treat remount errors as permission to open a new lease.

**Hand to macos-cua (comet-admin):** close every lease first →
`--browser-intent comet-admin` → re-probe → new lease if needed.

To inspect the boundary without acquiring it:

```bash
./scripts/check-cua-coexistence.py --target-pid <pid> --intent comet-admin
```

## Operator-visible window layout

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
./scripts/sync-wip.sh   # after code changes; then reload extension
COMET_CONTROL_BRIDGE_SOCKET="$PWD/run/comet-control.sock" \
  python3 plugin/comet_control/tests/test_multi_agent_isolation.py
# Two consecutive exit 0 required
```

The gate includes 2/3/4-window secondary-display geometry and re-tiling after
closeout, in addition to negative lease boundaries, screenshots, overlapping
clicks, continuation, and clean closeout. Require two consecutive green runs.

## Known failure modes (fixed in WIP code)

| Failure | Fix in tree |
| --- | --- |
| Lease vanish after preflight | Remove tab-group and active-tab fallbacks; keep the exact owned-window tab |
| Empty `agent_label` after navigation | Invalidate injection on load/URL change; reinject; re-apply identity |
| Human never sees pointer | `focused: true` + focus on `run`; watch correct host app; screenshot proof |
| Agent window hides another | Serialized secondary-display tiling on every lease lifecycle change |
