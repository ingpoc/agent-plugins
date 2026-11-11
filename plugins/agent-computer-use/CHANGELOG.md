# Changelog: agent-computer-use

## 0.2.129

- Treat numbers with grouping separators (9,801) as equal to plain digits in `expect` checks; report the label of the step that actually failed in `target_missing`.

## 0.2.128

- Offer `comet_act` and `comet_end` together with `comet_begin` from the start: the live model ignored the mid-turn tool-list update and said it could not read or close the page.

## 0.2.127

- Wait up to 2 s for `session.updated` before continuing after a tool-list update (Comet read/close tools were unusable); log expected and read-back text on failed native steps; lift the Codex-circle initiator badge so it stays inside the menu-bar height.

## 0.2.126

- Show the initiator badge on the Dynamic Island left Codex circle when one initiator owns it; skip queued tool calls after a spoken cancel and report in-flight ones as unknown; block Ledger recall retries after a rejected query in the same turn.

## 0.2.125

- Fail fast with `screen_locked` or `display_asleep` and a plain hint instead of "App has no open window"; add `step_ref` to usage rows.

## 0.2.124

- Report empty Ledger lookups as `none`; record request expectations and completed outcomes from the last successful tool result.

## 0.2.123

- Correlate task, tool, cancellation, playback and failure events by run and step refs while retaining non-secret diagnostic content.
- Keep Comet and native CUA failure facts compact, with credential-only redaction and explicit gaps for missing links or final URLs.

## 0.2.122

- Clarify when to close a Comet task window and avoid retrying dispatch-only native actions blindly.
- Record preference replacements returned by Context Ledger save receipts.

## 0.2.121

- Stamp every activity row with the installed build identity, including the app package digest.
- Record tool durations and a bounded Ledger decision trail; run ids join voice latency and usage to a task.
- Reopen rotated activity logs even when another writer rotated them first.
- Keep the island at its content width while a card closes into controlled voice.
- Apply a stop snapshot that only changes the Samantha mark.

## 0.2.120

- Search the same site or app scope for saved decisions when a rule-specific lookup returns no matches.

## 0.2.119

- Preserve action dispatch and observed app exit when screenshot capture races with the app closing.

## 0.2.118

- Apply an unambiguous saved user preference without treating its application history as an approval requirement.
- Preserve an observed app exit in action feedback without claiming an unverified action succeeded.

## 0.2.117

- Return an explicit not-running state when an observed app has exited.
- Direct site and app preference lookups to their own Ledger scope.

## 0.2.116

- Use an agreed saved preference when complete, relevant Ledger decisions have the same value under different rules.
- Recognize an exited app as an observed absence when verifying a planned action.
- Record the Ledger scope category and rule presence without saving their values.

## 0.2.115

- Normalize bare site/app scopes to tool scopes before Context Ledger preference lookup.
- Keep app-state reads passive so post-close checks cannot relaunch the app being checked.

## 0.2.114

- Add authorized agent control to start, stop, and inspect Samantha using the menu supervisor.

## 0.2.113

- Require an observed after-state before accepting a negative text postcondition.

## 0.2.112

- Clarify pending application outcomes for recorded user decisions without asserting approval or successful use.

## 0.2.111

- Maintenance release for installed MCP refresh verification.

## 0.2.110

- Reconnect a closed service socket when probing its identity before an MCP handoff.

## 0.2.109

- Prepare stale MCP clients to hand off queued requests to a package matching the running service before dispatch.
- Clarify that incomplete Ledger results require a question before selection.

## 0.2.108

- Report incomplete Ledger search results as needing disambiguation instead of claiming a complete match.

## 0.2.107

- Refresh Ledger lookup results on every lookup so saved and externally updated preferences do not remain hidden behind stale cached results.

## 0.2.106

- Do not assign a completed spoken task’s identity to a later unmatched delegation.

## 0.2.105

- Serialize Python and Swift log rotations through compression to prevent concurrent archive races.

## 0.2.104

- Package standalone tests without a collection-only sibling dependency.

## 0.2.103

- Route preference-sensitive choices to the saved-decision check.

## 0.2.102

- Include an explicit site or app scope in Samantha’s general Context Ledger lookup.

## 0.2.101

- Load the service client at MCP startup so an idle client retains it across package refresh.

## 0.2.100

- Correlate delayed speech transcripts when one unstarted delegation has a matching timeline.

## 0.2.99

- Tag mirrored startup diagnostics with a shared event identity.


Built from git tags and commit history (`git log -- plugins/agent-computer-use`). Dates are commit dates. Earlier untagged versions are grouped.

## 0.2.98 (2026-11-06)

- Correlate tool and Ledger activity with tasks, distinguish tool reports from independently verified outcomes, and record microphone and island transition evidence.

## 0.2.96 (2026-11-06)

- Correlate explicit task outcomes and capture bounded, credential-redacted Ledger and error diagnostics locally.

## 0.2.97 (2026-11-06)

- Include need, situation, and decision-rule terms in Context Ledger recalls instead of sending an empty query.
- Cancel an idle-width pinch when controlled voice content appears mid-transition, keeping the voice slot inside the island.

## 0.2.95 (2026-11-06)

- Emit privacy-safe ledger operation and failure categories for diagnostics.

## 0.2.94 (2026-11-05)

- Include the lookup need in Context Ledger searches and stop on rejected arguments.

## 0.2.93 (2026-11-05)

- Restore the compact idle island pill to its original 74 pt width.

