# Displays + agent cursor

## Problem (validated 2026-07-06)

| Fact | Implication |
| --- | --- |
| AX frames use **global Quartz** coords | Secondary-display targets may have large or negative origins |
| CUAService cursor uses global Quartz coords | It follows targets across displays without moving the physical pointer |
| Stage Manager can move only the Quartz thumbnail off-screen | Monitor proof must fall back to the exact logical AX frame |
| System Events can report `NO_WIN` while native AX still exposes the window | Placement must fall back to direct AX size/position without activating the app |
| `screencapture` omits overlay | Screenshots never show agent cursor ([cua#1902](https://github.com/trycua/cua/issues/1902)) |

## Fix contract

CUAService owns the visible cursor and handles movement and target landing for pointer actions. It resolves the live target window and fails closed if the target cannot be reached. AX actions leave the physical pointer alone.

For a pinned lab run, set `MACOS_CUA_DISPLAY` and move the app only. `ensure-display` uses native AX if System Events cannot resolve the window; it verifies placement against the active display.

## Detect displays

```bash
python3 $SKILL_DIR/scripts/macos-cua.py displays
```

`displays` and `workflow.py preflight` return one packet.
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

CUAService moves the agent cursor. AX element clicks do not move the user's pointer. Coordinate actions can use the system pointer and are user-interruptive. `--preserve-pointer` restores its position but does not isolate the action.

## Cursor visibility

CUAService owns the overlay, movement, and landing gate. Check live captures when visual proof is required.

## Do not

- Treat action dispatch alone as proof of the intended outcome.
- Use stale window geometry for coordinate actions.
