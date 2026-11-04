# Shared Dynamic Island

Load this reference when any installed harness should report task/subagent
lifecycle or status to the shared Dynamic Island, request an approval or choice,
or save a credential through its secure form. Samantha is one client of shared
CUAService. CLI workflows require the plugin, installed service, and shell
access to the package.

## Agent roster

The roster separates reported agent tasks from best-effort local process
observations. Registered task rows carry their reported status and parent/child
links. Local matches appear under **Detected local processes** with **Activity
unknown** and a PID; they do not count as active agent tasks. Process presence
does not prove that work is active. The scanner reads process names and PIDs
only, never arguments or environment, and labels Comet as Comet without
inferring a Grok agent. A remote child appears when its parent or the child
registers it; local process scanning cannot discover remote workers.

Once, open Agent Computer Use Settings → **AI Agents → Agent Roster**, enable it,
and choose **Rotate** for its token. Every harness uses the shared `agent`
source after this setup.

Load this workflow when starting a task that should appear in the roster, when
launching a child agent, and when each task ends. Use a unique ID per task run;
repost `running` with the same ID to update it. Child IDs point to the parent's
raw ID. A parent can post a remote child's status on its behalf.

```sh
TASK_ID="$(uuidgen)"
python3 "$SKILL_DIR/../../bin/island-post" \
  --source agent --name Codex --id "$TASK_ID" \
  --kind running --title "Implementing the feature"

CHILD_ID="$(uuidgen)"
python3 "$SKILL_DIR/../../bin/island-post" \
  --source agent --name Codex --id "$CHILD_ID" --parent-id "$TASK_ID" \
  --kind running --title "Reviewing the implementation"

python3 "$SKILL_DIR/../../bin/island-post" \
  --source agent --name Codex --id "$CHILD_ID" --parent-id "$TASK_ID" \
  --kind done --title "Review complete"
python3 "$SKILL_DIR/../../bin/island-post" \
  --source agent --name Codex --id "$TASK_ID" --kind done --title "Feature complete"
```

Use `error` when a task fails. Completed and failed rows remain briefly, then
disappear. When a running or `needs_you` row stops updating past its TTL, it
stays visible as **updates stopped** with its last-seen age; its remote controls
are disabled. Dismiss the stale row locally, or repost the same ID to restore
live status. The parent ID is optional for top-level tasks. Keep secrets and
private user data out of titles.

The compact pill names the highest-attention agent and state. The expanded
roster sorts attention-needed branches ahead of healthy work, labels each
child's parent, and keeps children beside that parent. Pending approval and
choice rows open the full request; activating a roster row never answers it.
Stopping shows the full agent/task target plus **Confirm stop** and **Keep
running**. Failed Stop and Retry requests remain marked **Not sent · Try
again** until that agent publishes a later update.

For a destructive approval, --detail must name the exact affected targets.
The hub rejects new destructive requests without that scope, and the card shows
the full detail before **Confirm approve** can be activated. A restored legacy
request without scope remains denyable, but its approval button stays disabled.

Use **⌃⌘I** to navigate the open island with arrow keys or Tab. Navigation
covers every actionable roster row in display order and scrolls focused rows
into view. VoiceOver labels include the task, state, and parent relationship.

## Plugin marks (right side)

A plugin an agent is using shows as a circle on the **right of the compact
pill**, after Samantha's app circles. The left side stays the agent harness
(source) circles; a plugin is never drawn there.