## 0.2.92 (2026-11-05)

- Remove Samantha-owned plugin activity and compact surviving harness circles after her exit fade.
- Fill the existing outside plugin circles with circularly clipped package artwork for ACU, Comet Control, Context Ledger, and Developer Observability.
- Use Context Ledger proactively for relevant preference recall and durable owner preferences; normalize scope/rule formatting and clarify supersession limits.

## 0.2.90

- Owner preferences and decisions: automatic capture (user_decision, tool:/project: scope, rule = decision point, same scope+rule supersedes) and exact recall before a decision point.
## 0.2.89 (2026-11-04)

- Correct compact island geometry: 74 pt minimum, even whole-point width, snapped origin, and the 24 pt ring-outer-edge gap; keep the no-target voice slot and dot aligned across voice states.
- Crop plugin and app marks to opaque glyph pixels, center them, remove background plates, and keep the developer-observability waterfall centered. Plugin and harness circles stay bare.
- Keep harness slots stable during joins and exits; keep voice width reserved through done plus 2 s, grow immediately, and debounce shrink by 400 ms.
- Retain target and app activity through a voice turn; clear speaking without audio after 3 s.
- Keep multi-step ACU app/voice layout intact, preserve done holds through playback completion, clear no-done turns on idle, and reuse harness slots after exit transitions.
- Extend `island_layout` geometry/status/app identity fields and add timestamped voice state transition events.
- Add scale-1/2 capture checks, multi-app rendering, shrink-overlap and harness stability coverage, plus timestamp and transition tests.

## 0.2.88 (2026-11-04)

- Accept valid marketplace plugin IDs for right-side island marks, with manifest icons, monograms, and optional display labels; add Claude Code, Cursor, and Codex MCP hook support.
- Fallback marks are bare on the black circle like every plugin mark (Guru 16:06 IST): a manifest icon goes through `IslandPluginMarkGlyphView` (lifted off its plate), and the monogram is white letters with no colour disc or tile. `IslandTargetGlyphTests` covers both.

## 0.2.87 (2026-11-04)

- Controlled app inside the island in every state, voice included (Guru 2026-11-04: "why do i see app icon still on the right side of the island"). One shared rule: inside the pill, left to right, the controlled target with its ring (Apple for ACU, Comet for comet-control), the gap, the app icons (ACU only), then Samantha's voice slot (only while she listens, thinks, speaks or finishes), then the dot. Harness circles (Samantha included) stay outside on the left; only plugin marks go outside on the right. The `.app` outside-circle path is gone.
- Root cause: voice_cua kept `_ACTIVE_APPS` until idle and re-sent it with every listening/thinking/speaking post; CUAService turned those apps into outside-right circles once the ACU row dropped, while voice states were excluded from the ring, operated apps and hug width. Voice posts now carry no stale apps, and the model keeps the ring, apps and hug width in every compact Samantha state.
- 13:26:54 flash fixed: Samantha registers the plugin use before posting `acting`, so the app never shows for one frame without its ring.
- Target gap: 24 pt (Guru 13:30 IST "instead of 16 pt put 20 pt", then 15:52 IST "increse from 20 to 24pt"), one shared constant for ring to apps and ring to voice slot / dot, giving 87 / 127 / 147 pt pills for 1 / 3 / 3 + N apps.
- Samantha's voice slot (Guru 14:17 IST, "A plus C's shape cue"; UI & UX spec). There is no mic or wave glyph and no wide empty meter; the slot is the whole voice element:
  - Meter: 5 bars, 3 pt wide with a 2 pt gap, in a fixed 23 pt slot. Heights run 3–14 pt from the audio level, smoothed over about 80 ms at ~30 Hz (10 Hz with Reduce Motion).
  - When Guru speaks, white bars grow up from the baseline. When Samantha speaks, the bars are centred and symmetric in her #BF5AF2 → #FF6482 gradient.
  - Thinking: 3 dots, 3.5 pt across and 6 pt apart, in a 14 pt slot, pulsing 100 / 60 / 35 % on a 0.9 s loop (static 62 % with Reduce Motion).
- Pill width:
  - With nothing controlled, the pill hugs 9 · slot · 6 · dot · 9: 53 pt for the meter, 44 pt while thinking.
  - With ACU active: ring · 24 · apps (newest first, 3, then +N) · 8 · slot · 6 · dot · 9.
  - Loudness never changes the width. The width changes only when the slot's content changes, on the 250 ms spring and with no 74 pt pinch. The meter never lights the dot (Nov 1 rules).
- Voice slot transitions:
  - Listening → thinking: the bars ease into dots over 200 ms. Thinking → speaking: the dots grow into bars over 200 ms. Speaking → listening: a 150 ms colour crossfade.
  - Reduce Motion uses a crossfade instead of the morph. The old pose leaves within 50 ms; the new one fades in from 80 ms.
  - The check ↔ meter swap works the same way: out in 50 ms, in after 100 ms, so slot content never touches the moving dot in any frame.
