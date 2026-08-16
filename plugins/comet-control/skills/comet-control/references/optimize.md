# Comet Control diagnosis and repair

Load this reference after the same bridge action fails twice, the WIP socket is
unreachable, or live readback contradicts the requested outcome. Routine use
belongs in [`operate.md`](operate.md); plugin invariants belong in
[`agent-handbook.md`](agent-handbook.md).

## Safety boundary

This skill controls only the plugin root (the directory with `plugin.json`):

Source lives in `plugin/comet_control/`, deployment lives in `deploy/`, and the
socket lives at `run/comet-control.sock`. Use `scripts/sync-wip.sh` after source
changes. Never run the copied production `plugin/comet_control/scripts/sync.sh`
or `preflight.sh`; both target live paths under `~/.codex` / `~/.comet-control` and are
outside this WIP runtime.

WIP operation must not create bridge artifacts under `~/.comet-control`.

## Diagnosis ladder

Stop at the first failed layer. A higher-layer success cannot prove a lower
layer that was skipped.

### 1. Probe the runtime without mutation

```bash
../../scripts/ensure-wip-broker.sh probe --json
```

The JSON result is authoritative for the broker-attested logged-in Comet profile
directory, exact-origin loopback safety, socket mode, bridge health, and active
sessions. Probe must make zero writes. A focused window and Comet Preferences
are not ownership proof.

### 2. Start the owned runtime explicitly

Start the repository-owned broker and normal logged-in Comet runtime:

```bash
../../scripts/ensure-wip-broker.sh start
../../scripts/launch-wip-comet.sh
../../scripts/ensure-wip-broker.sh probe --json
```

Do not delete socket state or use a legacy `~/.comet-control` runtime.

For a debugger/extension-URL error, first run `page_context` alone. If it works
and only `screenshot` fails, inspect the deployed viewport screenshot path: it
must use `chrome.tabs.captureVisibleTab`, not CDP. If `page_context` also fails,
use `$macos-cua` only inside Comet for visible `chrome://extensions`
administration. Load `deploy/extension` there once; do not copy, inspect,
export, or print the Comet profile or credential
database.

### 3. Prove a leased target

Start `lease_driver.py` with a new opaque id, URL, and label. Begin with
`page_context`. The driver owns the private token and window-isolated session;
never copy the token into a command or log. A healthy probe plus a failed lease
is a session/content-script problem, not a socket problem.

Always send `{"command":"closeout"}` for a created lease, even when the
operation failed, and require the id to be absent from sessions.

### 4. Inspect the failing action

Use one compact readback:

- wrong URL or missing controls: `page_context`;
- unknown selector: one fresh `snapshot`;
- console or network failure: [`devtools.md`](devtools.md);
- cursor/UI mismatch: `screenshot`, then read the image;
- isolation or cleanup mismatch: `{"type": "sessions"}`.

**Click hang / `EXTENSION_TIMEOUT` / “content script missing” after a click
(especially when a prior `page_context` in the same lease succeeded):** stop.
Before inject, poison, SPA remount, or reload theories:

1. Grep the **app** click handler for `prompt(` / `confirm(` / `alert(` (or run
   `dialog_get` if a dialog may already be open).
2. If it is a JS dialog → Comet Control `dialog_handle` (+ `promptText`); do not reload.
3. If it is an OS sheet / file chooser / system UI → `$macos-cua` via
   [`multi-agent.md`](multi-agent.md). Do not treat that as a Comet Control inject bug.
4. Only then chase content-script / scripting FIFO owners.

Do not burn repeated end-to-end owner proofs on the same inject hypothesis
without completing step 1 once. Do not add a retry loop. One fresh readback
should identify the owning layer.

## Symptom to owner

