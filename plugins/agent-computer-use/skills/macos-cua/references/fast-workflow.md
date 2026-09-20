# macos-cua — Fast workflow

Load for act-first detail, input delivery, expect/verify rules, and Mini local-exec. Keep [SKILL.md](../SKILL.md) thin.

## Act-first

**Act-first.** Labels known → `act` (optional `steps` + `expect`). Compact `state` only for discovery or after an act miss. Never probe `state` then `state` then `act` on the same app.

**Two wall clocks.** Within-app: one batched `act`. Cross-app: one `act` per surface then switch. Each extra MCP tool is a full agent turn.

`act` returns a **compact AX delta** on verified success (not a full tree). Failures return a typed `error_type` + nearby context — see [observe-feedback.md](observe-feedback.md). A JPEG attaches only when pixels are the evidence (empty AX, Stage Manager thumb, or `screenshot:true`). Capture is the CG window after raise, not an AX-rect crop of wallpaper. ScreenCaptureKit has a deadline. If a returned shot is the wrong app or a Stage Manager thumb, stop and fix bounds — do not click. `verified` is true when `expect` matches a text **value** (`AXStaticText` / `AXTextArea` / `AXTextField` / `AXCell` titles), never button titles. Service prefers the **AX focused** window over the largest sibling (0.2.18+).

**Encode friction.** Extra `state`, leftover `verify`/`start_session`, unbatched same-app `act`s, chat-only recovery → `scripts/fast_path.py` grader that **fails the old trace**, then retry. No named-app helpers.

**Ambiguous enumerable plans.** At most one compact `state`, build complete candidate `act` payloads, run `scripts/jev_act.py` (Jev Choice over IDs including `__reobserve__` / `__abstain__`), then at most one reobserve rebuild, then one batched `act`. Opt in when quality holds or improves and Jev is faster and cheaper per decision in USD; do not call Jev when a single labeled plan is already known.

**Best first, then fallback.** Overlay tip lands, then AX press (`ax_timeout` fail-closed). Tip and click stay in sync — do not fire glide concurrent with press. In-place retitles (Clear/All Clear) do not need a fresh `state`.

1. **Act-first** when labels or outcomes are known.
2. `act` with `expect`. Reuse the returned delta (or failure nearby); full tree only via `state`.
   **Fallback, in order:** (1) one fresh `state` on label miss, then retry; (2) if AX has no useful labels, [actions.md](actions.md) recovery ladder.
3. Switch apps — no observe hops between surfaces.

WhatsApp **send/attach**: `$whatsapp` only. Not this skill.

## Expect / verify

Dispatch `ok` is never proof. The compact adapter blocks a mutating act before dispatch unless it has `expect` (string, `{text: ...}`, `{not_text: ...}`, or a list). It returns `ok:true` only after the settled AX tree verifies that postcondition. `allow_unverified:true` is a dispatch-only escape hatch: report the attempt as unverified and never say done. App-only focus/launch is verified by the settled target-window state without an extra `state` call. Mutating RPCs have a 15-second deadline and are never replayed after an ambiguous timeout; only read-only state may reconnect and retry once.

## Input delivery (any app)

AXPress/AXClick only if the node advertises that action; success on an unlisted action is a no-op. Successful AX press skips the 0.6s settle. HID keys raise the target app and wait until it is frontmost; otherwise `key_target_not_front` (the host IDE must not eat cmd+n). `type_text` after New refuses a still-full text field and walks the focused window, not the cached largest. No text field in the walk → `type_no_text_target`, not HID. `type_text` sets AXSelectedText at the caret; string or attributed AXValue must contain the insert or it is a miss. Prefer `AXTextArea` over focused `AXSearchField` / `AXTextField`. Miss → click the text-area frame, then bulk HID (not 10ms/char). Walk packs attrs in one IPC, clips off-window nodes, caches live refs. Coordinate fallback is the **finite AX frame** (or schema `x`/`y`), HID at that point — not PID+HID (doubled glyphs) and not a desktop hunt. JSON integers must coerce to Double. `cgevent-click` with a null point is a failed step. `set_value` writes an AX attribute; it is not keystroke delivery. Slow UI: one batched `wait` (cap 45s), not extra `state`. Window PNG can omit `AXPopover`/`AXMenu` whose frame sits outside the window; the tree is the source of truth. `expect` must be a **new** value vs the before-tree (a needle already in the body is not proof a table/cell changed).

## Mini unavailable is not a CUA miss

Empty ListMachines, Shell "unavailable", or `desktop ownership lost` / DeadlineExceeded is Grok Bot local-exec down, not HID and not Comet pairing. Do not debug CUAService. CUA uses the same pipe, so it cannot recover the Mini while local-exec is dead. Known Grok Bot bug; workaround is a full Grok Bot quit (`Cmd+Q`), kill leftover `local-exec-daemon` processes, relaunch once. Two Grok Bot desktops flap ownership. Do not mint a Comet session for this.