- Transitions: plugin marks enter after the pill settles (0.30 s, 0.15 s under Reduce Motion) and leave in 0.1 s, and inside elements are clipped to the animated pill, so no frame puts a mark over a shrinking pill or an inside element outside it. Mid-frame zone tests cover Samantha on/off, the voice cycle, a turn with ACU, speaking while ACU acts, apps added or removed mid-voice, plugin marks during voice and barge-in, with and without Reduce Motion.
- Not squeezed (Guru 15:26 IST, "the dynamic island is squeezed": "Use the same 20 pt gap between the ring and the dot", then 15:52 IST 24 pt): a ringed target with no apps (Comet reading a page, or ACU with no app) now hugs ring · 24 · dot · 9 = 65 pt instead of ring · 8 · dot · 9 = 49 pt. Whatever follows the ring (apps, voice slot or dot) starts the same 24 pt after it, through `IslandBarLayout` (`targetOnlyDotGap` = `operatedRingGap`); a target with the voice slot and no apps is ring · 24 · slot (94 pt speaking, 85 pt thinking).
- Bare logo in the ring (Guru 15:27 IST: "have the icon rather than icon inside white square, comet sign is not visible then"; "similarly for apple logo"; 15:29 IST "that is the design language"): every controlled target draws its bare glyph in the ring, no white tile. One path, `IslandTargetGlyphView`: ACU's white `apple.logo`; Comet's coloured mark lifted from its real installed icon (`IslandTargetGlyph.bareGlyph`), else the app's SF Symbol in white. `IslandTargetGlyphTests` fails if any target, current or future, renders on a tile.
- Voice group (Guru 16:05 IST, approved live reference "comet icon is on the left and listening has moved along with dot"): the voice slot stays 6 pt before the dot in every voice state, Samantha speaking included, with or without a target; the 24 pt gap sits between the ring and that group.
- Plugin marks bare (Guru 16:06 IST: "the plugin icon is coming in the white square bar, rather have the icon"): every outside-right plugin mark is its bare glyph on the black circle through one path, `IslandPluginMarkGlyphView` (`bareMark` lifts the logo off its plate, dark strokes turn white, colours stay); developer-observability's mark is #30D158 with no tile. `IslandTargetGlyphTests` fails on a tiled plugin mark.
- developer-observability has its own designed mark outside on the right (trace waterfall: three 2 pt round-cap bars, 1.5 pt apart, #30D158 bare on black) and is a known plugin for island activity.

## 0.2.86 (2026-11-04)

- Samantha's mark is her own wave (Guru picked B at 11:54 IST; UI & UX spec): one tapered sound wave, 2 pt round-cap stroke (at least 1.5 pt), 60 % of the circle wide, vertically centred, #BF5AF2 → #FF6482 left to right, in the same 24 pt circle and rim as every harness. It replaces the plain waveform glyph.
- Turning Samantha on shows her harness circle on the left at once, through the same circle list and motion as every harness. One shared harness motion (`IslandMotion.harness*`): `.smooth(duration: 0.35)`, with the neighbours sliding one slot in the same transaction. A circle enters with scale 0.6 → 1 and opacity 0 → 1 as one layer, kept inside its own opening slot (free space shared 4 : 6 with the neighbour and the pill, as at rest), drawn above the neighbours it pushes. On turn-on it no longer slips under Codex: clear by 0.07 pt at 25 % and 2.7 pt at 50 % (was −8.4 and −0.15 pt). Shared exit for every harness circle: it scales 1 → 0.6 and fades 1 → 0 in place with `.easeIn(0.15)`, then the slot closes with `.smooth(0.3)` from 0.10 s (0.40 s in all), so no neighbour reaches it while it is above 10 % opacity (min 2.54 pt clear). With Reduce Motion the layout, pill width included, snaps at once and only the entering or leaving circle fades over 0.15 s (no slide, no duplicate neighbour). This also fixes a scale anchor that sat at the window origin instead of the circle.
- Ring-to-apps gap: 16 pt, no divider, for the Apple and Comet rings alike (UI & UX, from a 12 / 16 / 20 study), giving 79 / 119 / 139 pt pills for 1 / 3 / 3 + N apps. A controlled target with no apps in use (comet-control reading a page) now hugs ring · 8 · dot · 9 (49 pt) instead of stretching to 136 pt.
- Log rotation by the writer: `samantha-activity.jsonl` and `voice.log` roll over at 50 MB, keeping `.1` … `.5` gzipped and deleting older copies. The Python (voice_cua) and Swift (CUAService) writers share one lock-and-rename protocol, so rows are never lost or written into a rotated copy. voice.log is now opened `O_APPEND` by both sides, and voice_cua rotates its own stdout and stderr. Sockets, locks and other files in `~/.cache/macos-cua` are never touched. developer-observability 0.1.9 reads the rotated copies.
- Test hygiene: the ledger test modules write their activity rows to a temp path, never to the real Samantha log.
- One CUAService per user (two ACU menu bar icons, 2026-11-04). A second launch of the installed app hands off and exits before any socket, status item or island exists. It detects a running copy of the same bundle id, or an exclusive lock in `~/Library/Application Support/CUAService/instance.lock` that survives a wiped `~/.cache`. MCP clients start the default service through LaunchServices (`open -g -b com.ingpoc.cua-service`), never by executing the binary.
- One menu-bar item per process: `StatusBarController` owns a single status item, reuses it on every `install` and removes it on terminate, so a SIGTERM'd instance leaves no ghost icon. A test enforces one creation site.

## 0.2.85

- Dynamic Island gap (Guru 2026-11-04 10:22 IST): a 12 pt gap (was 8) separates the ringed target from the app icons inside, so it is clear what is controlled and which apps are in use. One shared layout for every controlled target (Apple ring for ACU, Comet for comet-control); the pill springs wider when apps appear and collapses when none are in use (75/115/135 pt for 1/3/3+N apps). Samantha stays a harness circle outside on the left, and Comet is never a stray circle outside the island.
- `island_layout` rows (schema 1): CUAService logs the island, its inside marks, outside circles and the agent cursor as rectangles only, on change, moves coalesced to one per 250 ms. 0.2.85 is the first version that emits them.
- Context Ledger: an invalid argument is reported as a rejection ("Ledger needs clearer details", `ledger_invalid_argument`, nothing saved), not as "unavailable"; project names are normalised (`Netflix` → `netflix`), personal notes go to `personal`, quotes may span recent utterances, and rejection or revocation in a later utterance still blocks the save.
- "Bring Comet to the front": `comet_begin {foreground: true}` raises Comet through CUAService (no real mouse) and verifies it is frontmost; Samantha says it is in front only when verified, otherwise "Couldn't bring Comet to front".
- Specific island error copy for Ledger rejections, unconfirmed saves, a busy Ledger, Comet not frontmost and confirmations, instead of "Something went wrong".
- Logging: assistant replies no longer split one word per row when user and assistant transcripts interleave; a final `usage` row (final=true, session totals) on session close; numeric `ts` on usage rows (`ts_iso` alongside); phases after go-live are `session_phase`, not `startup`; `comet_call` rows carry opaque `target_id` and `tab_id` hashes (no content).

## 0.2.84

- One failed tool shows one island error: when `cua_act` or a Comet tool already put its own error on the Dynamic Island, the generic "Failed · …" summary is no longer published a second time (it doubled `island_error_raw` rows and replaced the specific copy).
- A risk-gate confirmation request is not a failure: the island shows "Needs your OK" instead of "Missed that, retrying", and the activity log records `tool_needs_confirmation` instead of `tool_error`.
- Every Samantha session's first `stack_begin` row records the ACU `version`, so logs can be split by release.
- A Mac with no input device fails the mic preflight fast: no TCC helper or stream open, one `mic_preflight` row with status `skip` and cause `no_input_device`, and the session continues text-only.

## 0.2.83

- Samantha can use Context Ledger decisions through a lazily loaded, project-scoped local helper: relevance checks, owner-decision and sensitivity gates, duplicate lookup, grounded IDs and uncertain-write protection. When Ledger access is unavailable she says so instead of guessing a website.
- An unverified computer action is not replayed, including equivalent spellings of a keyboard shortcut; a non-retryable failure stops retries at once, and a new user turn allows recovery.
- Tool-task outcomes are logged without prompt text; only explicit ACU verification counts as success. Native actions get precise postcondition guidance.
- `comet_act` and `comet_end` stay out of Samantha's idle tool schema until a Comet lease begins; full page text reads are distinct from compact `page_context`.
- GPT-Live answers greetings, simple arithmetic and exact-format requests itself, without a backend delegation or preamble.

## 0.2.82

- Harden the local computer-use RPC boundary: the socket is restricted to its owner and each client's kernel-reported user identity is checked; malformed and oversized requests are rejected, incomplete requests expire, and connections and concurrent requests are bounded without dropping healthy persistent clients; unsafe socket paths are refused, and startup stops if the listener cannot be secured.

## 0.2.81

- Reverts 0.2.80 (never released): inside-pill app icons are back to the accepted 0.2.79 behaviour, each app clearing 2 s after its own last call with a 200 ms fade, newest first, 3 then "+N" (Guru, 2026-11-03 14:49 IST: "2 second fade is good enough"). Code is identical to 0.2.79; the number only moves because the release policy forbids going backwards.

## 0.2.79 (2026-11-03)

- Plugin activity on the Dynamic Island (Guru, 2026-11-03): one shared signal, `bin/island_activity.py` (`start`/`end`/`pulse` over the owner-only `island.sock`, body field `plugin`). Rows with `plugin` are not agent rows: the plugin's own mark sits on the RIGHT of the pill (ACU icon, comet-control and context-ledger logos; tooltip and VoiceOver name the agent), and the controlled target sits INSIDE the pill's lead slot in a spinning ring (Apple mark for ACU, Comet for comet-control; none for context-ledger). Left stays the agent-harness circles; the 6 pt compact dot is unchanged. The hub drops plugin rows at their TTL (never "updates stopped").
- The ACU MCP reports `act`/`state` use per calling agent: held while the call runs, dropped 2 s after the last call (a failed call never holds it longer), cleared at MCP exit.
- Samantha uses the same island language: her ACU and Comet tool calls post through `island_activity.py` as "Samantha", so she gets a left agent circle, the target ring inside the pill, the blue dot and the right plugin mark, dropped 2 s after her last call. Comet is no longer a separate right-side app circle for her.
- Operated Mac apps INSIDE the pill (Guru, 2026-11-03 13:27 IST): for any agent, each ACU `act`/`state` call sends its app (`target_app`, one row per app), and the island draws the real app icons (Calculator, Finder, TextEdit…) inside the pill on its right, newest first, 3 then "+N", just left of the status dot, with the Apple ring still inside on the left; the pill grows to fit them. Each app clears 2 s after its own last call with a 200 ms fade. Samantha's `cua_*` calls do the same, and an app shown inside is no longer repeated as an outside circle. Plugin marks stay outside. UI & UX fixes: the compact pill hugs ring · 8 · icons (4 apart) · 6 · dot · 9 in every state (no Samantha acting/conversation width), near-white icons sit on a white tile with a dark glyph, "+N" is Semibold 11 at white 62 % in a fixed 16 pt slot, and icon changes spring the width over ~250 ms (Reduce Motion: crossfade) with 200 ms icon fades, straight to the fitted width (no squeeze). The left side is harness-only: plugin identities map to their calling harness or drop, and Samantha's circle wears her own glyph instead of ACU's icon.
- UI & UX review: the inside target sits on a 12 pt white tile in a 20 pt ring; a plugin in use lights the blue working dot (red and amber still win); names in title case ("Agent Computer Use"), "Using …" only in VoiceOver and the expanded row (one row per use); plugin circles fade out over 200 ms; one shared agent-name normaliser (`normalize_agent`) so "grok-bot" and "Grok Bot" are one name.

## 0.2.69 (unreleased: not tagged; waiting for Guru's OK)

- Claude desktop: an open chat shows as Working while Claude replies and Done after. A plain chat's sidebar row stays "Idle <title>" while it streams, so the watcher now reads the composer's "Stop response" button (which sits below the old depth-30 walk limit) and the "Currently streaming message" message group, and names the row from the chat header. A new chat that Claude renames mid-reply no longer flashes Done. The "Chat and Cowork" tab is read from its AXValue. Message subtrees are pruned and only their accessible names are read.
- Claude Code: a sign-in or billing failure (`authentication_failed`, `billing_error`) now ages out after 2 minutes instead of holding "Claude · Failed" for 30 minutes, and it no longer hides a running Claude desktop chat.
- Opening the island marks the failures it shows as seen; a new failure shows again.
- Compact pill: no text and no icons, just the 74×24 black pill plus one solid 6 pt dot, 9 pt in from the right end and vertically centred (no glow, ring or animation). The dot is red (#FF453A) for an unseen failure, then amber for needs-you, then #64D2FF for working; idle and done have no dot. Details show only in the expanded island. VoiceOver reads the full state from the pill ("Codex failed, Claude needs you, Cursor working").
- Agent circles get one 12 pt badge at the top-right, in the same order: a red cross for failed, an amber "!" for needs-you (it replaces the count), and a green check for done. The check holds 10 s, then the circle fades out over 200 ms; VoiceOver reads "<Agent> finished". A seen failure clears its red cross and red dot, and the circle too unless that agent has a new run.
- The menu bar and island menus say "Settings…" instead of "ACU Settings…".
- Fix: "Account details…" (the "Your island is on" window) no longer swings up and down or clips "Shown on the Agent Plugins site" behind Done. The window opens at its content size, and the header wave's endless animation is now scoped to the wave, so it can't catch the window's resize.
- Menu bar: removed "Show Dynamic Island" (⌃⌘I), since it only expanded the island. The global hotkey still works. Removed "Show me on the site" from the account submenu: you answer the site question once, before unlock, and that answer is final. The unlocked card and Settings now show the choice without a toggle.

## 0.2.68 (2026-11-01)

- Unlock the island by entering your handle once and following Gurusharan for a free three-day trial. Repost the launch post to keep it permanently, with no post to write or link to paste.
- Before the launch post is published, trials stay free and the Repost row stays hidden. Once the launch is configured, everyone gets at least three days to repost. Follow and repost already confirmed together unlock permanently without creating a trial. Existing unlocked accounts keep their access, and older post-link unlocks remain supported by the gate.
- Unlock screens highlight the next step, show follow and repost checks in a status card, and use neutral Close and Done buttons. Expired trials keep the confirmed Follow step, with a Change link for the handle. Missing-permission warnings replace the island's app glyph without shifting the pill.
- The unlocked card now says “Works offline too”. Valid offline access survives refresh errors, and refresh/wall requests include the signed device ID required by both gate server versions.
- Claude desktop: sidebar rows are found in Claude 4.4212+ ("Sidebar"-titled group, "More options" popups in a different parent, "Mark as unread" rows), the watcher polls every 1–2 s, and `status --json` adds `stop_button_visible`.
- Typing into empty web/Electron composers (e.g. Claude desktop) now falls back to an AXValue write instead of refusing.
- Gate transitions and panel visibility reasons are logged without tokens. Status JSON can report the running service’s gate state and actual panel frame over the private local socket.
- Claude desktop activity supports the new sidebar layout and the Stop response signal without reading chat text. Finished runs remain visible briefly, and status JSON includes Claude attachment and accessibility diagnostics.

## 0.2.67 (2026-11-01)

- Claude Code: the ACU MCP server connects again. The runtime contract now ignores Claude's `.in_use/<pid>` session markers in the plugin root (like Cursor's `.cache-complete`), which made every Claude session fail with `package/app build mismatch`.
- install-mac.sh filters plugins by each harness's catalog: comet-control is never requested for Claude Code; a plugin missing from a catalog is reported as `skipped: not in catalog` instead of exiting 14. Grok is reported from `grok plugin list --json` (skip when current, otherwise manual: its trust prompt is yours to confirm), never installed or prompted by the script, and finish gives Grok a listing proof.

## 0.2.66 (2026-11-01)

- Island gate go-live: production gate at https://acu-gate.onrender.com with a pinned key, redesigned unlock window, LinkedIn follow, and guarded URL opening. Fix `gate-admin --wake`; ship release manifests and installer.

- Keep unlock sharing available during service cold starts and retry checks through the wake budget.
- Add signed-DMG release asset manifests/checksums and a macOS installer with read-only verification and JSON summaries.
- Add read-only `acu status --json` for runtime, permission, and island-gate diagnostics.
- Package Claude Code hooks separately from Cursor hooks and avoid duplicate fallback registration.

## 0.2.65 (unreleased, PR draft)

- Dynamic Island gate (free, verify-to-unlock, no paid APIs and no sign-in): "Unlock the island" opens x.com/gurusharan and the prefilled post in your default browser (any browser); you type your @handle once (post link optional) and ACU shows "Checking… usually within the hour" and unlocks by itself. Our side verifies the post for free through public X oEmbed or a Comet search and checks the follow against @gurusharan's follower list, through admin endpoints. LinkedIn route: follow nudge plus share, checked the same way. One person, one Mac, 14-day offline grace, rebind needs a new fresh post and a 7-day cooldown. Tolerates a 30–60 s cold start of the free unlock service ("Waking up the unlock service…"). `acu-island://unlock` deep link opens the unlock screen. While locked the island stays hidden and Samantha shows her state in the menu bar; computer use, Samantha and every other plugin are unchanged. Opt-in "Show me on the site" wall. Backend in `services/acu-gate` (Render free plan + Neon), not deployed. Handle-based claims can be spoofed; accepted tradeoff, see `docs/island-gate-plan.md`.

## 0.2.64 (2026-10-31)

- Settings redesign: General, Agents, Samantha and Computer Use panes plus About. One source table (show and alert level per agent), three alert levels, one alert timer, "Open on" choice, Open at login (SMAppService), done/error peeks only at the critical level, last pane remembered. Old settings migrate on first launch; every allowed value was screenshotted at its extremes.
- Agent-started runs: a run a bot started shows the agent app icon with the starter's icon as a small badge, and a second line like "via ACU · Sol · light · 4m" that drops time, effort, model and "via" in that order when space is short. The tooltip and VoiceOver say who started it. Detection is native: ACU sets Codex's originator (`acu_exec.<bot>`), recorded in Codex's own thread state; no command-line scraping. Screen sharing hides the "via" name.
- Claude on the island: Claude Code via its official hooks (SessionStart, UserPromptSubmit, Notification, PermissionRequest, Stop, StopFailure, SessionEnd; `bin/acu-claude-hook --install` registers them in ~/.claude/settings.json and keeps existing hooks) and the Claude desktop app via an event-driven AX observer of its sidebar status and model picker. Only title, model, effort and state are read; chat contents never are.
- A Codex run whose process exits mid-turn now shows "Stopped", not "Failed", and hook events never mark a run failed. "Failed" means a real failed turn, with its reason.

## 0.2.63 (2026-10-31)

- Island leave fix: a card you opened (or an alert you hovered) now compacts about 0.3 s after the pointer leaves, every time, however long it was open. Root cause: leave detection relied only on NSTrackingArea mouseExited, and the tracking areas and tracking windows are rebuilt whenever the card resizes (rows join, open/close) — an exit that crossed a rebuild never arrived, so the hover flag stayed true and the 0.2.62 hover-hold kept the card open until the next geometry change. While a card or peek is up, a 10 Hz watchdog now checks the real pointer (NSEvent.mouseLocation vs the panel frame) and runs the normal leave path when it disagrees; a backstop leaves if the pointer stays off the island past the grace with no leave pending. Nothing runs while compact.
- Island rows wear the real app icon of the agent's app — Grok Bot, Codex (the ChatGPT/Codex app, found by bundle id com.openai.codex) and Cursor — resolved natively with NSWorkspace (urlForApplication(withBundleIdentifier:) → icon(forFile:)), cached once per app as one small bitmap. Not installed → a neutral SF Symbol. Samantha rows use ACU's own app icon. No bundled third-party logos.
- Hovered peeks dismiss 0.3 s after the pointer leaves (was 1.5 s); unhovered alerts and peeks keep their timers. island-events.jsonl now logs closed, pointer-left (via exit/watchdog/backstop) and exit-lost.

## 0.2.62 (2026-10-31)

- Island hover-hold: a card you open (click or hover-expand) stays open while the pointer is over it and compacts about 0.3 s after the pointer leaves. Root cause: every hub snapshot with Samantha idle reset the "user opened" flag, and since 0.2.61 roster ages change every second, so the roster closed within 1–2 s. Data refreshes and answered asks no longer close the roster you opened.
- Alerts (error/done peeks) keep their timed auto-collapse; the pointer entering one switches it to hover-hold. The empty "No agents running" card waits for the pointer to leave. The leave grace is 0.3 s (was 0.12 s), and the leave timer re-checks the real pointer before closing.

## 0.2.61 (2026-10-30)

- Posted rows with a meta line (Cursor) now age on the island's 1 s clock, like Grok Bot rows; before, "· 34s" froze until the next post.

## 0.2.60 (2026-10-30)

- Model meta on every Codex and Cursor row, done rows included ("Sol · light · done 2m", "Grok 4.7 · high · fast · done 1m"). Codex app threads use the app's own effort labels (low → Light, xhigh → Extra High, per the Codex docs); terminal threads keep the CLI's values. Cursor rows add Fast from the hook's model params, or from the chat's own modelConfig (read-only) for ids the payload leaves out; the stop hook resends the meta. Claude slugs like `claude-4.5-sonnet` now read "Sonnet 4.5".

## 0.2.59 (2026-10-30)

- Grok Bot status: read the app's ARIA status region (AXApplicationStatus, aria-live polite) wherever it sits in the top six levels of the page; 0.2.58 looked only two levels down and missed it. 0.2.58 was built but not tagged.

## 0.2.58 (2026-10-30)

- Grok Bot rows hold for the whole turn: the sidebar's ", Working" label shows only while a reply streams, so a Bot running a long tool call dropped off. The island now also reads the app's screen-reader status "<Bot> is working" for the open conversation (accepted only for a Bot in the sidebar). 0.2.57 was built but not tagged.

## 0.2.57 (2026-10-30)

- Grok Bot agents show on the island while they work: a row per Bot titled with its name ("Agent Computer Use"), meta "Grok Bot · 3m", dropped as soon as the Bot stops. Source: Grok Bot's documented sidebar "Working" status, read from the app's accessibility tree through one AXObserver (event-driven, no timers, no clicks). Bots need to post nothing.
- Cursor agents show on the island: the plugin ships Cursor hooks (`hooks/hooks.json` → `bin/acu-cursor-hook`). `beforeSubmitPrompt` posts a working row with the Cursor icon, the chat's name (read-only from Cursor's state DB, else the trimmed prompt) and "Grok 4.7 · high · 1m" meta; `stop` marks it done, failed or clears it; `sessionEnd` clears it.
- `island-post --meta` sets a running row's meta line; the island appends the live age.

