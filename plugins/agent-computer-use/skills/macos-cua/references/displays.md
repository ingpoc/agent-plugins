# Displays + agent cursor

## Problem (validated 2026-07-06)

| Fact | Implication |
| --- | --- |
| AX frames use **global Quartz** coords | Secondary-display targets may have large or negative origins |
| Signed operator cursor uses global Quartz coords | It follows targets across displays without moving the physical pointer |
| Stage Manager can move only the Quartz thumbnail off-screen | Monitor proof must fall back to the exact logical AX frame |
| System Events can report `NO_WIN` while native AX still exposes the window | Placement must fall back to direct AX size/position without activating the app |
| `screencapture` omits overlay | Screenshots never show agent cursor ([cua#1902](https://github.com/trycua/cua/issues/1902)) |

## Fix contract

`click-label-pointer` follows the target element in global Quartz coordinates.
The signed operator is the only visible cursor owner; its independent panel can
cross displays without cua-driver cursor alignment.

1. Resolve the target app's live window/display.
2. Publish the global target to the signed operator.
3. Wait until `cursor_rendered_x/y` and `cursor_rendered_update_id` acknowledge
   the exact app/PID/window update; do not click on a timeout or stale/sibling
   task acknowledgement.
4. Click through the fresh AX `element_index`, leaving the hardware pointer alone.
5. For a pinned lab run, set `MACOS_CUA_DISPLAY` and move the app only.
6. `ensure-display` tries System Events first, then uses direct native AX
   size/position when System Events reports no window. It does not activate the
   app or touch the physical pointer.
7. It polls WindowServer, then accepts the largest logical AX frame only when
   its requested size/position and display match exactly. It preserves the
   divergent thumbnail under `quartz_window_after` and reports `move_method`.

Pass signal after click:

```json
"move": {
  "ok": true,
  "sync": { "ok": true, "update_id": "unique-render-id" }
}
```

The rendered coordinates must match the requested global target. The live
pointer grader additionally compares the layer-1000 Quartz panel origin with a
one-pixel tolerance.

## Detect displays

```bash
python3 $SKILL_DIR/scripts/macos-cua.py displays
```

`displays` and optional `start_session preflight:true` return one packet.
Do not attach it to every `state` / `run` observe.

| Field | Meaning |
| --- | --- |
| `display_count_active` | CG/NSScreen started and awake |
| `display_count_configured` | CG online (attached; may include asleep/clamshell) |
| `displays[]` | Active only: id, main, frame, scale. No vendor pick |
| `target_window_display` | Set when a window bounds hint is passed to `display_packet` |

Topology: 1 active = valid. 2+ active = act on the window's display. 2
configured / 1 active = do not move onto the sleeping screen. The elon
secondary-display gate is lab-only; session preflight must not require it.

Set `MACOS_CUA_DISPLAY` to any unique display-name substring. If unset,
`ensure-display` selects the first secondary display (or the main display when
only one is connected) and preserves the current window size unless explicit
width/height are supplied. App-specific placement remains repository-owned.

## Agent vs user mouse

The signed operator panel moves the **agent cursor only**; AX element clicks do
not move the user's pointer. Never `cliclick` in this workflow.

Raw-HID-only interactions are out of scope: use the app's automation/API path.
AX actions do not move the user's pointer. Coordinate actions use the single
system pointer; `--preserve-pointer` restores its position but is not isolation.

**Single-pointer contract:** `click-label-pointer` publishes the target to the
signed operator, waits for its 0.32 s visual glide and exact rendered-position
acknowledgement, then clicks through AX `element_index`.

The explicit `cursor` CLI and `displays.py` overlay helpers remain compatibility
diagnostics for existing external scripts. They are not part of the primary
pointer-action path and must not be used as visibility proof.

`MACOS_CUA_PIXEL_CLICK=1` (default **0**) explicitly permits desktop pixel
fallback for custom-drawn surfaces with no AX tree. It is user-interruptive;
there is no silent vision-label-to-pixel escalation.

## Cursor visibility

| Setting | Why |
| --- | --- |
| Shared Hermes SVG | Visual source; rasterized to the signed operator's cache |
| Signed operator overlay | Sole visible owner: 28 px pointer, CSS-equivalent glow, label, and glide |
| Render acknowledgement | Blocks AX click until the visible panel reaches the target |

The normal path materializes `~/.cache/macos-cua/hermes-pointer.png` from the
shared Hermes SVG. The signed operator adds the browser-rendered glow, motion,
and harness label. Treat driver overlay state as diagnostics, not visibility
proof.

## Do not

- Prepare, align, show, or move the cua-driver cursor before a primary pointer action.
- Use `click-label` when operator must **see** the pointer.
- Accept `publish` as movement proof; require the operator's rendered acknowledgement.
