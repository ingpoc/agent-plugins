---
name: macos-cua
description: >-
  Operate native macOS apps through the packaged macos-cua implementation.
  Uses native Accessibility fast paths plus cua-driver screenshot/coordinate
  fallbacks to observe, click, type, press keys, scroll, drag, and execute
  asserted multi-step plans with background AX-first delivery. Its optional
  visible agent cursor is only an overlay; coordinate fallbacks still use the
  single real macOS pointer. Use when
  an agent must operate or validate any native Mac app. Not for terminal-only
  work or browser DOM tasks.
allowed-tools: Bash, Read
---

# macos-cua

This plugin is the sole macos-cua owner. Harnesses may symlink
`skills/macos-cua`; do not keep a copied skill. Use this plugin's native AX
workflow and packaged 5-tool MCP facade. Raw `bin/cua-driver-mcp` is
diagnostic-only.

> **Self-validate after edits.** Any change to this skill's files (SKILL.md, scripts/, references/, templates/, assets/) must be followed by `./scripts/validate.sh` from the skill directory. Hard findings → create-skill Optimize lane.

Set `SKILL_DIR` to this `macos-cua` directory. Owner CLI:
`$SKILL_DIR/scripts/macos-cua.py`. First-time or after plugin edits:

```bash
python3 "$SKILL_DIR/scripts/install_harness.py" all --replace-copy
```

## Surfaces

| Surface | Owns | Does not own |
| --- | --- | --- |
| MCP (5 tools) | Verbs every session: `start_session`, `state`, `act`, `verify`, `end_session` | Rejected-attempt history, WhatsApp recipes, driver field notes |
| This file | Trigger, 5-step loop, one-window/sheet rule, hard bans, load map | cua-driver schemas, attach recipes, bench numbers |
| `references/` | Driver contract, troubleshooting, plan schema, gates | Always-loaded routing |

A tool exists only if it is required every session and cannot be a skill rule.
Do not grow toward raw cua-driver (54).

## Fast workflow

When a Computer Use MCP server is ready, use **one** server. Codex bundled
Computer Use uses `node_repl` + ~10 `sky` methods and disables its standalone
`computer-use` MCP. Match that discipline.

1. `start_session` once. Do not open a second Computer Use MCP.
2. Resolve the app by name, then bundle id. Never `list_apps` as preflight.
3. `state` is always compact, no screenshot, `--max`. Prefer `query` / `diff`
   after the first observe. Display topology is CLI `displays` or optional
   `start_session preflight:true`, not every observe. Single-monitor is
   valid; never assume a secondary. Snapshot root: open sheet/popover/dialog;
   else open app-level context menus; else one window. Do not walk the menu
   bar as a root. Do not `bring_to_front` unless `escalation.recommended` is
   `foreground` or background AX missed the label.
4. `act` by AX label/index, or a small asserted `plan`. Every element-addressed
   mutation — label and index clicks, double-click, `perform_action`,
   `right_click` — glides the signed cursor to the target, then AX. A mutation
   never lands while the cursor is elsewhere: if the glide is not acknowledged,
   the press fails instead of acting invisibly.
   Pointer is window-local: omit stale screen points so the operator maps
   normalized coords. Do not Quartz-read on the AX click path. Do not
   browse raw cua-driver tools.
5. `verify` then `end_session` once. After live probes, quit the test app
   (Calculator, Dictionary, Stickies, extra TextEdit/Preview). Do not quit
   Cursor, WhatsApp, or the user's Chrome/Safari/Comet session.

CLI when MCP is down or a `run` plan is shorter than N tool calls:

```bash
python3 "$SKILL_DIR/scripts/workflow.py" preflight
python3 "$SKILL_DIR/scripts/macos-cua.py" state Calculator --compact --query 7
python3 "$SKILL_DIR/scripts/macos-cua.py" run Calculator @plan.json
python3 "$SKILL_DIR/scripts/workflow.py" closeout
```

Pass means `ready:true`, fresh state, assertions true, then `success:true`.
Dispatch acceptance is never proof. Mutating `run` needs a final or per-step
`expect` (`allow_unverified:true` is an exception, not success). A reused
plan snapshot refreshes only after a label miss. `key` retries once in
foreground when AX reports `off_space_or_ax_unresolved` or
`escalation.recommended=foreground`.

## Hard bans

- No `list_apps` / `health_report` preflight. No raw 54-tool catalog.
- No menu-bar BFS root (closed Apple menu floods `--max`).
- No Chrome via this skill (browser MCP owns Chrome; Hermes coexistence).
- No silent Quartz fallback (`MACOS_CUA_PIXEL_CLICK=1` required).
- No trusting Catalyst `typed_path` / `ok` (Voice→Send or screenshot).
- No WhatsApp send. App recipes stay in the target repo (`$whatsapp`).
- Confirm immediately before a risky UI action; see safety.md.

## Load map

| When | File |
| --- | --- |
| Driver fields, MCP tools, or `mcp.json` | [`references/cua-driver-mcp.md`](references/cua-driver-mcp.md) — `dump-docs` first; do not retry rejected rows |
| Command failure / rejected attempt | [`references/troubleshooting.md`](references/troubleshooting.md) |
| Writing `run` / plan schema | [`references/actions.md`](references/actions.md) |
| Risky UI action | [`references/safety.md`](references/safety.md) |
| Preflight / closeout / storage | [`references/lifecycle.md`](references/lifecycle.md) |
| Secondary display / cursor offset | [`references/displays.md`](references/displays.md) |
| Desktop widgets / Notification Center / iPhone Mirroring | [`references/special-surfaces.md`](references/special-surfaces.md) |
| Menu bar / PiP / harness links | [`references/operator-ui.md`](references/operator-ui.md) |
| Bundled Computer Use comparison | [`references/computer-parity.md`](references/computer-parity.md) |
| Benches / gates (not chat) | After plugin edits: warm `python3 scripts/run_benchmarks.py`; if any official row is slower than the last warm green, revert; do not loosen budgets. [`references/troubleshooting.md`](references/troubleshooting.md) Eval / revert; `entry-contract.json`, `hardening-contract.json` |
| Like-minded-app only | [`references/likeminded.md`](references/likeminded.md) |
| WhatsApp attach / New Chat | `$whatsapp` — not this skill |

Cursor plugin spawn: dest rewrite is owned by
[`references/cua-driver-mcp.md`](references/cua-driver-mcp.md). Do not add
files to an application workspace to “fix” `./bin`.
