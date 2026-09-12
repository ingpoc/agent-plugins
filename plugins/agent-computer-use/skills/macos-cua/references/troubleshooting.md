# Troubleshooting

Agent path (plugin ≥ 0.2.18): **`state` + `act` only**. Act-first batched
`act`/`plan` per app; `state` only for discovery or after an act miss. Do **not**
call `start_session` / `verify` / `end_session`, `list_apps`, or raw cua-driver.

On success, reuse the **compact AX delta** from `act`. On failure, read
`error_type` + nearby context — see [`observe-feedback.md`](observe-feedback.md).
Do not dump full AX trees into chat.

After probe apps you launched, quit them. Do not quit Cursor, the user's
messenger, or their browser session. Overlay wedge → restart CUAService /
operator (see `operator-ui.md`), not a session tool.

| Symptom | Fix |
| --- | --- |
| Tree is Apple-menu items | Do not use menu bar as snapshot root. Open sheet/popover/dialog, else app menu, else one window |
| Label miss after prior click | Current-tree first (in-place retitles, `Clear`=`All Clear`). One fresh `state` only if that tree still has no match. Never seed a postcondition `expect` from a pre-mutation tree |
| `perform_action` hangs until MCP timeout | AX messaging timeout is 1.5s; fail closed with `ax_timeout`. Do not retry until the MCP budget |
| Glide misses sheet/popover control | Act on the open sheet/popover root, not the parent window alone |
| Background `key` misses | Retry once when `escalation.recommended` is `foreground` or `off_space_or_ax_unresolved` |
| Chrome / web UI | Browser MCP — not this skill |
| Clicks do nothing though Cursor has Accessibility | Grant Accessibility + Screen Recording to **macos-cua Service** / **CUAService**, then relaunch the service |
| Glide hits old screen point after window moved | Omit stale `cursor_screen_x/y`; use window-local coords |
| Single monitor / asleep secondary | Valid. Do not force a second display |
| `type-text` ok but field empty (Catalyst) | Treat as incomplete; prove with UI outcome, not `typed_path` |
| Dispatch `ok` / `accepted` but UI unchanged | `act.verified` + expect / failure taxonomy is proof — never trust dispatch alone |
| Wrong document typed (multi-window) | Service prefers **AX focused** window (0.2.18+). If `state` title is wrong, stop — do not mutate |
| `error_type: expect_unverified` / `target_missing` / `stale_id` | See [`observe-feedback.md`](observe-feedback.md); one fresh `state` by label, then retry |
| App `unix id = <old pid>` after quit/relaunch | Clear resolution cache / relaunch CUAService; do not reopen a dead process |
| README scores from `--repeat 1` or a failed suite | Refuse. Only a passing warm `--repeat 5 --rate` may refresh README |
| Extra `state` hops; same-app unbatched `act`s | Two wall clocks. Encode `fast_path.grade_tool_trace` so the old hop sequence fails |
| MCP pays process startup on every state/act | Run `bench_mcp_runtime.py`; production telemetry must keep `cli_invocations=0`. Per-call `macos-cua.py` is bench/debug only |
| State floods context | Compact `state` + `query` / `diff`; prefer act deltas. Raise `--max` only when Compose/Send/modals truncated (often 80) |
| Open sheet: no filename via query | Sheet is the root; do not press Open blind. File attach recipes stay in the target app skill |
| Unlabeled AXRow/AXCell not clickable | Owner attaches static child text to the row; do not `bring_to_front`+sleep |
| Desktop widgets / Notification Center | Not ordinary AX windows — see `special-surfaces.md` |
| Pixel / coordinate click wanted | Last resort after AX miss, and only with `MACOS_CUA_PIXEL_CLICK=1`. Default observe stays AX-only |
| Clipboard paste for background type | Refuse — AX / `type-text` only |
| Two cursors or invisible Hermes cursor | Prefer labeled glide path; restart operator if overlay wedged — see `operator-ui.md` |
| Wrong display / offset pointer | Window-local actions; `displays.md` |
| Screenshot black / occluded | Need visual proof → foreground once; otherwise AX-only compact state |
| Custom-drawn field eats shortcuts, not text | Stop. Use the app’s native API/automation or report the gap |
| Hold needed (time-based key) | `hold-key` with duration; not repeated taps |

Maintainer bench/revert rules: plugin `README.md` + `entry-contract.json` /
`hardening-contract.json` — not this file.
