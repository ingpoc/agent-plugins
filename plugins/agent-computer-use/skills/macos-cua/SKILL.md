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

Engine: **CUAService** (Swift `.app`). Cursor MCP is a 2-tool adapter
(`state`, `act`) over that socket — not a second computer-use stack.

TCC on **macos-cua Service** / **CUAService**: Accessibility + Screen Recording.
Relaunch the service after granting Screen Recording.

> **Self-validate after edits.** `./scripts/validate.sh` from this skill directory.

```bash
python3 "$SKILL_DIR/service/install_service.py"
```

Do not shell a Python client per click when MCP is up. Do not use cua-driver
or `start_session` / `verify` / `end_session`.

## Surfaces

| Surface | Owns |
| --- | --- |
| CUAService | AX walk, click/key/type, overlay cursor, settle, screenshot |
| MCP `state` + `act` | Cursor tool catalog only |
| This file | Act-first loop, bans, friction-encode |

## Architecture boundary

MCP follows stable `2026-07-28`: stateless requests, per-request `_meta`,
`server/discover`, structured results, and legacy fallback only at the adapter.
Realtime uses the same two operations through local function calling; it does
not create a second native engine or expose the Mac as a remote MCP server.

Keep the model surface at `state` + `act`. New macOS capability is an `act`
step, not another model tool. CUAService serializes desktop requests and owns
one native `execute_plan` RPC: before-state → all same-app steps → settle →
after-state. The compact adapter evaluates the canonical structured
postcondition against that native evidence and is the only completion gate.

Reuse the native operations CUAService already has: scroll, text selection,
secondary AX actions, and drag. File, directory, URL, and application opening
belongs in CUAService as an `act` step backed by
`NSWorkspace` plus `FileManager` validation—not Finder keystroke choreography.
Use AX first and one-shot ScreenCaptureKit vision only after an AX miss.

`compact_mcp.py` owns the canonical MCP input/output schema. Realtime schemas
must be derived from it or parity-tested. Use explicit operation variants and
structured `expect` predicates; never an unconstrained action string. Long or
deferred work may use the MCP tasks extension only after ordinary bounded plans
prove insufficient.

## Fast workflow

**Act-first.** Labels known → `act` (optional `steps` + `expect`). Compact
`state` only for discovery or after an act miss. Never probe `state` then
`state` then `act` on the same app.

**Two wall clocks.** Within-app: one batched `act`. Cross-app: one `act` per
surface then switch. Each extra MCP tool is a full agent turn.

`act` returns the settled tree plus **screenshot_before** and
**screenshot_after** (same tool). Capture is the CG window after raise,
not an AX-rect crop of wallpaper. ScreenCaptureKit has a deadline.
Inspect those pixels: if the before shot is the wrong app or a Stage
Manager thumb, stop and fix bounds — do not click. `verified` is true when `expect` matches a text **value**
(`AXStaticText` / `AXTextArea` / `AXTextField` / `AXCell` titles), never
button titles. Capture the focused window, not the largest sibling.

**Encode friction.** Extra `state`, leftover `verify`/`start_session`,
unbatched same-app `act`s, chat-only recovery → `scripts/fast_path.py`
grader that **fails the old trace**, then retry. No named-app helpers.

**Best first, then fallback.** Overlay tip lands, then AX press
(`ax_timeout` fail-closed). Tip and click stay in sync — do not fire
glide concurrent with press. In-place retitles (Clear/All Clear) do not need
a fresh `state`.

1. **Act-first** when labels or outcomes are known.
2. `act` with `expect`. Reuse the returned tree.
   **Fallback, in order:** (1) one fresh `state` on label miss, then retry;
   (2) if AX has no useful labels, `references/actions.md` recovery ladder.
3. Switch apps — no observe hops between surfaces.

WhatsApp **send/attach**: `$whatsapp` only. Not this skill.

Dispatch `ok` is never proof. The compact adapter blocks a mutating act before
dispatch unless it has `expect` (string, `{text: ...}`, `{not_text: ...}`, or a
list). It returns `ok:true` only after the settled AX tree verifies that
postcondition. `allow_unverified:true` is a dispatch-only escape hatch: report
the attempt as unverified and never say done. App-only focus/launch is verified
by the settled target-window state without an extra `state` call.
Mutating RPCs have a 15-second deadline and are never replayed after an
ambiguous timeout; only read-only state may reconnect and retry once.

**Input delivery (any app).** AXPress/AXClick only if the node advertises that action; success on an unlisted action is a no-op. Successful AX press skips the 0.6s settle. HID keys raise the target app and wait until it is frontmost; otherwise `key_target_not_front` (the host IDE must not eat cmd+n). `type_text` after New refuses a still-full text field and walks the focused window, not the cached largest. No text field in the walk → `type_no_text_target`, not HID. `type_text` sets AXSelectedText at the caret; string or attributed AXValue must contain the insert or it is a miss. Prefer `AXTextArea` over focused `AXSearchField` / `AXTextField`. Miss → click the text-area frame, then bulk HID (not 10ms/char). Walk packs attrs in one IPC, clips off-window nodes, caches live refs. Coordinate fallback is the **finite AX frame** (or schema `x`/`y`), HID at that point — not PID+HID (doubled glyphs) and not a desktop hunt. JSON integers must coerce to Double. `cgevent-click` with a null point is a failed step. `set_value` writes an AX attribute; it is not keystroke delivery. Slow UI: one batched `wait` (cap 45s), not extra `state`. Window PNG can omit `AXPopover`/`AXMenu` whose frame sits outside the window; the tree is the source of truth. `expect` must be a **new** value vs the before-tree (a needle already in the body is not proof a table/cell changed).

## Hard bans

- No `start_session` / `verify` / `end_session` / cua-driver / 54-tool MCP.
- No probe `state` chains before `act` when labels are known.
- No menu-bar BFS root.
- No Chrome via this skill (browser MCP owns Chrome).
- No silent Quartz fallback (`MACOS_CUA_PIXEL_CLICK=1` required).
- No WhatsApp send.
- Dispatch `ok` is never proof. No desktop-global click.
- Confirm immediately before a risky UI action; see safety.md.

## Load map

| When | File |
| --- | --- |
| Command failure | [`references/troubleshooting.md`](references/troubleshooting.md) |
| Risky UI | [`references/safety.md`](references/safety.md) |
| Friction graders | `python3 scripts/fast_path.py --lint` |
| WhatsApp | `$whatsapp` |
