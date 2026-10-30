# agent-computer-use

Native macOS computer use through a two-tool MCP facade over compact AX-first macos-cua workflows, with the self-contained Samantha GPT-Live 1 voice runtime.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: [`AGENTS.md`](AGENTS.md). Collection routing: repo-root `AGENTS.md`.

## Overview

**Purpose.** Lets an agent see and operate native macOS apps. The `agent-computer-use` MCP server exposes three tools: read-only `sessions` (local agent sessions: Codex threads with model, effort, state, repo and tokens, plus harness processes), `state` (a compact Accessibility snapshot of one app) and `act` (a batch of actions that presses Accessibility elements first and falls back to pointer and keyboard input, then returns a verified delta). The `macos-cua` skill tells the agent how to use them. Optional: Samantha, ACU's own voice runtime (`runtime/voice-cua/`, GPT-Live-1 by default or Grok Voice 3.0) that drives the same tools by voice.

**Requirements.** macOS 13 or later (tested on Apple silicon), `python3` on `PATH`, and the CUAService helper app (**Agent Computer Use.app**) installed. It asks for its macOS permissions once, in its onboarding window. Not supported on Linux or Windows.

**Install.**

1. Install the `agent-computer-use` plugin from this repository's marketplace. Per-client commands (Codex, Cursor, Grok Build, Agent Plugins Installer): repository root [`README.md`](../../README.md#install).
2. Install CUAService. Testers use the notarized build from the repository's GitHub release (download the DMG, drag **Agent Computer Use** to Applications, open it). Maintainers can build from source with `python3 skills/macos-cua/service/install_service.py --team <YOUR_TEAM_ID>`. That needs Xcode Command Line Tools and a code-signing identity for that Apple Developer team.
3. Grant permissions in the onboarding window (it opens on first launch; also ACU Settings › Computer Use). Required: **Accessibility** and **Screen Recording** (quit and reopen Agent Computer Use after granting Screen Recording; macOS applies it only on relaunch). **Automation**, per app, the first time an agent drives that app (for example Music, Finder, Comet, System Events). Optional: **Microphone** (Samantha voice only), **Calendars** and **Focus** (island extras). Every prompt is macOS's own.
4. Restart the client. The plugin should list three tools, `state`, `act`, and read-only `sessions`.

**Configuration.** Nothing is required. `AGENT_COMPUTER_USE_PYTHON` picks the Python interpreter. For voice only, store an OpenAI API key (GPT-Live-1) or an xAI API key (Grok Voice 3.0) in the macOS login Keychain with the `runtime/voice-cua` secrets CLI. `runtime/voice-cua/config/secret-templates/` holds metadata only (label, Keychain service, account) and never a value.

**Network endpoints and credentials.** `state`, `act`, and `sessions` stay on the machine. They talk to the local CUAService process, and nothing is uploaded. The voice runtime, when you turn it on, calls OpenAI GPT-Live at `wss://api.openai.com/v1/live/sessions` with the Keychain OpenAI key (or, if you pick Grok Voice, `wss://api.x.ai/v1/realtime` with a Keychain xAI key). The developer-only benchmark `skills/macos-cua/scripts/bench_jev_act.py` calls `https://ai-gateway.vercel.sh` with your own gateway key. Runtime counters and logs stay local under `~/.cache/macos-cua/`. There is no remote telemetry.

**Testing.** From this directory: `python3 -m unittest skills/macos-cua/tests/test_plugin_package.py skills/macos-cua/tests/test_jev_act.py skills/macos-cua/tests/test_cua_service_health.py`. Live check: ask the agent to "open Calculator and compute 12 × 3 with agent-computer-use". It should call `state` and then `act`, and the display should read 36.

**License.** MIT (repository root [`LICENSE`](../../LICENSE)).

## Package boundary

Cursor MCP loads `.cursor-plugin/mcp.json` (`${CURSOR_PLUGIN_ROOT}/bin/agent-computer-use-mcp`, `cwd` `${CURSOR_PLUGIN_ROOT}`). Portable root `mcp.json` stays `./bin/agent-computer-use-mcp`. `python3 skills/macos-cua/scripts/install_harness.py cursor-plugin` retires the stale `~/.cursor/plugins/local/agent-computer-use` copy (to the Trash) and never copies into Cursor dirs. Details: [`AGENTS.md`](AGENTS.md).

