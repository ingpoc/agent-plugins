# Agent Handbook — modifying & troubleshooting Comet Control

Read before editing the plugin or debugging deeper than `optimize.md`.
`operate.md` / `optimize.md` = using the bridge; this handbook = modifying it.

> Runtime root is this plugin directory (`plugin.json`). Do not sync runtime
> files into `~/.codex` or `~/.comet-control`.
> Leases: [`multi-agent.md`](multi-agent.md)

---

## Architecture map

```text
Comet ──────────────────────────┬─ plugin/comet_control/extension (Load unpacked)
                                │       │ exact-origin loopback WebSocket
                                │       ▼
                                │   plugin/comet_control/native/broker.py
                                │       │ AF_UNIX
                                │       ▼
                                └── run/comet-control.sock
```

One owner: `plugin/comet_control/`.

| Piece | Path | Role |
| --- | --- | --- |
| Extension | `plugin/comet_control/extension/` | SW, cursor, leases |
| Broker | `plugin/comet_control/native/broker.py` | WebSocket ↔ socket |
| Skill | `skills/comet-control/` | Agent instructions |
| Isolation suite | `plugin/comet_control/tests/test_multi_agent_isolation.py` | 2× green gate |

---

## Hard-won lessons

### Wrong browser runtime

**Symptom:** Suite/screenshots green; the operator sees nothing in Comet.
**Why:** The extension is missing, disabled, or running under a custom Comet
user-data directory.
**Fix:** Run `../../scripts/ensure-broker.sh probe --json`; require its
broker-attested runtime, then use `../../scripts/launch-comet.sh`. Never
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
**Fix:** Do not steal macOS focus (`focused: false`); screenshot + Read(image); hold if demoing.

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

### Broker

`ensure-broker.sh start` supervises `broker.py` with `run/comet-control.sock` and
the exact extension origin. If the socket is missing, inspect the broker log.

### `chrome.debugger` vs DevTools

`Another debugger is already attached` is a real attach collision. Close
DevTools on the target tab. `Cannot access a chrome-extension:// URL of
different extension` is instead a foreign-extension URL/frame restriction;
Chromium redacts the other extension id, and the service worker reports `hit a
foreign-extension URL/frame restriction`. Do not guess the extension, retry
`debugger.attach`, mint another session, closeout, or reload while leases are
live. Locator clicks fall back to the content script in controllable same-tab
`http(s)` / `file` frames; foreign-extension frames are never queried.
`page_context` can still be healthy, so diagnose through the same lease.

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

1. Send `status` through `run/comet-control.sock`.
2. Start a leased `session_preflight` on a real `https://` URL.
3. If both fail after an extension edit, close every lease, use the host reload
   and probe sequence below, then retry with a new lease.

### Bridge socket present but no actions

1. `lsof …/run/comet-control.sock`
2. `ensure-broker.sh probe --json`
3. If `extension_connected` remains false, follow
   [`extension-install.md`](extension-install.md) (reload/install + probe smoke)

### Cursor/UI claim

1. `screenshot` → **Read the PNG**
2. If the overlay is present but unseen, check the host app and window focus.

### Agents collide / tabs change under me

Use leases ([`multi-agent.md`](multi-agent.md)), not `sessionName` alone.

---

## Editing conventions

- Edit **`plugin/comet_control/`** only.
- After changes, close every lease and require `verified_absent` / empty
  sessions. Send host `{"type":"reload"}` on `run/comet-control.sock`
  (`bridge({"type":"reload"})`), require `success` plus `reloading`, and stop on
  `ACTIVE_AGENT_LEASES`. Wait and repeat `ensure-broker.sh probe --json` until
  `success`, `runtime_verified`, `extension_connected`, and pairing return;
  confirm `connection_generation` and `extension_build_sha256` moved. CUA Load
  unpacked is for first install or a missing card, not the default reload.
- Never sync into `~/.codex` or `~/.comet-control`.
- New action → SW + content script if needed + `lease_driver.py` + `operate.md`.
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
- Loaded broker/extension hashes must match `plugin/comet_control/` before acceptance. A
  stale broker may restart only after an authoritative empty lease inventory.