## 0.2.56 (2026-10-30)

- Cursor IDE marketplace installs work: Cursor's plugin cache writes `.cache-complete` into the plugin root, which the runtime contract hashed as package content, so the MCP launcher failed with `RUNTIME_CONTRACT_FAILED: package/app build mismatch` and Cursor showed "Connection closed". Harness marker files at the plugin root are no longer part of the build identity.

## 0.2.55 (2026-10-30)

- Approval cards are sticky, so in 0.2.54 a click on the pill while an approval showed only pinned the ask card. A click now opens the roster with the needs-you row on top (choice cards being typed into keep their clicks); ⌃⌘I still opens the ask card.
- Roster rows have an accessibility action, so VoiceOver can open a row (thread link, ask card, or clear an error row) like a click.

## 0.2.54 (2026-10-30)

UI & UX review fixes on the accepted 0.2.53 roster:
- Grok Bot asks show Grok Bot's own icon and name (row, pill circle, ask card and VoiceOver), not Comet.
- The pill's VoiceOver label leads with priority: "Agents: 1 needs you, 1 error, 2 running".
- Codex titles are cut at the first colon or sentence end, then at a word boundary ("Audit release notes"); `sessions` titles too.
- Pill badge: amber #FFB340 with the needs-you count, including the ask the pill itself shows; red only for errors.
- Error rows clear when clicked open, otherwise after 30 min.
- A Codex turn whose process exited shows as error "Stopped" (dropped if never seen working), never as working. ChatGPT-app threads count as stopped when no app-server runs.
- "+N more" is a button (11 pt, 60 %, hover state) that grows the card to every row; it scrolls only past the screen height.
- With an ask pending, clicking the pill opens the roster with the needs-you row on top; ⌃⌘I still opens the ask card.
- Clicking the empty pill opens a small "No agents running" card (13 pt, 60 %, centred) that closes after 3 s or when the pointer leaves.
- Needs-you meta is "Grok Bot · 2m"; error meta is human words (`island-post --detail`, or "Connection lost" / "Turn failed" for Codex), with the technical detail in the tooltip (`island-post --error`) and in `sessions` (`error`).