- Bodies come from the shared `bin/island_activity.py`: `call_started(plugin,
  agent, key)` holds the row while a call runs, `call_finished(...)` leaves it 2 s
  (`CLEAR_AFTER_S`) after the last call, ok or failed; `start`/`end`/`pulse`/
  `linger` for long-lived uses. A post is source `agent` with keys `id`
  (`plugin:<plugin>:<key>`), `kind` (`running`/`clear`), `name` (the agent),
  `app` (the plugin's island app name), `title` (`Using <Plugin Name>`), `meta`,
  `plugin`, and `ttl_s` (plugin rows may go down to 1 s; other rows keep 5 s).
- Agent names go through the one shared `normalize_agent` (split on space, `-`,
  `_`, `.`; lowercase words capitalised): "grok-bot" and "Grok Bot" are one name.
- Allowed `plugin` values: `acu`, `comet-control`, `context-ledger`. Any other
  value is ignored and the row stays a normal agent row. One circle per plugin,
  agents grouped. The right mark is the plugin's own: ACU's app icon, else
  `<plugins>/<id>/assets/logo.svg` (`~/.agents/plugins`, `ACU_PLUGIN_LOGO_ROOT`),
  drawn as its BARE glyph on the black circle, never on a white tile (Guru
  2026-11-04 16:06 IST, see the no-tile rule below).
- Inside the pill (over the idle/done-settled lead slot, drawn at real size): what
  the plugin controls, as its bare logo glyph (12 pt box) in a 20 pt spinning ring — the white Apple logo for `acu` (the Mac),
  the Comet mark lifted from Comet's own icon for `comet-control`;
  `context-ledger` controls nothing and has no inside mark (Guru 12:05 IST). The
  most recently seen controlling use wins the ring; VoiceOver names every
  target, newest first ("Controlling Comet and Mac").
- Placement: right circles hang off the pill's right edge, **outside** the
  pill; the inside mark uses the existing left lead slot. The 6 pt compact
  status dot at a 9 pt inset from the right end is untouched.
- Names in title case: Agent Computer Use, Comet Control, Context Ledger.
  Tooltip: `<Plugin Name> · <agents>`. "Using <Plugin Name>" appears only in
  VoiceOver (`Using Comet Control: Claude and Codex`) and in the expanded
  island's row per use, never in the compact pill. Tapping a circle does nothing.
- Dot: a plugin in use lights the blue #64D2FF working dot; red and amber still
  win.
- Samantha, same language (Guru 2026-11-03 13:10 IST): voice-cua's
  `plugin_island.py` posts her `cua_*` tools as `acu` and `comet_*` tools as
  `comet-control` through `island_activity.py` (agent "Samantha"). While she
  acts: her circle on the left, the target ring inside (the progress ring and
  driven-app lead icon give way), the 6 pt blue dot as the trail, the plugin mark
  on the right. Comet is no longer a right-side app circle.
- Operated Mac apps, any agent (Guru 2026-11-03 13:27 IST): each ACU `act` /
  `state` call sends its `app` as `target_app` on its own row (key
  `<agent>:<pid>:app:<app>`), so every app clears 2 s after its own last call.
  The real app icons (NSWorkspace, the same `SamanthaAppIcon` component as the
  app circles) sit INSIDE the pill on its right, newest first, 3 then "+N".
  **Locked zone rule** (Guru 2026-11-04 13:30 / 15:26 / 15:52 IST): inside the pill, left to
  right, the ringed target, 24 pt, the app icons (16 pt, 4 apart), 8 pt, the voice
  slot, 6 pt, the 6 pt dot, 9 pt inset. The voice slot and the dot travel together
  as one group at the right end, the slot 6 pt before the dot in every voice state
  (listening, thinking, Samantha speaking) with or without a target: Guru approved
  the live Comet + listening frame as the reference at 16:05 IST ("it makes sense,
  where comet icon is on the left and listening has moved along with dot"; 6 pt
  measured in that frame). Whatever comes first after the ring (apps, the
  voice slot or the dot) starts the same 24 pt after it, one shared constant
  (`IslandBarLayout.operatedRingGap`; "Use the same gap between the ring and the
  dot", then "increse from 20 to 24pt"; 0.2.86 used 8 pt with no apps, which read
  as squeezed). Harness circles sit outside left; only plugin marks sit outside right.
  The gap is one shared `IslandBarLayout` path for every controlled target, the
  Apple ring for ACU and the Comet ring for comet-control alike ("same design
  language should follow for all"; 12 pt in 0.2.85, 16 pt in 0.2.86, 20 pt until
  15:52 IST). Widths: a target alone hugs ring · 24 · dot · 9 = 65 pt (never the
  136 pt acting width); a target with the voice slot and no apps ring · 24 · slot ·
  6 · dot · 9 = 94 pt speaking/listening, 85 pt thinking; with apps 87 / 127 / 147 pt
  for 1 / 3 / 3 + N (118 / 158 pt with the meter).
  **Target glyph rule** (Guru 2026-11-04 15:27 / 15:29 IST, "that is the design
  language"): every controlled target, current or future, draws its BARE logo glyph
  in the ring on the black pill, never on a white tile or squircle. One path,
  `IslandTargetGlyphView`: an SF Symbol target (ACU's `apple.logo`) in white; an
  app target (Comet) as the coloured mark lifted from the app's real installed
  icon (`IslandTargetGlyph.bareGlyph`: keeps the coloured pixels, drops the dark
  plate and white specks, crops to the mark), else the app's SF Symbol in white.
  **No-tile rule for plugin marks** (Guru 2026-11-04 16:06 IST: "the plugin icon is
  coming in the white square bar, rather have the icon, since currently is not
  visible properly"): every outside-right plugin mark, current or future (ACU,
  Comet Control, Context Ledger, developer-observability, and the manifest-icon and
  monogram fallbacks), is its bare glyph on the black 24 pt circle, never a white
  tile. One path, `IslandPluginMarkGlyphView`: the plugin's logo or icon with its
  plate lifted off (`IslandPluginMarkGlyph.bareMark`: the plate colour is the
  median of a ring inside the icon; dark neutral strokes turn white for contrast
  on black, colours stay, lifted to at least 55 % brightness); developer-
  observability's vector trace-waterfall in #30D158 (was #248A3D on white); else
  the plugin's SF Symbol in white.
  `IslandTargetGlyphTests` renders every target in its ring and every plugin mark
  on its circle and fails on a tile.
  The compact pill hugs exactly that in
  every state, Samantha's included (87 pt for one, 127 for three, 147 for 3 + N); "+N" is SF Pro
  Semibold 11 at white 62 % in a fixed 16 pt slot. A near-white or missing icon
  among the operated APPS (TextEdit; detected generically by
  `AppIconResolver.isBlank`) draws on a white rounded tile with a dark glyph; that
  tile is for app icons only, never for the ringed target. Width changes from icons
  spring over ~250 ms straight to the fitted width (no 160 ms squeeze to 74 pt;
  crossfade with Reduce Motion); icons fade in and out over 200 ms.
- Left is harness-only: a row posted under a plugin identity (ACU, Agent
  Computer Use, Comet Control, Context Ledger) maps to its calling harness (the
  `acu_exec` initiator) or is dropped; Samantha's circle wears her waveform
  wave mark, never ACU's app icon. VoiceOver adds
  "Operating Calculator, Finder and TextEdit". An app shown inside is never
  drawn again as an outside app circle (Samantha's included). Plugin marks stay
  outside on the right.
- Samantha is a harness (Guru 2026-11-04 10:23 IST): her circle sits on the
  LEFT, outside the pill, with the other harness circles; never inside. It
  appears as soon as her session is on (hub `samantha_on`, set from CUAService's
  own session state, never from her posts), through the same source-group list,
  `ForEach` and transition as Codex, Cursor or Grok (Guru 11:09 IST), and
  leaves when the session stops. Harness motion (`IslandMotion.harness*`, one
  path for every harness): `.smooth(duration: 0.35)` (`widthCurve`), the same
  transaction that slides the neighbours one slot (circle + 4 pt); enter is scale
  0.6 → 1 plus opacity 0 → 1, the whole circle (backing, rim, badges) as one
  layer (`HarnessSlotEffect`), kept inside its own opening slot: the free space
  between the sliding neighbour and the pill is shared 4 : 6 as at rest
  (`IslandMotion.harnessFrame`), so it never sits under the pill and lands
  exactly on its slot. It draws above the neighbours it pushes
  (`harnessDrawOrder`, also while leaving). Exit (shared by every harness,
  UI & UX 2026-11-04 12:40 IST): the circle scales 1 → 0.6 and fades 1 → 0 in
  place with `.easeIn(duration: 0.15)`, then the slot closes with
  `.smooth(duration: 0.3)` starting 0.10 s after it (0.40 s in all); no
  neighbour overlaps it while it is above 10 % opacity (min 2.54 pt). The
  circles live in their own layer so the removal always runs on that exit,
  whatever else animates in the same update (`harnessExitSeq`). Reduce Motion:
  any update that adds or removes a harness circle snaps the layout, pill width
  included (no slide, no scale), and only the entering or leaving circle fades
  over 0.15 s. Her mark is
  her own (Guru 11:09 IST: "the style of samantha should be distinct"; 11:54 IST
  picked the wave): `SamanthaMark`, one tapered sound wave, 2 pt round-cap stroke,
  60 % of the circle wide, vertically centred, gradient #BF5AF2 → #FF6482 left to
  right, in the same 24 pt circle and rim as every harness. Never a white plugin
  tile or ACU's app icon. The
  target gap applies only to what sits inside; plugin marks stay outside right.
  A controlled target (Comet) is never also shown as an app icon inside.
- Drop: the hub removes a plugin row when `ttl_s` lapses (1 s sweep); the
  circle fades out over 200 ms. Plugin rows never go stale and never show
  "updates stopped".
- `ACU_ISLAND_ACTIVITY=0` turns the signal off; tests are off by default via
  `scripts/check.py`.

## Island layout log (`island_layout`, schema 1, 0.2.85+)

0.2.85 is the first ACU version that logs the island's geometry, so
developer-observability can flag a mark drawn outside the island. CUAService
appends one row to `~/.cache/macos-cua/samantha-activity.jsonl`:

```json
{"ts":"2026-11-04T10:30:00.123+05:30","ts_epoch":1793770800.123,"event":"island_layout","kind":"island_layout",
 "source":"cuaservice","schema":1,"session_id":"70899191","turn_id":null,"version":"0.2.85","trigger":"show",
 "screen":{"id":"1","x":0,"y":0,"w":1512,"h":982,"scale":2},
 "island":{"x":718,"y":6,"w":75,"h":24,"visible":true},
 "overlays":[{"kind":"comet_circle","id":"comet_circle","x":724,"y":8,"w":20,"h":20,"visible":true,"expected_inside_island":true}]}
```

- Rectangles only, in global points with the origin at the top-left of the
  primary screen; numbers only. No titles, app names, URLs or text.
- `trigger`: `show`, `hide`, `move`, `resize` (island panel) and `overlay_show`,
  `overlay_move`, `overlay_hide` (the agent cursor, `agent_cursor`).
- Overlay kinds (ids add `_<i>` where there can be several): `target_ring` /
  `comet_circle` and `app_icon` (inside, `expected_inside_island: true`);
  `plugin_mark`, `app_circle`, `overflow_circle`, `harness_circle`,
  `agent_cursor` (outside, false). Tolerance for "inside" is 2 pt.
- Written on change only; `move`, `resize` and `overlay_move` coalesce to at
  most one row per 250 ms, the latest frame winning.
- `ACU_ISLAND_LAYOUT_LOG=0` turns it off; tests write to a temp file.

## Status, approvals, and choices

Use the packaged `bin/island-post` command. Reuse one stable task ID to update
the same status row, and finish it with `done` or `error`. Keep secrets and
private credential values out of titles, details, prompts, and choices.

```sh
python3 "$SKILL_DIR/../../bin/island-post" \
  --source codex --kind running --id "task-123" --title "Preparing the update"

python3 "$SKILL_DIR/../../bin/island-post" \
  --source codex --kind done --id "task-123" --title "Update complete"
```

For a user decision, use `approval` or `choice` and `--wait`; the command
returns the user's decision as JSON. The harness source must be enabled in
Agent Computer Use Settings under **AI Agents**. Use the matching source ID:
`codex`, `cursor`, `grok-bot` (or `grok`), or `samantha`. If the request is
rejected, report that and stop.

## Save a credential

Call `bin/island-credential` with locator metadata only. The secure value is
entered by the user in the Dynamic Island form and saved directly to login
Keychain. This shared command uses the same capture helper as Samantha's
`secrets_put` flow, so other harnesses do not need a separate credential skill.

```sh
python3 "$SKILL_DIR/../../bin/island-credential" \
  --id provider.api_key \
  --label "Provider API key" \
  --service provider \
  --account api_key
```

`--id`, `--label`, and `--service` are required; `--account` may be omitted so
the user can enter it in the form. Choose the intended service before opening
the form and use the right account: saving replaces the value for that
Keychain service/account. The form shows the service and account, and the user
presses **Save to Keychain**.

Never put the credential value in chat, tool arguments, command-line arguments,
environment variables, `type_text`, clipboard, or logs. On success, the command
returns `ok: true` and `stored: true` with locator metadata only. If it returns
`stored: true` with an error, the Keychain write succeeded but the locator was
not recorded; do not retry the save. If the user cancels or the request fails,
stop without moving the secret to another channel.

## Agent rows that need nothing from the agent

- **Grok Bot**: the island reads the Grok Bot app's documented sidebar status ("Working or typing status", https://docs.x.ai/grok-bot/settings-and-notifications). A busy Bot's sidebar button is labelled `<name>, Working`; one AXObserver on the app (title/created/destroyed/live-region notifications, coalesced 0.5 s) re-reads only the `Bot list` group. Row: title = Bot name, meta `Grok Bot · 3m`, gone when the label drops `Working`. No timers; needs the Grok Bot window open (its AX tree), and Accessibility for ACU.
- **Cursor (IDE and CLI)**: documented agent hooks (https://cursor.com/docs/agent/hooks). The plugin's `hooks/hooks.json` runs `bin/acu-cursor-hook` on `beforeSubmitPrompt` (working row: Cursor icon, chat name read-only from Cursor's `state.vscdb`, else the trimmed prompt; meta `Grok 4.7 · high · 1m` from `model_id`/`model_params`), `stop` (done / failed / cleared when aborted) and `sessionEnd` (cleared). The Cursor CLI does not load plugin hooks; register the same script once in `~/.cursor/hooks.json` (`"command": "<home>/.agents/plugins/agent-computer-use/bin/acu-cursor-hook"`) for `beforeSubmitPrompt`, `stop` and `sessionEnd`. Posts are idempotent per conversation, so the IDE running both copies is harmless.
- **Claude Code**: `bin/acu-claude-hook` consumes metadata-only Claude Code hooks for `SessionStart`, `UserPromptSubmit`, `Notification`, `PermissionRequest`, `Stop`, `StopFailure`, and `SessionEnd`. It never reads the prompt or transcript path. Install user hooks with `python3 "<plugin>/bin/acu-claude-hook" --install` (or remove only these entries with `--uninstall`); installer backs up existing settings once to `~/.claude/settings.json.acu-bak`. Session start preserves cached model and title when resume metadata is omitted; prompt submit stores the effective title/model and posts a working row with known model/effort metadata. Permission and input notifications request attention; stop marks done/error; session end clears and removes cached metadata. All posts use source `claude` and stable ID `claude-code:<session_id>`, with the Claude app icon. Initiators are forwarded as `acu_exec.<initiator>` (existing `acu_exec.` prefixes are preserved).
- **Codex**: thread rows from Codex's own rollout files (see `sessions`).
- Anything else posts with `island-post`; `--meta "Model · effort"` gives running rows a meta line with the live age.

Meta lines: `<model> · <effort>[ · fast]` from the source, then the age (running) or `done <age>` (done). Codex app threads show the app's effort labels (low → light, xhigh → extra high); terminal threads show the CLI value. A field the source does not have is left out, never guessed.

Claude desktop activity is observed with AX notifications. Status-labelled buttons are recognized inside sidebar containers or next to matching “More options” buttons. The model popup and composer Stop/Stop response button labels are also observed. A plain chat's sidebar row stays `Idle <title>` while Claude replies (4.4212, free tier): the working signal is the composer `Stop response` button (depth 32 from the app element) or the `Currently streaming message` message group, and the row is named from the header's `<title>, rename chat` button. The walk goes to depth 48 and prunes message groups (`AXDocumentArticle`), reading only their accessible name. Conversation text and AX values are excluded; text, article, heading and composer text-input subtrees are skipped. Model/effort is shown only for a selected, sole working chat; missing selection metadata leaves it blank.

When a working row finishes or its Stop button disappears, the island retains a done row for about ten seconds. Claude launch events re-enable AXManualAccessibility with bounded retries while waiting for windows. `acu status --json` includes `claude_desktop` attachment, last-read time, row counts by status, model-picker presence, manual-accessibility attempts/error, and chat-tab activity. Diagnostics contain counts and timestamps, not conversation labels.


### Gate diagnostics

`acu status --json` keeps the persisted entitlement under `island`. When the running service responds over its existing owner-only Unix socket, `island.live` adds the current gate state, actual `panel_visible`, and the actual NSWindow `panel_frame` (x/y/width/height in AppKit screen coordinates; null before a panel is created). An unreachable or older service leaves the stored status unchanged and omits `live`. The local read has a bounded deadline and never opens a URL or starts the service.

Unified logging category `gate` records state names and panel visibility reasons (`gate_locked`, `setting_off`, `env_hidden`, or `allowed` when shown). Tokens, handles and error messages are excluded. A verified, unexpired entitlement continues enabling the island during non-revoking refresh failures; explicit revoked, moved-device and missing-binding responses still remove access. Signed expiration remains authoritative.
