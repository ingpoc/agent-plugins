# Samantha test run

Trigger: run or validate Samantha through prompts, investigate an incident, or accept a candidate before handoff. Read the installed runtime version first; preserve other agents' work and the user's active session.

## 1. Preflight and baseline

1. If the developer-observability MCP is configured, discover and select the Samantha project, query issues and inspect each target by full issue ID. Save the snapshot timestamp, issue ID, last-seen timestamp and count scope. Use the Kindle project's read-only owner doctor or skill for source details when available. If neither source is available, record that the telemetry baseline is unavailable; do not infer a fix from test-only evidence. Follow owner documentation rather than assuming a device mount is necessary for Mac findings.
2. Verify installed identity with the entrypoint's runtime-contract command. Record the version/fingerprint. Never replace or restart another driver's session; ask the operator to finish an active test before installing.
3. Record the activity-log byte offset, start time and eventual session ID. Logs are `~/.cache/macos-cua/samantha-activity.jsonl` and `voice.log`; keep private evidence outside the public repository in a timestamped task artifact directory.
4. For API behavior or uncertain recommendations, use `openai-docs` and current official GPT-Live documentation. Relevant owners: [session lifecycle](https://developers.openai.com/api/docs/guides/live-conversations), [delegation](https://developers.openai.com/api/docs/guides/live-delegation). Do not apply legacy Realtime assumptions to `gpt-live-1`.

## 2. Choose one test path

**Existing voice session:** the operator turns Samantha on. Discover the actual gateway port from the helper startup record/listener; confirm `/health` identifies `service=voice-cua` and the expected version, then require `/api/session/status` `ready: true`. Never guess port 8765 or kill its unrelated owner. The operator speaks for microphone/speaker acceptance; agents can submit typed prompts to the same session but must label them typed.

**Isolated installed-helper text test:** require no active Samantha helper and no other live driver. Set `H` to `/Applications/Agent Computer Use.app/Contents/Helpers/voice-cua.app/Contents/MacOS/voice-cua`. Select a verified free localhost port and retain the launched process/session handle:

```sh
VOICE_CUA_PORT="$PORT" "$H" --no-audio
```

This command runs until shutdown; it is not a one-shot prompt command. Use another tool invocation for the API, wait for health and session readiness, and record the installed helper's session ID. No microphone capture or speaker playback is tested. A terminal runner must keep the process alive; do not launch an orphan child and assume it survived.

Submit one prompt at a time:

```sh
curl --fail-with-body --max-time 100 -sS \
  -X POST "http://127.0.0.1:$PORT/api/session/text" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Reply with exactly: test ready.","timeout":90}'
```

A direct `/api/tools/call` checks the local tool boundary, not model selection/delegation. Keep that evidence separate.

## 3. Bounded prompt set

Run only rows relevant to the change. Start with the smoke prompt, stop on failure, preserve evidence, and diagnose before expanding or retrying. A diagnostic follow-up never turns the original failed attempt into a pass.

| Test | Prompt/action | Required outcome |
| --- | --- | --- |
| Conversation | “Reply with exactly: test ready.” | Completed answer; no API/session error. |
| Native action | “Open Calculator and compute 8 times 8.” | Relevant tool succeeds and independent app readback shows 64. |
| Volume read | “Read the current volume only; do not change it.” | `system_volume` read succeeds; no write. Test changes separately with operator authorization and restore prior state. |
| Credential lookup | “List credentials for platform samantha-verification-nonexistent; report only the count.” | Platform-filtered tool completes without exception. Keep labels/values out of the report. |
| Browser journey | “Open https://example.com in your isolated Comet browser, read its heading, then close that test window because I am finished with it.” | On this first request: begin → page read → correct heading → closeout; broker confirms no owned test sessions/windows remain. Opening alone is incomplete. |
| Follow-up during work | Give one harmless correction while a bounded task is running. | Corrected outcome; no duplicate side effect or unsupported cancellation claim. Do not test against real accounts. |
| Credential form | Ask for secure entry with synthetic metadata; see below. | Actual first-click typing, tab/focus return, cancel and privacy behavior, not just a pending receipt. |
| Voice continuity | Operator speaks, pauses 20–30 seconds, resumes, and interrupts a reply. | Recognized input, audible output, correct continuation; record drops, recoveries and observed duration. A 5–10 minute run is a sample, not proof against future drops. |

## 4. Credential acceptance

Use a dedicated synthetic service/account and dummy value only. Record its exact locator before testing. No real Keychain enumeration, login or overwrite is implied by a prompt test.

1. Request the form; required metadata includes `id`, `label`, `service`. Verify the panel is visible and capture privacy is active.
2. Click each field and type using keyboard events. AX value-setting and model/unit tests do not prove first-click keyboard entry. Confirm the account binding and Save enabled state without reading the secure value.
3. Click elsewhere, return, and verify typing resumes. Cancel; verify no save. Stop automation on user intervention or changed element identities; refresh the UI before any further action, especially Save.
4. For a separately authorized dummy save: click Save deliberately, verify a stored receipt plus catalog locator, and resolve/reuse it through the local injection path. The model sees metadata and outcome only. Verify the intended target field and final login outcome separately; masking alone does not prove successful login. Use an approved test target; never inject into an unrelated app/site.
5. Remove only the exact test item/metadata this run created, preserving pre-existing entries. Unknown account or saved state: stop and report uncertainty; never guess which Keychain item to delete.

## 5. Evidence and Kindle comparison

For every prompt retain input, elapsed time, session ID, tool start/result/error delta, requested outcome readback and cleanup result. Keep any private response content local. Record first attempt and follow-up independently.

For a transport drop inspect `ws_closed`, safe request/edge IDs, close-frame flag, last server event, successful client/microphone-send ages and ping timing sample. Distinguish intentional shutdown from an unrequested EOF. These fields support diagnosis; they do not establish a network or upstream cause alone.

Refresh the Kindle report after its next snapshot and compare last-seen timestamps against the run interval. Use its adapter/proof docs if ingestion lags. Separate historical rows, user cancellation, mocked-test contamination and actual new failures. Do not erase history to make the dashboard green.

A task may return HTTP 200 and every tool may return `ok`, yet the requested journey is incomplete. Record such failures even if Kindle has no new card. Kindle is a collated incident source, not the sole acceptance oracle.

## 6. Closeout

Close only this run's browser lease; require verified absence and an empty owned inventory. Stop only the helper/process this run started, allowing graceful session closure; leave the user's session alone. Preserve evidence and restore reversible settings. Check the Kindle doctor again after any surprise.

Report each row as passed, failed, incomplete or not tested, with source/test/installed/live proof. Name the installed candidate, observed test duration, unresolved causes and remaining manual checks. No build, short clean session, or successful follow-up substitutes for the missing proof.