## 0.2.53 (2026-10-30)

- Hub error rows show the posted error as their meta line and stay 30 min; hub done rows fade after 3 min (was 30 s). Rollout tails widen to 4 MB when the last 256 KB holds no turn event, so a long instruction preamble no longer hides a working thread.

## 0.2.52 (2026-10-30)

- Dynamic Island roster rebuilt to the UI & UX spec: only sessions a person acts on (needs you, error, working, done within 3 min); idle threads, PIDs, app-server hosts, the "Detected local processes" section, "unknown" fields and ACU's own probe runs are gone. Max 3 rows plus "+N more"; needs-you rows are never capped. Order: needs you (oldest ask first), error, working (newest first), done.
- Row: app icon, task title, meta line `Luna · low · 4m` (product model name and effort, unknown parts omitted, "+N helpers" folded in), state shape (amber !, red x with the error line, spinning ring, green check `done 2m`). Needs-you rows answer in place with Approve and Deny (Deny first for irreversible asks).
- Row click opens the Codex thread via `codex://threads/<id>`; otherwise it focuses the app. Hover shows repo · branch and tokens. Hide while sharing reduces rows to "Codex · working". VoiceOver reads "Codex, needs approval, <ask>. Luna, low, 4 minutes."
- New read-only `sessions` MCP tool: everything the island hides (pids, hosts, cwd, repo, branch, CODEX_HOME basename, tokens, context %, idle threads, probes flagged `probe: true`), never emails or account ids.
- Codex thread state comes from rollout events (task_started / task_complete / error); only rollouts updated in the last 30 min are read.

