# Changelog: agent-computer-use

Built from git tags and commit history (`git log -- plugins/agent-computer-use`). Dates are commit dates. Earlier untagged versions are grouped.

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
