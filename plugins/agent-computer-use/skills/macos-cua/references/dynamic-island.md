# Shared Dynamic Island

Load this reference for status, approval, or choice from the supported sources
(Codex, Cursor, Grok, Samantha), or credential capture from a shell-capable
harness with the plugin installed. Samantha is one client of shared CUAService.
Credential capture requires the `agent-computer-use` plugin, installed
CUAService, and shell access to the package.

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