## 0.2.51 (2026-10-30)

- Grok Build now loads the `agent-computer-use` MCP server. Grok reads the root `plugin.json` before `.grok-plugin/plugin.json`, so the Grok pointer was never read and Grok found no `.mcp.json`; the old pointer also used `${CURSOR_PLUGIN_ROOT}`, which Grok does not expand. The package ships a root `.mcp.json` with `${GROK_PLUGIN_ROOT}/bin/agent-computer-use-mcp`. A repo test fails when any harness manifest's MCP config uses another harness's variables or a cwd-relative command.
- Dynamic Island "Detected local processes": Codex threads are listed, not processes. Each row shows the thread title (or first prompt line), model, reasoning effort, working (rollout updated in the last 90 s) or idle, and the last update, read-only from the Codex state DB (`state_5.sqlite`, `mode=ro`, metadata columns only) and the rollout's latest `turn_context`. Threads are grouped under their host: terminal `codex` processes (mapped by the rollout they hold open, PID shown) or the ChatGPT app. The ChatGPT app's `codex` app-server processes appear only as one "Codex host" line. Cursor rows say "model unknown".
- Retired `bin/cua-driver-mcp` (raw cua-driver wrapper, diagnostic only).

## 0.2.50 (2026-10-30)

- Dynamic Island stays visible while the Dock is showing. The Mission Control check counted the Dock's own window (display-sized, Dock level 20, on screen whenever the Dock is shown), so with "Hide in Mission Control" on the island was ordered out whenever the Dock appeared, and permanently with Dock auto-hide off. Mission Control is now detected from Window Server's `ExposeShieldWindow` (layer 19). Island hide/show transitions are logged with their reason (`island_env_hidden` / `island_env_shown`).
- App resolution skips app extensions and XPC services. With only WhatsApp's `ServiceExtension.appex` running, "WhatsApp" resolved to the extension and the launch failed; it now resolves and launches `WhatsApp.app`.
- Several instances of one app (a second TextEdit beside yours): input and state calls address the exact process with a `pid:<n>` selector instead of refusing. CUAService resolves `pid:<n>` to that process only.
- Tests: every test `UserDefaults` suite is removed with its plist at the end of the run (runs used to leave one plist per suite in `~/Library/Preferences`), and unit tests no longer write to the live `~/.cache/macos-cua/samantha-activity.jsonl`.