| Symptom | Likely owner | Correct response |
| --- | --- | --- |
| Socket missing or refused | Broker lifecycle | Start the broker; reload the unpacked extension only if it remains disconnected |
| Status succeeds, lease preflight fails | Session/content-script setup | Use a real `https://` URL; inspect the exact preflight error |
| Operator sees no cursor | Wrong host app, hidden window, or overlay | Focus the leased window; read a screenshot; inspect `cursor-agent.js` only if absent there |
| Cursor state resets after navigation | Injection lifecycle | Check invalidation/reinjection and identity re-apply in `service_worker.js` |
| Click lands on wrong element | Resolver or page readiness | Wait for the target, refresh one snapshot, then inspect resolver code |
| SPA shell has no content | Page readiness | Wait on a page-specific selector; do not add a fixed sleep |
| Agents collide | Missing/invalid lease identity | Require distinct ids and matching private tokens; no active-tab fallback |
| Lease vanishes | Owned-window grouping or lifecycle | Confirm owned windows are never tab-grouped |
| Extension reload rejected | Live leases exist | Close leases first; reload only from an empty inventory |
| Source edit has no effect | WIP deploy is stale | Run `scripts/sync-wip.sh`, reload extension, rerun the original proof |
| `LEASE_HELD` after driver died; `busy: true` orphan | Orphan reclaim in `sessionPreflight` / reap | Confirm `stuckBusyStale` + busy-past-ttl reap; close orphan window if targets still present; never invent a new session id |
| `already leased by another caller` / `Invalid browser lease token` after a short `python3 -c` / one-shot `comet_control_run`; `sessions` still shows the id with `busy: false` | Lease token is **process-local**. The shell exited before closeout; extension still owns the window; next process has no token | Same session id only. Start `durable_lease_controller.py` (or keep one `lease_driver` alive) for the whole campaign; wait for orphan reclaim — do **not** invent a second session id, and do not drive multi-step fills with one-shot shells |
| `EXTENSION_TIMEOUT` / “content script missing” after Seller Dispatch click | `window.prompt` freezes the content-script click reply; mislabeled as missing; reload dismisses the prompt | Comet Control owns JS prompts: `click_text` returns `dialog_opened` → batch/next `dialog_handle` + `promptText`. Never reload while a JS dialog is open. OS-native UI is `$macos-cua` ownership, not a Comet Control retry |
| Open JS prompt / hung click blocks `closeout`; `LEASE_HELD` orphan | Dialog pins the tab; dead controller cannot dismiss | `dialog_handle` (or dismiss) on the owning lease before closeout. Reclaim needs controllable `http(s)` URL (not `about:blank`). Empty inventory required before extension reload / Comet quit |
| Stale fill/locator errors for later `page_context` / `sessions` / `closeout` | `durable_lease_controller` response race | Use only `durable_lease_controller.py send` (seq-matched); never write `request.json` by hand or invent a FIFO wrapper |
| Controller remains alive through a prolonged extension/socket outage | Consecutive lease renewals cannot reach the owning runtime | The driver emits `renewal_exhausted` after three failures, attempts authenticated closeout, and exits; diagnose the broker/runtime before starting the same session id again |
| Driver dies right after `ready` | Launching shell EOF'd stdin | Prefer `durable_lease_controller.py start`; keep one process for the whole campaign |

## Repair by surface

| Surface | Owner | Validation after change |
| --- | --- | --- |
| Lease, action, resolver, screenshot | `plugin/comet_control/extension/service_worker.js` | Relevant suite plus isolation suite |
| Cursor DOM, motion, overlay | `plugin/comet_control/extension/content-scripts/cursor-agent.js` | Screenshot readback plus isolation suite |
| Loopback and socket transport | `plugin/comet_control/native/broker.py` | Broker tests plus live status |
| Tool argument mapping | `plugin/comet_control/tools.py` | Unit tests plus one live mapped action |
| Skill routing or payloads | `skills/comet-control/` | `skills/comet-control/scripts/validate.sh --strict` plus live path named by the edit |

Edit the owner, run `scripts/sync-wip.sh` for runtime code, reload the unpacked
extension only when the sessions inventory is empty, and rerun the complete
failing workflow. Do not compensate for code defects with skill prose.

## Validation matrix

Run from the repo root with the WIP socket exported.

```bash
export COMET_CONTROL_BRIDGE_SOCKET="$PWD/run/comet-control.sock"
```

| Changed behavior | Required command |
| --- | --- |
| Lease, cursor, tiling, closeout | `python3 plugin/comet_control/tests/test_multi_agent_isolation.py` |
| Console, network, navigation diagnostics | `python3 plugin/comet_control/tests/test_developer_diagnostics.py` |
| Semantic locators, CDP, dialogs, files, viewport, tabs/history | `python3 plugin/comet_control/tests/test_comet_capabilities.py` |
| Broker | `python3 -B -m unittest plugin.comet_control.tests.test_broker` |
| Orphan reclaim / content-script reinject contracts | `python3 plugin/comet_control/tests/test_source_contracts.py` |
| Durable controller sequencing | `python3 plugin/comet_control/tests/test_durable_lease_controller.py` |
| Skill files | `skills/comet-control/scripts/validate.sh --strict` |

Exit `2` from a live suite means the environment was unavailable or skipped;
it is not acceptance evidence. Lease lifecycle changes require two consecutive
exit-0 isolation runs. Cursor/UI claims additionally require reading the PNG.

## Clean closeout

After repair:

1. Rerun the original failing action, not only a local helper.
2. Read the authoritative result surface.
3. Close every lease created during diagnosis.
4. Require those ids to be absent from `{"type": "sessions"}`.
5. Report the host browser, deployed WIP path, and any warning still unexplained.

## Structured failure triage

Treat `ACTIONABILITY_TARGET_COUNT`, `ACTIONABILITY_NOT_VISIBLE`,
`ACTIONABILITY_DISABLED`, `ACTIONABILITY_NOT_EDITABLE`,
`ACTIONABILITY_UNSTABLE`, and `ACTIONABILITY_OBSCURED` as page-state evidence,
not transport timeouts. Inspect `failure_record_path`; it contains the bounded
timing, locator/ref, URLs, console/network tail, and failure-only screenshot.

`EXTENSION_DISCONNECTED` and `EXTENSION_REPLACED` invalidate the old command.
Re-read page state after reconnection; never replay or switch pages implicitly.
`BROKER_BUSY` is bounded backpressure, so wait for current work to finish.

If `loaded_build_current` fails with zero leases, reload the unpacked Comet
extension and run `scripts/ensure-wip-broker.sh start`. Do not restart while a
lease exists, and do not touch Chrome.
