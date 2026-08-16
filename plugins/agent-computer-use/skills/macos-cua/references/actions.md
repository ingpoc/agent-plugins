# State and actions

CLI verbs (`state`, `click-label`, `run`, `key`, …) live here. SKILL.md
only routes; do not copy this table into the always-loaded skill.

## Observe

```bash
SKILL=$SKILL_DIR
python3 "$SKILL/scripts/macos-cua.py" state Finder --compact --query Downloads
python3 "$SKILL/scripts/macos-cua.py" state Calculator --compact
python3 "$SKILL/scripts/macos-cua.py" state pid:12345 --compact
python3 "$SKILL/scripts/macos-cua.py" state Calculator --compact --no-screenshot
python3 "$SKILL/scripts/macos-cua.py" apps --query ScreenContinuity
python3 "$SKILL/scripts/macos-cua.py" click-point Calculator 40 40 --button right
python3 "$SKILL/scripts/macos-cua.py" click-desktop 1200 400 --button right
```

`state --compact` returns indexed AX `text`, `snapshot_id`, and
`screenshot.path` while omitting the duplicate structured element array. Omit
`--compact` only when element tokens/fields are needed. `--no-screenshot` is an
intentional AX-only observation.
Explicit relative `--screenshot` and `--debug-image` paths are normalized to
absolute paths; cua-driver silently ignores relative output paths when called
directly.
Its normal view omits hidden descendants of closed macOS menus; opening a menu
causes its framed, visible items to appear. `hidden_element_count` reports the
suppressed noise, and `--query` narrows only the rendered text while preserving
the fresh snapshot indices in `elements`.
Use `pid:<number>` when two running instances share a bundle/name; resolution,
focus, and window ownership remain pinned to that exact process.
After a pointer action, `screenshot.path` is a `*-cursor.png` composited with
the full Hermes pointer plus a cyan target ring at the exact action point;
`screenshot.raw_path` preserves the unmodified driver
capture and `screenshot.cursor_included` is the deterministic pass signal.
Background AX/capture is the non-interruptive default and may show occluded
screenshot regions. Use `state … --foreground` only when an explicit visual
proof requires fronting the target and interrupting the user's current flow.

## Action addressing

| Address | Prefer when | Rule |
| --- | --- | --- |
| `label` | Human-visible control text exists | Re-resolved from fresh state and constrained to actionable AX roles; a matching window/app title is never selected |
| `element` | Acting immediately after a state read | Snapshot-scoped; never reuse after another snapshot |
| `x`,`y` | Irreducible canvas/custom-drawn surface | Explicit last resort. Uses CGEvent and the single system pointer; add `--preserve-pointer` to restore position afterward, without claiming isolation |

## Asserted plan

```json
{
  "pointer": true,
  "capture": "failures",
  "output": "compact",
  "max_elements": 120,
  "actions": [
    {"action": "click", "label": "7"},
    {"action": "click", "label": "Multiply"},
    {"action": "click", "label": "8"},
    {"action": "click", "label": "Equals", "expect": {"text": "56"}}
  ],
  "expect": {"text": "56"}
}
```

Run with `run <app> @plan.json` or pass the JSON string directly. The default
`pointer: true` makes label- and index-addressed clicks and text-field focus
human-legible through the signed software cursor without moving the user's
hardware pointer. `double_click`, `perform_action`, and `right_click` use the
same glide when `pointer` is true. Set `pointer: false` only for an explicitly approved visually silent
diagnostic. Watched runs fail if the cursor does not reach the target or a
pointer step exceeds `max_step_ms`. Set
`capture` to `always`, `failures`, or `never`. The PiP places the same cursor
asset from normalized window coordinates, independent of monitor origin or
Retina scale. Successful and failed plans are compact by default; failures keep
the failed step, error, assertions, and proof path without the complete element
array. Set `"output":"full"` only while debugging.

Mutating plans fail before dispatch unless a top-level `expect`, per-step
expectations, or explicit `allow_unverified:true` is present. A successful
postcondition snapshot is reused by the next addressed action while it remains
fresh. An accepted click without a per-step `expect` keeps that tree; a label
miss takes one fresh snapshot. External/user changes require a new `state`. An allowed unverified run
returns `accepted:true`, `verified:false`, and `ok:false`—it never proves the job.

### Actions

| `action` | Fields |
| --- | --- |
| `click` | `label` or `element` or `x`,`y`; optional `button`,`count`,`delivery_mode`,`debug_image_out`,`preserve_pointer` (coordinate path only) |
| `double_click` | `label` or `element` or `x`,`y`; optional `delivery_mode: foreground` |
| `drag` | `from_x`,`from_y`,`to_x`,`to_y`; optional `delivery_mode`,`duration_ms`,`steps`; native AX sliders keep the user cursor stationary, otherwise macos-cua visibly mirrors an explicit foreground fallback and reports `system_cursor_used:true`; accepted dispatch has `effect:unverified` until an expectation passes |
| `perform_action` | `label`/`element`; `name` defaults to `press` |
| `type` | `text`; optional `label`/`element`/`x`,`y` (omitted target = focused UI), `allow_newline` (default false because newline can submit/send); labeled typing visibly focuses the field first |
| `set_value` | `label`/`element`, `value` |
| `select_text` | `label`/`element`, `text`; optional `prefix`,`suffix`,`selection_type` |
| `key` | `keys` as `"cmd+s"` or `["cmd","s"]`; optional `delivery_mode: foreground | system_events` |
| `scroll` | `direction`,`amount`,`by`; optional `label`/`element`/`x`,`y`,`delivery_mode: foreground` |
| `right_click` | `label` or `element` |
| `wait` | `seconds` |
| `state` | optional `max_elements` |