`runtime/voice-cua/` owns Samantha's Python runtime, metadata-only secret templates, unit test, and PyInstaller builder. `skills/macos-cua/service/install_service.py` builds that packaged source into the signed nested `voice-cua.app`; it does not search for a sibling repository or fall back to system Python.

## Dynamic Island (island-post)

CUAService (the Agent Computer Use menu bar app) hosts the island from launch, Samantha or not. Menu: **Show Dynamic Island** (default on), **ACU Settings…** (⌘,: the one ACU Settings window — island, Samantha voice, AI agents, Computer Use permissions; also `macos-cua settings [pane]`), Samantha on/off.

**Transports.**
- **Local socket (default for `island-post` and Samantha's helper):** `~/Library/Application Support/CUAService/island.sock`, directory 0700, socket 0600. CUAService checks each peer's uid with `getpeereid()`. The client reads nothing from the Keychain, so macOS never shows a Keychain dialog. CUAService accepts a source once it is set up (ACU Settings → AI Agents → source → **Rotate**; Samantha's is created when you turn her on), and the source's toggle and attention cap still apply.
- **HTTP `127.0.0.1:8777` with `Authorization: Bearer <token>`:** for other harnesses (`island-post --http` too). The token sits in the Keychain (service `samantha.island`, account `samantha`/`grok`/`cursor`/`codex`). The item trusts only CUAService, so reading it with `/usr/bin/security` prompts. Use the socket where you can. Loopback only: `Origin` headers and non-loopback `Host` are rejected. Unsigned requests get 401 unless the local-dev flag `island.hub.allowUnsignedLocalDev` is on (off by default).

A source switched off in AI Agents gets 403. Approval and choice cards also need "Can raise approval cards" and an attention cap of Attention or higher.

```sh
cp bin/island-post ~/.local/bin/   # once
island-post --source grok-bot --kind running --title "Booking table" --app Comet
island-post --source cursor --kind approval --id deploy-1 --title "Deploy to staging?" --detail "Targets: api, worker, scheduler" --wait 120
island-post --source grok-bot --kind choice --id q1 --prompt "Which do you prefer?" \
    --option "Looks great=great" --option "Needs work=needs_work" --allow-text --wait 300
island-post --source codex --kind clear --id deploy-1
```

**Choice cards** (`--kind choice`, implied by `--prompt`/`--option`):
- Options: up to 6 `--option label=value`, plus `--multi`, `--allow-text`, `--primary <value>` and `--danger <value>`.
- Compact island: the source icon and one amber mark (a count badge instead when 2+ cards wait). Other sources get one circle each on the left (max 3 + "+N"). With music playing, a choice card shares the pill (Duo: artwork lead, source + mark trail); an approval takes the whole pill.
- Arrival: a soft Tink (ACU Settings → General → Mute agent prompt sound; silent when the source app is frontmost) and a 4 s **Quick Peek** of the card (never key). It re-peeks at +2 min and at T-60 s, max 3, and holds off while the owner is typing in the source chat app. Hovering a peek pins it.
- Opening: a click anywhere on the pill (including the strip above it) opens the card and makes it key: keys 1–6 pick options, Return sends, Shift-Return adds a line, Esc hands focus back. Approval cards still open on their own and jump the queue.
- Card layout: up to 4 option capsules, two per row (more than 4 shows 3 + "More in chat"); multi-select uses checkboxes and a "Submit N" button; the `--danger` option stays neutral until selected and then needs a confirm. The card is 176 or 220 pt and never scrolls.
- Text field (`--allow-text`): "Reply…", grows to 3 lines; an option and text are sent together.
- Answering: the card clears right away; if the owner was in the card, the next one slides in (two ghost layers show more are queued).
- Timeout: choice cards default to 15 min (`--ttl` to change). The last 60 s show an amber countdown ring; on expiry a 4 s "Timed out. Still open in chat" notice appears and the source circle keeps a hollow red ring until the owner opens the agent list or the agent sends `--kind clear`.
- Persistence: pending cards, timed-out notices and recent answers survive a CUAService restart (`island-pending.json`, 0600).
- Events: shown / hovered / clicked (x, y) / opened (via) / answered (via) / expired go to `~/.cache/macos-cua/island-events.jsonl` (ids and counts only — never titles, options or text).

**Agent roster:** the compact pill names the highest-attention agent; expanded rows name each child's parent. Attention-needed tasks sort before healthy work while child rows stay with their parent. Pending approval and choice rows open their details; the roster never answers them. Stop exposes its full target and a **Keep running** exit. Destructive approvals require exact target scope in --detail and a second confirmation. Keyboard navigation covers every actionable row and scrolls focus into view. When a running task stops updating past its TTL, its row reports the last-seen age, disables remote controls, and can be dismissed locally. A failed Stop or Retry remains marked **Not sent · Try again** until the agent publishes another update.

**`--wait N`** (up to 3600 s) long-polls the hub with no busy polling and reconnects across a CUAService restart. It prints one JSON line:

```json
{"ok": true, "id": "q1", "status": "answered", "values": ["great"], "text": "optional typed reply"}
```

Exit codes: 0 answered or approved, 1 denied or dismissed, 2 timeout or expired, 3 rejected or error. The CLI reports "Keychain access was refused or timed out" (HTTP mode) separately from "no token".

**Chat agents: mirror every inline prompt.** When you ask the owner something in chat (question with options, approval, free-text ask), also put it on the island so the owner can answer from any app:

```sh
island-post --source grok-bot --kind choice --id "$ASK_ID" --prompt "…" --option "Yes=yes" --option "No=no" \
    --allow-text --wait 3600 > answer.json &   # background; read answer.json when it exits
# …if the owner answers in chat first:
island-post --source grok-bot --kind clear --id "$ASK_ID"   # removes the card; the waiter exits 1 (dismissed)
```

**HTTP API.** Requests go to `POST /api/island/publish` (Bearer over HTTP, or `{"method","path","query","body"}` JSON lines over the socket):

```json
{
  "source": "grok-bot | cursor | codex",
  "id": "stable id (≤ 80 chars, default \"default\"); re-post to update",
  "kind": "running | needs_you | done | error | approval | choice | clear",
  "title": "required except for clear (≤ 120); choice prompt fallback",
  "prompt": "choice: the question (≤ 160)",
  "options": [{"label": "Looks great", "value": "great"}],
  "multi": false, "allow_text": false, "primary": "", "danger": "",
  "detail": "optional (≤ 200); for destructive approvals, required exact target scope",
  "app": "optional icon app (default Comet / Cursor / Codex)",
  "destructive": false,
  "ttl_s": 600
}
```

- **Rows** expire after `ttl_s` (default 600 s; done/error 30 s). Cards default to 900 s (15 min).
- **Answers:** `GET /api/agent/approval?source=<s>&id=<id>[&wait=N]` (N ≤ 600 per request) returns `{"status": "pending | approved | denied | answered | dismissed | expired | unknown", "values": [...], "text": "..."}`.
- **Samantha:** `source=samantha` posts a full island snapshot (her voice helper does this over the socket).

## Benchmarks

Measured numbers live outside the package so a benchmark run never changes the shipped plugin: [`docs/benchmarks/agent-computer-use.md`](../../docs/benchmarks/agent-computer-use.md). Reproduce with `python3 skills/macos-cua/scripts/run_benchmarks.py --repeat 5 --rate` (warm discard first). Model: `skills/macos-cua/references/entry-contract.json` `rating_model`. Do not loosen floors to force a pass.

Five graded axes, each 0–10; overall is their unweighted mean. Accuracy and visibility are trust gates, not averaged in. Scores for each release are in the linked file.

### Reliability

Every repeat passes its asserted postcondition; the p95/p50 spread is penalised.

### Robustness

Plans recover from stale trees, focus changes and refused AX actions without false passes.

### Context efficiency

Compact AX snapshots and verified deltas stay within the row's token floor.

### Speed

Wall-clock p50 and p95 per row against the row's floor.

### Efficiency

Tool calls and fallbacks per verified action.
