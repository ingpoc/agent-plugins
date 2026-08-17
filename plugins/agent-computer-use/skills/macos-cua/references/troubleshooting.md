# Troubleshooting

MCP path only: `start_session` → compact `state` → `act` / asserted plan →
`verify` → `end_session`. One Computer Use server. No `list_apps`. No raw
54-tool catalog. WhatsApp send/attach → `$whatsapp`, not this skill.

After probe apps (Calculator, Dictionary, Stickies, extra TextEdit/Preview),
quit them. Do not quit Cursor, WhatsApp, or the user’s browser session.
Recover a wedged session with `end_session` then `start_session`.

| Symptom | Fix |
| --- | --- |
| Tree is Apple-menu items | Do not use menu bar as snapshot root. Open sheet/popover/dialog, else app menu, else one window |
| Label miss after prior click in same plan | Reuse postcondition tree; refresh only on miss. Never seed a postcondition `expect` from a pre-mutation tree |
| Glide misses sheet/popover control | Act on the open sheet/popover root, not the parent window alone |
| Background `key` misses | Retry once when `escalation.recommended` is `foreground` or `off_space_or_ax_unresolved` |
| Chrome / web UI | Browser MCP — not this skill |
| Clicks do nothing though Cursor has Accessibility | Grant TCC to **Cua Driver.app** (`cua-driver permissions grant`) |
| Glide hits old screen point after window moved | Omit stale `cursor_screen_x/y`; use window-local coords |
| Single monitor / asleep secondary | Valid. Do not force a second display |
| `type-text` ok but field empty (Catalyst) | Treat as incomplete; prove with UI outcome (e.g. Voice→Send), not `typed_path` |
| Dispatch `ok` / `accepted` but UI unchanged | Fresh `state` or plan `expect` is proof — never trust dispatch alone |
| State floods context | Compact + `query` / `diff`; raise `--max` only when Compose/Send/modals truncated (often 80) |
| Open sheet: no filename via query | Sheet is the root; do not press Open blind. WhatsApp files → `$whatsapp` `attach-file` |
| Finder sidebar label not clickable | Owner attaches static child text to the row; do not `bring_to_front`+sleep |
| Desktop widgets / Notification Center | Not ordinary AX windows — see `special-surfaces.md` |
| Pixel / coordinate click wanted | Only with `MACOS_CUA_PIXEL_CLICK=1`; default is glide then AX |
| Clipboard paste for background type | Refuse — AX / `type-text` only |
| Two cursors or invisible Hermes cursor | Prefer labeled glide path; `end_session` / operator restart if overlay wedged — see `operator-ui.md` |
| Wrong display / offset pointer | Window-local actions; `displays.md` |
| Screenshot black / occluded | Need visual proof → foreground once; otherwise AX-only compact state |
| Custom-drawn field eats shortcuts, not text | Stop. Use the app’s native API/automation or report the gap |
| Hold needed (time-based key) | `hold-key` with duration; not repeated taps |

Maintainer bench/revert rules: plugin `README.md` + `entry-contract.json` /
`hardening-contract.json` — not this file.
