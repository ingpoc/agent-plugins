# Troubleshooting

MCP path only: `start_session` → **act-first** batched `act`/`plan` per app →
`state` only for discovery or after an `act` miss → `verify` only when
`act.verified` is false → `end_session`. That is the Computer Use middle.
`workflow.py preflight` is once before it; `workflow.py closeout` is once after
it. One Computer Use server. No `list_apps`. No raw 54-tool catalog. WhatsApp
send/attach → `$whatsapp`, not this skill.

After probe apps you launched, quit them. Do not quit Cursor, the user's
messenger, or their browser session.
Recover a wedged session with `end_session` then `start_session`.

| Symptom | Fix |
| --- | --- |
| Tree is Apple-menu items | Do not use menu bar as snapshot root. Open sheet/popover/dialog, else app menu, else one window |
| Label miss after prior click | Current-tree first (in-place retitles, `Clear`=`All Clear`). One fresh `state` only if that tree still has no match. Never seed a postcondition `expect` from a pre-mutation tree |
| `perform_action` hangs until MCP timeout | AX messaging timeout is 1.5s; fail closed with `ax_timeout`. Do not retry until the MCP budget |
| Glide misses sheet/popover control | Act on the open sheet/popover root, not the parent window alone |
| Background `key` misses | Retry once when `escalation.recommended` is `foreground` or `off_space_or_ax_unresolved` |
| Chrome / web UI | Browser MCP — not this skill |
| Clicks do nothing though Cursor has Accessibility | Grant TCC to **Cua Driver.app** (`cua-driver permissions grant`) |
| Glide hits old screen point after window moved | Omit stale `cursor_screen_x/y`; use window-local coords |
| Single monitor / asleep secondary | Valid. Do not force a second display |
| `type-text` ok but field empty (Catalyst) | Treat as incomplete; prove with UI outcome (e.g. Voice→Send), not `typed_path` |
| Dispatch `ok` / `accepted` but UI unchanged | Fresh `state` or plan `expect` is proof — never trust dispatch alone |
| App `unix id = <old pid>` after quit/relaunch | `clear_resolution_cache` then resolve a live PID; do not reopen a dead process |
| README scores from `--repeat 1` or a failed suite | Refuse. Only a passing warm `--repeat 5 --rate` may refresh README |
| Extra `state`/`verify` hops; same-app unbatched `act`s | Two wall clocks. Encode `fast_path.grade_tool_trace` so the old hop sequence fails | Chat-only “act-first next time” |
| MCP pays process startup on every state/act | Run `bench_mcp_runtime.py`; production telemetry must keep `cli_invocations=0`. Per-call `macos-cua.py` is bench/debug only |
| State floods context | Compact + `query` / `diff`; raise `--max` only when Compose/Send/modals truncated (often 80) |
| Open sheet: no filename via query | Sheet is the root; do not press Open blind. File attach recipes stay in the target app skill |
| Unlabeled AXRow/AXCell not clickable | Owner attaches static child text to the row; do not `bring_to_front`+sleep |
| Desktop widgets / Notification Center | Not ordinary AX windows — see `special-surfaces.md` |
| Pixel / coordinate click wanted | Last resort after AX miss, and only with `MACOS_CUA_PIXEL_CLICK=1`. Default observe stays AX-only |
| Clipboard paste for background type | Refuse — AX / `type-text` only |
| Two cursors or invisible Hermes cursor | Prefer labeled glide path; `end_session` / operator restart if overlay wedged — see `operator-ui.md` |
| Wrong display / offset pointer | Window-local actions; `displays.md` |
| Screenshot black / occluded | Need visual proof → foreground once; otherwise AX-only compact state |
| Custom-drawn field eats shortcuts, not text | Stop. Use the app’s native API/automation or report the gap |
| Hold needed (time-based key) | `hold-key` with duration; not repeated taps |

Maintainer bench/revert rules: plugin `README.md` + `entry-contract.json` /
`hardening-contract.json` — not this file.
