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
  `<plugins>/<id>/assets/logo.svg` (`~/.agents/plugins`, `ACU_PLUGIN_LOGO_ROOT`).
- Inside the pill (over the idle/done-settled lead slot, drawn at real size): what
  the plugin controls on a 12 pt white tile in a 20 pt spinning ring — Apple mark for `acu` (the Mac), Comet for `comet-control`;
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
  Fixed gaps (UI & UX): ring, 16 pt, 16 pt icons 4 pt apart, 6 pt, the 6 pt dot,
  9 pt inset. The gap after the ring (12 pt in 0.2.85, Guru 2026-11-04 10:22 IST:
  "there has to be some gap ... so its clear whats being used"; after 11:08 IST
  "should be more" UI & UX picked 16 pt with no divider at 11:57 IST from a
  12 / 16 / 20 study: a clear break without wasting width) is the same for
  every controlled target, the Apple ring for ACU and the Comet ring for
  comet-control alike (one shared `IslandBarLayout` path; "same design language
  should follow for all"). It exists only while apps are in use. With a controlled
  target but no apps (comet-control reading a page) the pill hugs ring · 8 · dot · 9,
  49 pt, and never stretches to the 136 pt acting width. The compact pill hugs exactly that in
  every state, Samantha's included (79 pt for one, 119 for three, 139 for 3 + N); "+N" is SF Pro
  Semibold 11 at white 62 % in a fixed 16 pt slot. A near-white or missing icon
  (TextEdit; detected generically by `AppIconResolver.isBlank`) draws on the
  target mark's white rounded tile with a dark glyph. Width changes from icons
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
  0.6 → 1 plus opacity 0 → 1 about the circle's own centre, the whole circle
  (backing, rim, badges) as one layer, leave is the exact reverse and the slot
  closes; Reduce Motion is a 0.15 s cross-fade with no slide or scale. Her mark is
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
