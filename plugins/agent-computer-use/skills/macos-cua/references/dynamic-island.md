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
- **Codex**: thread rows from Codex's own rollout files (see `sessions`).
- Anything else posts with `island-post`; `--meta "Model · effort"` gives running rows a meta line with the live age.

Meta lines: `<model> · <effort>[ · fast]` from the source, then the age (running) or `done <age>` (done). Codex app threads show the app's effort labels (low → light, xhigh → extra high); terminal threads show the CLI value. A field the source does not have is left out, never guessed.