Element-addressed `double_click` uses two native AX presses for an `AXButton`.
Text areas and custom surfaces keep the pixel click-count path so word selection
and rendered direct manipulation preserve native double-click semantics.

### Expectations

Use one object/string or a list. Supported assertions:

- `{"text":"Saved"}` / `{"not_text":"Error"}`
- `{"text":"New chat","role":"AXHeading"}` — role-scopes the match
- `{"label":"Profile"}`
- `{"element_count_min":1}`
- `{"value":{"label":"Name","equals":"Ada"}}`
- `{"value":{"label":"Status","contains":"Ready"}}`

Per-action `expect` polls fresh state until success or timeout. Top-level
`expect` owns the final pass. Matches on the acted element are ignored, so a
label cannot prove itself. A pre-existing sidebar string is not disclose
proof; require a role that only the new surface has. Compact output keeps
only expect-matching lines, omits acted controls, and caps at 12 lines.
`"ok": true` means every action was accepted and all requested assertions
passed. Native AX sets a 1.5s messaging timeout on the app and the press
target so a stuck app fails closed instead of hanging ~30s. WhatsApp
New Chat: Escape if the popover is already open, then `perform_action`
and expect `{"text":"New chat","role":"AXHeading"}`. No pixel-click
fallback.

`select_text` supports `text`, `cursor_before`, and `cursor_after`. Prefix and
suffix disambiguate repeated matches; the wrapper verifies the native
`AXSelectedTextRange` readback before returning success.

## Keyboard duration

Use `key` for discrete presses and shortcuts. Use `hold-key <app> <key>
--duration <seconds>` for continuous movement/navigation; it guarantees key-up
in a `finally` path and bounds duration to 0.05–10 seconds. Add `--foreground`
only after the target app has yielded focus and a raw-input surface ignores
PID-targeted delivery; this fronts the resolved window and posts synthetic
keyboard events through the system input tap without moving the pointer. Set
`MACOS_CUA_OPERATOR_UI=0` when the native menu-bar/PiP process must remain
stopped. `hold-key` itself does not start operator telemetry.
Use explicit `delivery_mode: system_events` (CLI: `key --system-events`) only
when an exact-PID native modal ignores accepted foreground CGEvents. It never
proves effect by itself; follow with fresh state or a plan expectation.

## Raw 3D mouse look

The agent cursor is a software overlay and does not impersonate raw HID mouse
motion. If a game or 3D viewport requires captured mouse-look, use its native
automation/API path supplied by the target application or report the gap.
Never warp or move the user's hardware pointer.

## Display framing

`ensure-display` verifies both monitor and requested frame. If the app is
already on the target display but its frame differs, it selects the largest app
window, resizes before positioning, waits for WindowServer, and verifies the
Quartz result. When Stage Manager exposes only an off-screen thumbnail, exact
logical AX bounds are the accepted fallback; the result says
`verification: accessibility-logical` and retains `quartz_window_after` for
diagnosis. A 32 px position tolerance permits macOS title-bar clamping; width
and height remain exact within 3 px. A clean rerun returns `moved: false`.

For a same-process toast/modal whose pixels are captured separately, pass its
Quartz ID to `click-point --window-id ID`. The CLI proves the ID belongs to the
resolved app PID before dispatch; this does not make a background custom-drawn
surface accept input, so keep `effect: unverifiable` until fresh state proves it.
An explicit modal ID disables the main-window Stage Manager correction. For the
resolved main window, a Quartz/AX frame mismatch triggers a fresh screenshot,
maps its raw pixels through the logical AX frame, and dispatches the resulting
screen point to the target PID through CGEvent. This can move/use the system
pointer; add `--preserve-pointer` to restore position afterward. Stale points
outside that fresh screenshot fail closed. `--foreground` prepares the window
before this corrected screen-coordinate dispatch.

## Recovery ladder

1. Fresh `state`; do not reuse indices.
2. AX press/set/select or targeted keyboard navigation without foregrounding.
3. Prefer the app's API/automation; browser pages use DOM/CDP, not native pixels.
4. If AX is incomplete, use fresh screenshot coordinates explicitly. Prefer
   `--preserve-pointer` for testing; the click remains user-interruptive.
5. Retry only the failed action with foreground delivery when interruption is
   accepted and background delivery cannot work.
6. On failure, inspect the capture and load troubleshooting.

Do not pre-scale screenshot coordinates. If a background pixel click misses a
custom surface, retry that click with `delivery_mode: "foreground"` and set
`debug_image_out` to capture the driver's red-crosshair proof image.
Point input is snapshot-bound: the operator proof must be no older than 30
seconds and exact window geometry must still match. Re-run `state` after a
resize, display move, or stale-proof error.