## 0.2.49 (2026-10-30)

- Dynamic Island accessibility: the pill and the agent circle are single VoiceOver buttons with labels, values and a press action; the panel reports a floating-window subrole.
- VoiceOver announcements for approvals, choices, needs-you, errors, results and the 60 s expiry warning, at most one per 3 s. Extra announcements are coalesced by priority, never dropped, so a routine message cannot hide an approval.
- With VoiceOver on, error and result peeks stay until dismissed (moving the pointer away no longer closes them).
- "Show Dynamic Island" menu-bar item that opens the island; the ⌃⌘I shortcut is rebindable in Settings › Island › Keyboard.
- Contrast ladder 100/70/60/15/8 everywhere; Increase Contrast and Differentiate Without Colour are honoured (outline, text and shape badges).
- Hit targets of at least 28 pt (compact) and 44 pt (cards). Deny gets first focus only for irreversible requests, ⌘↩ approves, Esc closes the card and leaves the approval pending.
- Hover uses NSTrackingArea instead of 15 Hz polling, and re-checks the pointer when the island changes shape.
- The agent badge counts only agents that need you; with no agents there is no circle. Empty roster reads "No agents running"; process rows lose the dead trailing glyph.
- Legacy "macos-cua Operator" helper retired: preflight moves its LaunchAgent, app and state to the Trash once; doctor no longer checks it; the old operator menu is gone.
- `install_harness.py cursor-plugin` no longer copies into Cursor directories; it moves the stale `~/.cursor/plugins/local/agent-computer-use` copy to the Trash and points `~/.local/bin/agent-computer-use-mcp` at the installed plugin.
- Marketplace prep: Grok manifest, README overview, permissions list (Accessibility, Screen Recording with relaunch, Automation per app, optional Microphone, Calendars, Focus), secret templates moved to `config/secret-templates/` (local `config/.secret/` is gitignored).
- Copy: Samantha is described as ACU's voice runtime on GPT-Live-1 (default) or Grok Voice 3.0. GPT Realtime was removed on 2026-10-21 (ad7414a).

