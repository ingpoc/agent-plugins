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

Set `SKILL_DIR` to this `macos-cua` directory. Install / harness:

```bash
python3 "$SKILL_DIR/scripts/install_harness.py" all --replace-copy
```

Agent sessions use the **MCP** tools below — not shelling `macos-cua.py`.
Bench/debug scripts may call `scripts/macos-cua.py` and `workflow.py`.

## Surfaces

| Surface | Owns | Does not own |
| --- | --- | --- |
| MCP (5 tools) | Verbs every session: `start_session`, `state`, `act`, `verify`, `end_session` | Rejected-attempt history, WhatsApp recipes, driver field notes |
| This file | Trigger, 5-step loop, one-window/sheet rule, hard bans, load map | cua-driver schemas, attach recipes, bench numbers |
| `references/` | Driver contract, troubleshooting, plan schema, gates | Always-loaded routing |

A tool exists only if it is required every session and cannot be a skill rule.
Do not grow toward raw cua-driver (54).

## Fast workflow (default)

**Agent path = MCP only** (`plugin-agent-computer-use-agent-computer-use`).
One server. Held cua-driver Unix socket under the MCP process. Do not open a
second Computer Use MCP. Do not browse raw `cua-driver` (54 tools). Do not
shell `macos-cua.py` per click when MCP is up.

1. `start_session` once.
2. Resolve the app by name, then bundle id. Never `list_apps` as preflight.
3. `state`: compact, no screenshot, tight `--max`. Prefer `query` / `diff`
   after the first observe. Topology: optional `start_session preflight:true`
   (or rare `displays`) — not every observe. Snapshot root: open
   sheet/popover/dialog; else open app-level menus; else one window. Never
   menu-bar BFS. No `bring_to_front` unless `escalation.recommended` is
   `foreground` or background AX missed the label.
4. `act`: AX label/index or one asserted `plan` (batch then verify). Every
   element-addressed mutation glides the signed cursor, then AX — fail closed
   if the glide is not acknowledged. Reuse postcondition trees
   (`seed_snapshot` / plan state reuse); refresh only on label miss. Never
   seed a **postcondition** expect from a pre-mutation tree. Omit stale screen
   points. No Quartz-read on the AX click path.
5. `verify` then `end_session` once. Quit probe apps (Calculator, Dictionary,
   Stickies, extra TextEdit/Preview). Do not quit Cursor, WhatsApp, or the
   user's browser session.

WhatsApp **send/attach**: `$whatsapp` only (in-process held socket + one-shot
`message-self` / `message` / `attach-file`). Not this skill.

Pass means assertions / verify true. Dispatch acceptance is never proof.
Mutating plans need final or per-step `expect`.

**Not the agent path:** spawning `macos-cua.py` per call, raw
`cua-driver call` CLI, or `MACOS_CUA_SUBPROCESS=1`. Those are debug/bench only
(`workflow.py` / `run_benchmarks.py`).

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
| Benches / gates (not chat) | Warm `python3 scripts/run_benchmarks.py`; keep only if no official row regresses vs last warm green. `entry-contract.json`, `hardening-contract.json`, plugin `README.md` — not troubleshooting |
| Like-minded-app only | [`references/likeminded.md`](references/likeminded.md) |
| WhatsApp attach / New Chat / send | `$whatsapp` (in-process + one-shot). Learnings: that skill’s `references/learnings.md` — not this package |

Cursor plugin spawn: dest rewrite is owned by
[`references/cua-driver-mcp.md`](references/cua-driver-mcp.md). Do not add
files to an application workspace to “fix” `./bin`.
