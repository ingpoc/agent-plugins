# Comet Control operation reference

Load this file for routine bridge actions. Leased window sessions are required;
[`multi-agent.md`](multi-agent.md) owns the socket client and complete lease
protocol. Load
[`advanced-capabilities.md`](advanced-capabilities.md) only for its named
capabilities.

The plugin socket is `run/comet-control.sock` under the plugin root. Override with
`COMET_CONTROL_BRIDGE_SOCKET` only when pointing at that same plugin tree.

Screenshots land under `run/cache/comet-control/`. The bridge must not create
legacy artifacts under `~/.comet-control`.

## Token-private testing campaign

Use one driver for the entire browser task or testing campaign: setup, planned
cases, diagnosis, retests, and proof. It retains the opaque lease token inside
one process, redacts private values, and silently renews the same leased window
while alive. TTL is crash-cleanup grace, not a reason to rotate leases:

```bash
python3 skills/comet-control/scripts/lease_driver.py \
  --session-id "agent-<uuid>" --label "Agent A" \
  --url "https://example.com/"
```

Send one newline-delimited JSON command per turn:

```json
{"actions":[{"type":"page_context"},{"type":"screenshot","format":"png"}]}
{"command":"sessions"}
{"command":"closeout"}
```

Keep that driver process alive between commands and tests. Do not close and
reopen a window for each assertion, screen, or retest. Request logical closeout
once only after the full campaign completes, is cancelled, or becomes genuinely
unrecoverable. The driver may retry that same authenticated cleanup internally
on a retryable removal failure; it never opens a replacement lease.

The driver disables canonical PTY buffering, so a long NDJSON form command is
received intact instead of being truncated at the terminal line limit. Keep
form mutations outcome-sized and omit redundant readbacks, but do not split a
coherent step merely to work around PTY input length.

When the Codex wrapper reports `Script running with cell ID ...` after a driver
write, the command is still pending: resume that exact cell with `wait`. Do not
resend the JSON, open another lease, or classify the absence of immediate output
as a browser failure.

Never print, persist, or ask the reviewer to copy the private token. If a run
fails, the driver automatically returns the filtered session inventory and any
lease-removal tombstone before the reviewer freezes its verdict.

A command failure is not a campaign boundary. Diagnose and retry through the
same driver and leased window; never silently create a replacement lease.

Viewport screenshots abort after ~8s (`SCREENSHOT_TIMEOUT`). A hung `captureVisibleTab` must not block the lease. Skip an opening screenshot on first paint if you only need URL/title.
A visual claim requires a `screenshot` action and an actual read of the returned
image file. `cursor_status` alone is state, not operator-visible proof.
Sticky unique names click the card rect, not the stuck inset. Checkbox/radio activation uses the native input click, not only a synthetic MouseEvent.
Missing locators fail fast (`ACTIONABILITY_*` / `ELEMENT_NOT_FOUND`) and do not reload the leased tab.
In-page commands and screenshots must not foreground Comet or steal the human's
macOS key focus. A true OS sheet may be foregrounded only through the CUA handoff.

## Routine action map

Actions below belong inside one `run.actions` list.

| Need | Action | Notes |
| --- | --- | --- |
| Compact orientation | `page_context` | Default read of the **top frame** only; URL, headings, controls, compact console counts. Ad/Twitter iframes are not the page. |
| Full visible text | `text` | Articles, tables, or long form content |
| Element inventory | `snapshot` | Use only when a selector is unknown |
| Navigate | `goto` | Pair with `wait_for_selector` or `wait_for_url_change` |
| Wait for UI | `wait_for_selector` | Prefer an observable element over a fixed delay |
| Click by label | `click_text` | Preferred when text is unique; unique sticky names use the card rect, not the stuck inset; header chips skipped when an in-page match exists; checkbox/radio need native `HTMLElement.click()`, not only synthetic MouseEvent; may return `dialog_opened` |
| Click by CSS | `click_selector` | Use when text is missing or ambiguous; searches open shadow roots; 16px icons count if the hit is a descendant or button ancestor |
| JS dialog | `dialog_handle` | Comet Control owns `alert`/`confirm`/`prompt`; batch after click with `promptText` when needed — see [`advanced-capabilities.md`](advanced-capabilities.md). OS sheets → `$macos-cua` |
| Fill input | `fill_selector` | Verify the value before submit |
| Visual proof | `screenshot` | Returns a file path; read the file |
| Region image | `zoom` | Use only for a small region; bytes are inline |
| Page diagnostics | `console_tail`, network actions | Follow [`devtools.md`](devtools.md) |
| Read-only expression | `evaluate` | Never click, submit, or mutate with it |

