# Agent Handbook — modifying & troubleshooting Comet Control (WIP)

Read before editing the plugin or debugging deeper than `optimize.md`.
`operate.md` / `optimize.md` = using the bridge; this handbook = modifying it.

> Runtime root is this plugin directory (`plugin.json`). Do not sync runtime
> files into `~/.codex` or `~/.comet-control`.
> Leases: [`multi-agent.md`](multi-agent.md)

---

## Architecture map

```text
Comet ──────────────────────────┬─ deploy/extension (Load unpacked)
                                │       │ exact-origin loopback WebSocket
                                │       ▼
                                │   deploy/native/broker.py
                                │       │ AF_UNIX
                                │       ▼
                                └── run/comet-control.sock
```

Source: `plugin/comet_control/`. `scripts/sync-wip.sh` deploys to `deploy/`.

| Piece | Source | Deploy | Role |
| --- | --- | --- | --- |
| Extension | `plugin/comet_control/extension/` | `deploy/extension/` | SW, cursor, leases |
| Broker | `plugin/comet_control/native/broker.py` | `deploy/native/broker.py` | WebSocket ↔ socket |
| Skill | `comet-control/` | this tree | Agent instructions |
| Isolation suite | `plugin/comet_control/tests/test_multi_agent_isolation.py` | — | 2× green gate |

---

## Hard-won lessons (WIP + carried forward)

### Wrong browser runtime

**Symptom:** Suite/screenshots green; the operator sees nothing in Comet.
**Why:** The extension is missing, disabled, or running under a custom Comet
user-data directory.
**Fix:** Run `../../scripts/ensure-wip-broker.sh probe --json`; require its
broker-attested runtime, then use `../../scripts/launch-wip-comet.sh`. Never
infer ownership from a focused window or Preferences.

### Lease vanish on owned window

**Symptom:** Preflight ok; lease gone before/during `run`.
**Why:** Tab-grouping a single-tab owned window can collapse it on macOS.
**Fix:** The obsolete tab-group/active-tab fallback was removed; every run stays
on the exact tab in its mandatory owned-window lease.

### Cursor label empty after navigation

**Symptom:** `agent_label` ≠ session label after `goto`.
**Why:** Injection guard survived navigation; content world gone.
**Fix:** Invalidate on load/URL change; reinject; re-apply lease identity.

### Operator never sees the floating pointer

**Symptom:** `cursor_status.visible` true; human sees no pointer.
**Why:** `focused: false` (behind IDE); wrong host app; suite closes fast.
**Fix:** Focus windows on create/run; screenshot + Read(image); hold if demoing.

### Socket is shared — leases isolate agents

**Symptom:** Actions land in the wrong tab/window.
**Why:** One AF_UNIX socket; `sessionName` alone does not isolate.
**Fix:** Start one `lease_driver.py` process with a unique session id and owned
window. The driver retains the private token. Unleased runs, active-tab
fallback, claimed tabs, and tab-only isolation are rejected.

### Service worker reload ≠ content-script refresh

**Symptom:** After reload, `"Content script did not respond after injection"`.
**Fix:** Reload the tab or `goto`. Auto-inject on `tabs.onUpdated` complete.

### `evaluate` uses `expression`, not `script`

`{"type":"evaluate","expression":"..."}`.

### Do not re-introduce `cdp_bridge.py`

Extension bridge only. Grep before adding transport files.

### MV3: no inline `<script>` from content scripts

Use `chrome-extension://…/runtime.js` + `web_accessible_resources`.

### Broker (macOS WIP)

`ensure-wip-broker.sh start` supervises `broker.py` with the WIP socket and
exact extension origin. If the socket is missing, inspect the broker log.

### `chrome.debugger` vs DevTools

Only one debugger per tab — close DevTools on the target tab.

### Don't hide cursor around `elementFromPoint`

Host is `pointer-events: none`. Hide/show caused opacity flicker; do not
reintroduce it.

### Locator clicks retain the resolved node

Selector/text resolution returns an opaque content-script token for the exact
DOM node. Click-time hit testing must validate that node, not merely any current
element matching the same locator. On movement, re-resolve and retry once; never
dispatch to stale coordinates or accept inverse ancestor containment.

### `createOverlay()` must preserve position + visibility

Re-injection must restore transform + `comet-control-visible` or the cursor vanishes
until its next move.

### Popup init must be fault-isolated

Optional `await` must not gate critical controls (`safeInit` pattern).

### Logging: `console.error` only for real errors

Traces → `console.debug` or remove.

---

## Diagnostic recipes

### Content script did not respond

1. Send WIP `status` through `run/comet-control.sock`.
2. Start a leased `session_preflight` on a real `https://` URL.
3. If both fail after an extension edit, run the repo-root `sync-wip.sh`, reload
   the unpacked extension, and retry with a new lease.

The copied production `plugin/comet_control/scripts/preflight.sh` and `sync.sh`
are outside the WIP control path and must not be used here.

### Bridge socket present but no actions

1. `lsof …/run/comet-control.sock`
2. `ensure-wip-broker.sh probe --json`
3. Reload the Comet extension only if `extension_connected` remains false

### Cursor/UI claim

1. `screenshot` → **Read the PNG**
2. If the overlay is present but unseen, check the host app and window focus.

### Agents collide / tabs change under me

Use leases ([`multi-agent.md`](multi-agent.md)), not `sessionName` alone.

---

## Editing conventions

- Edit **`plugin/comet_control/`** only.
- Never hand-edit **`deploy/`**.
- After changes, run **`scripts/sync-wip.sh`** and reload the unpacked
  extension. `bridge({"type":"reload"})` is safe only with no leases.
- Never sync into `~/.codex` / `~/.comet-control` until cutover.
- New action → SW + content script if needed + `tools.py` + `operate.md`.
  Update `multi-agent.md` only for lease behavior.
- No CDP transport. Keep `debugger` permission. Don't widen manifest without reason.

---

## Cross-references

- [`multi-agent.md`](multi-agent.md) · [`operate.md`](operate.md) ·
  [`optimize.md`](optimize.md) · [`../SKILL.md`](../SKILL.md)
- Plugin source: `plugin/comet_control/`

## Reliability invariants

- The extension must restore exact persisted lease targets before announcing
  its protocol handshake. Disconnect invalidation clears only those leased
  tabs; there is no active-tab or other-page fallback.
- Click/fill resolve exactly one visible node, then check enabled/editable,
  two-frame geometry stability, and exact center hit testing. Structured
  `ACTIONABILITY_*` failures must not enter reload recovery.
- Snapshot refs and semantic cache entries are in-memory and page-revision
  scoped. Never persist coordinates, target tokens, form values, or lease data.
- The broker admits one paired extension generation, caps responses at 16 MiB
  and pending work at 64, and fails stale-generation work without replay.
- Failure records are local, mode-0600, retain at most 20 commands, and capture
  a screenshot only after failure.
- Loaded broker/extension hashes must match `deploy/` before acceptance. A
  stale broker may restart only after an authoritative empty lease inventory.
