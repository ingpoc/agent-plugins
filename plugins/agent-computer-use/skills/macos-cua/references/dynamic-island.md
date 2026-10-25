# Shared Dynamic Island

Load this reference when any installed harness should report task/subagent
lifecycle or status to the shared Dynamic Island, request an approval or choice,
or save a credential through its secure form. Samantha is one client of shared
CUAService. CLI workflows require the plugin, installed service, and shell
access to the package.

## Agent roster

Agent Computer Use scans exact process names for common local harnesses and
shows those rows as **harness detected**. That proves only the harness process
is open; it does not prove a task is active. The scan reads process names and
PIDs only, never arguments or environment. Task rows come from explicit
registration and can include parent/child links. A remote child appears when
its parent or the child registers it; process scanning cannot discover remote
workers.

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
disappear; an unclosed registration expires after one hour. The parent ID is
optional for top-level tasks. Keep secrets and private user data out of titles.

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
