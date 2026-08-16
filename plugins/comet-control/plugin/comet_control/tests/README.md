# comet-control tests

Live end-to-end regression suite for Comet Control. Drives a real Comet window
and asserts real outcomes (URL / DOM / cursor position).

## test_comet_capabilities.py

Proves semantic/frame locators, safe CDP commands and filtered events, all four
JavaScript dialog classes, text and binary clipboard round-trips, local file
upload, visible download, page asset inventory/bundle, responsive viewport,
focused history, and read-only user-tab inventory against the deployed bridge.

The suite restores clipboard state, removes downloaded artifacts and temporary
files, closes its tabs/windows, and verifies that no parity lease remains.

## test_multi_agent_isolation.py

Guards the concurrent-agent lifecycle contract: two raw bridge clients receive
separate Comet windows and tabs in the loaded profile, retain their own
navigation and click targets, show distinct labeled cursors, and close only their
own browser state. It also proves that authenticated renewal preserves the same
window/tab beyond the original TTL and that the lease expires only after renewal
stops.

The suite is self-contained. It starts a fixture on an ephemeral loopback port,
sets and then clears a temporary profile cookie, and verifies in `finally` that
its sessions, fixture port, and any extra bridge socket are gone. It does not
depend on an application server.

Negative contracts are part of the suite: missing session identity, invalid
timeouts/TTLs, tokenless or wrong-token renewal/reuse, real TTL expiry after
renewal stops, token disclosure, cross-session tab targeting, and reload with
live leases must all be rejected.
The broker socket must be user-private, and both agents must return materialized
screenshot proofs for visual inspection.

**Prerequisites**
- Source synced, logged-in Comet runtime launched, and read-only probe returning
  `ready: true`.

**Run**
```bash
COMET_CONTROL_BRIDGE_SOCKET="$PWD/run/comet-control.sock" \
  python3 plugin/comet_control/tests/test_multi_agent_isolation.py
```

**Exit codes:** `0` all assertions and cleanup pass · `1` regression or cleanup
failure · `2` bridge unavailable (skipped).

Run this suite twice consecutively before treating a session lifecycle change as
validated. Both runs must exit `0`; exit `2` is not acceptance evidence.

`test_lease_driver.py` separately proves one preflight and one closeout around
multiple campaign commands, silent automatic renewal, token redaction, renewal
failure/recovery, and closeout ordering against an in-process fake broker.

The suite also creates 2, 3, and 4 leased windows. It verifies a shared
secondary display when one exists, exact half/corner/quadrant slots, actual
macOS bounds within tolerance, no overlap, and re-tiling as leases close.

## test_developer_diagnostics.py

Proves the progressive diagnostics contract against a local fixture:

- normal `page_context` leaves network capture off;
- console `levels`/`filter`/`limit` return bounded slices;
- opt-in capture records HTTP 503 and aborted-request failures;
- `network_summary` stays compact and `network_errors` owns detail;
- clear starts a clean diagnostic slice;
- back, forward, and reload preserve the leased target.

Run after console, CDP, navigation, or network-diagnostic changes:

```bash
COMET_CONTROL_BRIDGE_SOCKET="$PWD/run/comet-control.sock" \
  python3 plugin/comet_control/tests/test_developer_diagnostics.py
```

## test_dashboard_interactions.py

Guards the three click-reliability defects fixed 2026-05-28 (see
`../../../comet-control/references/agent-handbook.md` → cursor and resolver invariants):
exact+visible text matching, occlusion rejection, and cursor motion that lands on
target even when Comet is not the foreground window. The suite owns one isolated
leased window, sends every command with its session ID and private lease token,
and verifies closeout in all exit paths.

**Prerequisites**
- Source synced, logged-in Comet runtime launched, and probe returning `ready: true`.
- Compatible dashboard at `COMET_CONTROL_TEST_URL` (default `http://localhost:9876` — the
  Agent Builder dashboard: nav with Board/Metrics/Memory/Settings + a
  `memory-search` input). Incompatible dashboard → suite skips.

**Run**
```bash
COMET_CONTROL_BRIDGE_SOCKET="$PWD/run/comet-control.sock" \
  python3 plugin/comet_control/tests/test_dashboard_interactions.py
# or against another target:
COMET_CONTROL_TEST_URL=http://localhost:3000 python3 .../test_dashboard_interactions.py
```

**Exit codes:** `0` all pass · `1` a real regression · `2` env unavailable/incompatible (skipped).

Run after any change to `service_worker.js` (resolvers) or `cursor-agent.js` (motion).
