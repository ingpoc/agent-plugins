# Observe feedback (comet-control efficiency)

How lease `send` / `actions[]` speak to the model. **One path:** reuse the last
`page_context` / action result; call a fresh observe only for discovery or after a miss.
Never dump full DOM snapshots or screenshot-every-step into the agent loop.

## Success (compact)

Healthy batches return small structured results — prefer these over extra observes:

| Action | Typical payload | Notes |
| --- | --- | --- |
| `page_context` | url, title, headings≤8, buttons≤15, inputs≤10, console counts | Top frame only; ad iframes excluded |
| `click_*` / locator | type + target evidence | Reuse; do not re-`page_context` unless URL/controls must change |
| `screenshot` | file path (+ optional base64) | Only for visual claims; read the file |
| `activate_tab` / `focus_tab` (own tab) | `{activated:true, tabId}` | Foreign tab → typed failure below |

Verified success is the action result + optional compact `page_context` in the **same**
`actions[]` batch — not a follow-up observe turn.

## Failure taxonomy

Failures set `error_code` (+ optional `failure_record` path). Treat codes as page/lease
evidence, not transport noise:

| `error_code` | Meaning | Agent response |
| --- | --- | --- |
| `LEASE_TAB_SCOPED` | Other-tab activate/focus rejected | Do not retry; use AX/CUA for other-window GSI |
| `ACTIONABILITY_*` / `ELEMENT_NOT_FOUND` | Locator miss / not visible / obscured | One fresh `page_context` or snapshot; no remint |
| `SCREENSHOT_TIMEOUT` | captureVisibleTab bounded (~8s) | Skip opening shot; retry later or CDP after `viewport_set` |
| `EXTENSION_TIMEOUT` | Extension did not answer in time | Await / diagnose; no remint; check JS dialog first |
| `EXTENSION_NOT_CONNECTED` / `EXTENSION_DISCONNECTED` | Transport | Probe + recovery; invalidate old command |
| `LEASE_HELD` / invalid token | Orphan / process-local token lost | Same session + durable controller; wait reclaim |
| `CS_DEAD_AFTER_REMOUNT` | SPA killed content script | Nav-only `reload_page`/`goto` then re-read |
| `CUA_RUNTIME_CLAIMED` | macos-cua holds Comet PID | Wait / handoff per native-coexistence |
| `BROKER_BUSY` | Backpressure | Wait for current work |
| `VISUAL_FOCUS_*` | Focus lock contention | Retry bounded; do not open second lease |

Inspect `failure_record` when present (timing, locator, console/network tail, failure-only screenshot).
Do not replay mutations after `EXTENSION_DISCONNECTED` without a fresh read.

## Loop bans (agent wall-clock)

1. Observe → observe → observe with no mutation.
2. `page_context` + full-body `evaluate` + `screenshot` every step.
3. Tight poll while OS/CUA owns the picker (≤1 `page_context` / 30s).
4. Dual Browser Use + `send`.
5. Remint on timeout / `LEASE_TAB_SCOPED`.

See [`speed-bar.md`](speed-bar.md).