## 0.2.30 – 0.2.48 (2026-10-22 – 2026-10-25)

- Cross-harness agent roster and shared Dynamic Island credential workflow.
- Samantha hardening: audible latency measurement, connection diagnostics, island states.
- One package and running-service contract; read-only contract probes; isolated media builds; release gate with installed live evidence.

## 0.2.29 (2026-10-22)

- Island layout, secure credential entry, workflow store, volume read-back.

## 0.2.27 – 0.2.28 (2026-10-21 – 2026-10-22)

- Plans after ⌘N target the new window; new-document guard; pinnable build number. Samantha validation and microphone permission fixes.

## 0.2.26 (2026-10-21, tag `agent-computer-use-v0.2.26`, first notarized GitHub release)

- Settings UX review fixes and island brand logos.

## 0.2.23 – 0.2.25 (2026-10-21)

- One ACU Settings window (voice and Computer Use panes), onboarding with one-time approvals, TCC audit, Developer ID release step.
- Voice: GPT-Live-1 and Grok Voice 3.0 behind a provider adapter; GPT Realtime removed (GPT-Live-1 is the default OpenAI path).

## 0.2.12 – 0.2.22 (2026-08-23 – 2026-10-11)

- Samantha runtime packaged in the plugin; one CUAService instance; compact verified AX deltas and typed failure feedback; actions pinned to their window and fail closed without evidence.

## 0.2.8 – 0.2.9 (2026-08-16)

- First portable Agent Plugin release with the `state` and `act` MCP tools.
