# Changelog: agent-computer-use

Built from git tags and commit history (`git log -- plugins/agent-computer-use`). Dates are commit dates. Earlier untagged versions are grouped.

## 0.2.65 (unreleased, PR draft)

- Dynamic Island gate (free, verify-to-unlock): the island unlocks after Sign in with X (follow @gurusharan + post the preset) or, with no X account, Sign In with LinkedIn (share the preset + paste the post link, reviewed when it can't be auto-checked). One person, one Mac, 14-day offline grace, safe "Move to this Mac" rebind. While locked the island stays hidden and Samantha shows her state in the menu bar; computer use, Samantha and every other plugin are unchanged. Opt-in "Show me on the site" wall. Backend in `services/acu-gate` (Render + Neon), not deployed. Plan: `docs/island-gate-plan.md`.

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
