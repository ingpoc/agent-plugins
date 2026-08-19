---
name: macos-cua
description: >-
  Operate native macOS apps through the packaged macos-cua implementation.
  Act-first: one batched act/plan per app; compact state only for discovery
  or after an act miss. Current-tree labels, 1.5s AX fail-closed.
  Screenshot/coordinate fallbacks only after an AX miss. Visible agent cursor
  is an overlay; coordinate clicks still use the one real macOS pointer. Use
  when an agent must operate or validate any native Mac app. Not for
  terminal-only work or browser DOM tasks. Every session friction must
  become an app-agnostic fast_path linter/grader so later agents do not
  repeat it.
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
| This file | Trigger, act-first loop, one-window/sheet rule, hard bans, friction-encode, load map | cua-driver schemas, attach recipes, bench numbers |
| `references/` | Driver contract, troubleshooting, plan schema, gates | Always-loaded routing |

A tool exists only if it is required every session and cannot be a skill rule.
Do not grow toward raw cua-driver (54).

## Fast workflow (default)

**Agent path = MCP only** (`plugin-agent-computer-use-agent-computer-use`).
One server. Lifecycle, state, and act stay in one Python process with a held
cua-driver Unix socket. Do not open a second Computer Use MCP. Do not browse
raw `cua-driver` (54 tools). Do not shell `macos-cua.py` per click when MCP is
up.

**Session:** `workflow.py preflight` once → MCP (`start_session` → **act-first**
per app → `end_session`) → `workflow.py closeout` once. Preflight never
launches an app. Window readiness is `resolve_app` inside `act`/`state`.

**Two wall clocks.** Both count. Within-app: one batched `act`/`plan`
(`resolve_app` + snapshot happen inside `act`). Cross-app: one `act` per
surface then switch. Each extra MCP tool is a full agent turn. Skip
`verify` when `act.verified` is true.

**Encode friction.** Any issue, miss, or rooted inefficiency (extra `state`,
redundant `verify`, unbatched same-app `act`s, dropped driver session,
false-green dispatch, chat-only recovery) must become an **app-agnostic**
`scripts/fast_path.py` linter/grader that **fails the old trace**, then the
production retry. No named-app helpers. Do not leave the fix in chat.

**Best first, then fallback.** Do not start a new `state` or pixels while the
current tree can still resolve the control.

1. `start_session` once. Do not pass preflight into MCP.
2. **Act-first** when labels or outcomes are known. Compact `state` only for
   discovery or after an `act` miss. Never probe with repeated `state` before
   `act` on the same app. Snapshot root: open sheet/popover/dialog; else
   app-level menus; else one window. Never menu-bar BFS.
3. `act`: one asserted `plan` with `expect`. Reuse the postcondition tree.
   In-place retitles (e.g. Clear/All Clear) do not need a fresh `state`. AX
   press fails closed in ~1.5s (`ax_timeout`).
   **Fallback, in order:** (1) one fresh `state` on label miss, then retry;
   (2) if AX has no useful labels, `references/actions.md` recovery ladder.
4. Skip `verify` when `act.verified` is true. Then switch apps — no observe
   hops between surfaces.
5. `end_session` once (`end_session` already runs closeout). Do not quit
   Cursor, the user's messenger, or their browser session.

WhatsApp **send/attach**: `$whatsapp` only (in-process held socket + one-shot
`message-self` / `message` / `attach-file`). Not this skill.

Pass means assertions / verify true. Dispatch acceptance is never proof.
Mutating plans need final or per-step `expect`.

**Not the agent path:** spawning `macos-cua.py` per call or raw
`cua-driver call` CLI. Those are debug/bench only (`workflow.py` /
`run_benchmarks.py`).

## Hard bans

- No redundant `verify` after `act.verified` is true. No probe `state` chains
  before `act` on the same app when labels are already known.
- No menu-bar BFS root (closed Apple menu floods `--max`).
- No Chrome via this skill (browser MCP owns Chrome; Hermes coexistence).
- No silent Quartz fallback (`MACOS_CUA_PIXEL_CLICK=1` required).
- No trusting Catalyst `typed_path` / `ok` (Voice→Send or screenshot).
- No WhatsApp send. App recipes stay in the target repo (`$whatsapp`).
- Dispatch `ok` is never proof. No desktop-global / `global_input` click path.
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
| Benches / gates (not chat) | `python3 scripts/bench_session_shape.py` compares act-first vs probe-state and cross-app tool budgets (app-agnostic). `python3 scripts/fast_path.py --lint` encodes session friction. `python3 scripts/bench_mcp_runtime.py` must beat per-call CLI by >=10%. Warm `python3 scripts/run_benchmarks.py --repeat 5 --rate` — keep only if ok. |
| WhatsApp attach / New Chat / send | `$whatsapp` (in-process + one-shot). Use that skill’s learnings reference, not this package |

Cursor plugin spawn: dest rewrite is owned by
[`references/cua-driver-mcp.md`](references/cua-driver-mcp.md). Do not add
files to an application workspace to “fix” `./bin`.