Common payloads:

```python
{"type": "page_context"}
{"type": "text"}
{"type": "snapshot"}
{"type": "goto", "url": "https://example.com/"}
{"type": "wait_for_selector", "selector": "main", "timeout": 5000}
{"type": "wait_for_url_change", "from_url": "https://example.com/login",
 "timeout": 5000}
{"type": "click_text", "text": "Submit"}
{"type": "click_selector", "selector": "#submit"}
{"type": "fill_selector", "selector": "#email", "value": "user@example.com"}
{"type": "screenshot", "format": "png"}
{"type": "zoom", "x0": 0, "y0": 0, "x1": 800, "y1": 300}
{"type": "evaluate", "expression": "document.title"}
```

Locator clicks fail closed when layout movement or an overlay changes the hit
target during the visible cursor glide. The bridge re-resolves once and reports
`retried: true`; a continuously moving, detached, hidden, or occluded target is
an action failure, never permission to click the element now under stale coordinates.

## Cursor actions

Semantic clicks still move the labeled cursor. Use coordinate actions only for
canvas, drag, hover, or other geometry-dependent interaction.

```python
{"type": "cursor_move", "x": 400, "y": 200}
{"type": "cursor_click"}
{"type": "cursor_right_click"}
{"type": "cursor_double_click"}
{"type": "cursor_triple_click"}
{"type": "cursor_type", "text": "hello"}
{"type": "cursor_key", "key": "Enter", "modifiers": []}
{"type": "cursor_drag", "x": 600, "y": 300, "duration": 500}
{"type": "cursor_scroll", "deltaX": 0, "deltaY": 300}
{"type": "cursor_status"}
```

For keyboard-focus evidence, put a short renderer settle between the native key
and the readback in the same run. Use at least `300` ms before `screenshot`,
`page_context`, or the next focus judgment:

```json
{"actions":[{"type":"cursor_key","key":"Tab","modifiers":[]},{"type":"wait","ms":300},{"type":"screenshot","format":"png"}]}
```

An immediate screenshot may still show the previous focus ring even though the
native key dispatch succeeded. Do not repeat the key or report a keyboard
failure until the settled readback is captured.

Do not add `cursor_hide` or park the cursor at a corner during normal agent
work. Leave it on the last meaningful target so the operator can read intent.

## Reliable task slice

Orient once, batch the mutation with an observable wait, then read back the
result. Do not repeat `page_context` or take screenshots between every action.

```json
{"actions":[{"type":"page_context"},{"type":"click_text","text":"Save"},{"type":"wait_for_selector","selector":".saved","timeout":5000},{"type":"page_context"},{"type":"screenshot","format":"png"}]}
```

If a click target is not yet rendered, put `wait_for_selector` immediately
before the click. If a selector fails, refresh `snapshot` once before deciding
the selector is wrong. Repeated blind retries mask the owning defect.

## Failure boundary

On a socket drop, record the failed action, check `run/comet-control.sock`, and follow
[`optimize.md`](optimize.md). If the extension was reloaded, start a new lease;
old tokens and selectors are stale.

## Closeout

Leased session:

1. Add `screenshot` only when the result includes a UI claim; read the file.
2. After all setup, tests, retests, and proof, send `{"command":"closeout"}`
   once for the campaign driver.
3. Query sessions and require those ids to be absent.
4. Report the host browser, final URL if relevant, and clean lease state.

## Operator pause and stale-state rules

The popup Pause control is the emergency stop. While paused, active runs are
cancelled and new browser mutations return `CONTROL_PAUSED`; status, renewal,
closeout, and native-control release remain available for safe cleanup.

After `EXTENSION_DISCONNECTED`, `EXTENSION_REPLACED`, navigation, or a changed
`page_revision`, discard prior refs and re-read `page_context`/`snapshot` on the
same leased tab. Never reuse coordinates or fall back to another tab.
