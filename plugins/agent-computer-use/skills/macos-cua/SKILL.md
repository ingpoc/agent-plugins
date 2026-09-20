---
name: macos-cua
description: >-
  Operate native macOS apps through CUAService. Act-first: one batched act
  per app; compact state only for discovery or after an act miss.
  Current-tree labels (Clear=All Clear). Overlay cursor; AX press first.
  Use when an agent must operate or validate any native Mac app. Not for
  terminal-only work or browser DOM tasks. Every session friction must
  become an app-agnostic fast_path linter/grader so later agents do not
  repeat it.
allowed-tools: Bash, Read
---

# macos-cua

Engine: **CUAService** (Swift `.app`). Cursor MCP is a 2-tool adapter (`state`, `act`) — not a second computer-use stack.

TCC on **macos-cua Service** / **CUAService**: Accessibility + Screen Recording. Relaunch after granting Screen Recording.

> **Self-validate after edits.** `./scripts/validate.sh` from this skill directory.

```bash
python3 "$SKILL_DIR/service/install_service.py"
```

Do not shell a Python client per click when MCP is up. Do not use cua-driver or `start_session` / `verify` / `end_session`.

## Surfaces

| Surface | Owns |
| --- | --- |
| CUAService | AX walk, click/key/type, overlay cursor, settle, screenshot |
| MCP `state` + `act` | Cursor tool catalog only |
| This file | Act-first loop, bans, friction-encode |

## Critical path

1. **Act-first** when labels/outcomes are known — one batched `act` per app (`steps` + `expect`). Compact `state` only for discovery or after an act miss. Never `state`→`state`→`act` on the same app.
2. Overlay tip then AX press. Reuse the returned **compact AX delta** / failure nearby. Full tree only via `state`. `expect` matches text **values**, never button titles. A JPEG attaches only when pixels are the evidence (empty AX, Stage Manager thumb, or `screenshot:true`); wrong-app / thumb shot → stop. Typed failures → [`references/observe-feedback.md`](references/observe-feedback.md).
3. Cross-app: one `act` per surface, then switch — no observe hops between apps.
4. **Encode friction** into `scripts/fast_path.py` (fail the old trace), then retry. No named-app helpers.
5. **Ambiguity only:** when multiple complete plans are plausible, `scripts/jev_act.py` builds immutable candidate IDs → TypeSafe Jev Choice → validate ID → one `act`. Known labels skip Jev. Abstain/reobserve/low confidence ⇒ no auto-act. Expect proof still required after act.

WhatsApp **send/attach**: `$whatsapp` only — not this skill.

## Hard bans

- No `start_session` / `verify` / `end_session` / cua-driver / 54-tool MCP.
- No probe `state` chains before `act` when labels are known.
- No menu-bar BFS root.
- No Chrome via this skill (browser MCP owns Chrome).
- No silent Quartz fallback (`MACOS_CUA_PIXEL_CLICK=1` required).
- No WhatsApp send.
- Dispatch `ok` is never proof. No desktop-global click.
- Confirm immediately before a risky UI action; see [`references/safety.md`](references/safety.md).

## Load map

| When | File |
| --- | --- |
| Act-first loop / input delivery / Mini local-exec | [`references/fast-workflow.md`](references/fast-workflow.md) |
| MCP / CUAService architecture | [`references/architecture.md`](references/architecture.md) |
| Act deltas / failure taxonomy / focused window | [`references/observe-feedback.md`](references/observe-feedback.md) |
| Recovery ladder | [`references/actions.md`](references/actions.md) |
| Command failure | [`references/troubleshooting.md`](references/troubleshooting.md) |
| Risky UI | [`references/safety.md`](references/safety.md) |
| Friction graders | `python3 scripts/fast_path.py --lint` |
| WhatsApp | `$whatsapp` |

Maintainer-only (not the agent loop): `references/lifecycle.md`, `references/cua-driver-mcp.md`, `references/computer-parity.md`, `references/entry-contract.json`.
