# Developer observability

Project-scoped build and runtime findings, redacted evidence and investigation leads over a local `findings.json`, with an optional local Decider and an MCP Apps dashboard.

Portable [Agent Plugin](https://agent-plugins.org/specification). Agent install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

## Install

Needs Python 3.11+ (3.12 recommended). Works on macOS and Linux; no Node at runtime.

1. Add the collection marketplace in your client and install `developer-observability`:
   - Codex: `codex plugin marketplace add ingpoc/agent-plugins`, then `codex plugin add developer-observability@ingpoc-agent-plugins`.
   - Cursor: import `https://github.com/ingpoc/agent-plugins` as a team marketplace and install from **Customize**, or copy `plugins/developer-observability/` to `~/.cursor/plugins/local/developer-observability/`.
   - Claude Code: `claude plugin marketplace add ingpoc/agent-plugins`, then `claude plugin install developer-observability@ingpoc-agent-plugins`.
   - Grok Build: `grok plugin marketplace add ingpoc/agent-plugins`, then `grok plugin install developer-observability`.
2. Run the one install step from the installed plugin folder: `python3 runtime/install.py`. It creates a private venv at `~/.local/share/developer-observability/runtime`, installs the pinned `mcp==2.2.0`, and writes `~/.config/developer-observability/config.json` only if it does not exist yet (never overwritten).
3. Start a new agent session and call `observability_projects`.

The default config reads `~/.local/share/developer-observability/data/findings.json`, turns on project discovery from the findings themselves, and has no Decider. To bind other paths or name projects explicitly before the first install, run `python3 runtime/configure.py --runtime DIR [--decider PATH] [--project id,label=Name,labels=a|b,owners=owner:x] [--no-discover]` (`--print` previews without writing). An existing config is edited by hand.

## Where the data comes from

The plugin reads a prepared `findings.json`. Two producers can feed it, and the read side merges them:

- the bundled portable collector (`runtime/collector/`, stdlib only, any OS): `observability_collect` or `bin/developer-observability collect --project-dir <dir>` scans JSONL and text logs, JUnit XML, agent-plugins `check.py` logs, git state and `hygiene --json` reports, and writes failure, latency-hotspot (p50/p95) and redundancy findings with `file:line` evidence to `~/.local/share/developer-observability/collected/findings.json`. Extra sources go in config `collector.sources` (`[{"type": "jsonl", "path": "...", "project": "..."}]`); `collector.known_sources: false` skips the built-in Samantha and agent-plugins CI paths. Each cause is one finding however many logs repeat it; island HUD mirrors and test sessions (`--include-tests` to keep them) never count as failures; latency comes only from real step durations; `active` means seen within `collector.active_window_h` (24 h), and sessions with a `version` get before/after counts. Nothing is capped: findings are ranked and paginated. Transcript and audio fields are never stored; tokens, emails and home paths are scrubbed.
- an external producer such as the Kindle telemetry snapshot service (its source adapters, SigNoz and launchd jobs live in that project). When `~/Library/Application Support/KindleSnapshot/runtime/findings.json` exists, the default config points at it.

Without either, `observability_projects` returns an empty state with a hint instead of an error.

Optional, detected when present, never required:

| Service | Used for | When absent |
| --- | --- | --- |
| findings producer (`findings.json`) | data from that producer | the bundled collector's findings only, else an empty state with a hint |
| local Decider script | `observability_decide`, recommendation `propose` | `decider_unavailable` error, no fallback answer |
| SigNoz | nothing in this plugin (producer side only) | no effect |

## Benchmarks

Scale 0–10 unless noted. Context efficiency maps to the rating key `token_efficiency`. Source: unmeasured.

| Axis | Score |
| --- | --- |
| Reliability | unmeasured |
| Robustness | unmeasured |
| Context efficiency | unmeasured |
| Speed | unmeasured |
| Efficiency | unmeasured |

### Reliability

Repeatable pass rate. Spread penalty when p95/p50 duration > 1.5.

### Robustness

Fraction of repeats that stay on the owner path (`measured.robust`).

### Context efficiency

Proof bytes / driver RPCs vs floor (`token_efficiency`). Compact query/diff, no chat dumps.

### Speed

Wall time and per-step latency vs floors.

### Efficiency

Round-trips / AX snapshots vs floor. Prefer one asserted run over N granular calls.

Replace `unmeasured` after a real suite. Do not invent scores.

## Interface

Six tools: projects, scoped issues, issue/evidence detail, existing Ledger
recommendation, typed Decider decision, and MCP Apps dashboard. The read-side
backend is vendored in `runtime/backend/` (see `VENDORED.json`). Tool names carry
the `observability_` prefix. Prepared data is reused without a source rescan.

The dashboard is bundled with the official `@modelcontextprotocol/ext-apps`
2.0.3 SDK. The server uses MCP Python SDK 2.2.0 with modern 2026-07-28 discovery
and legacy initialize compatibility. UI resources use the negotiated MCP Apps
extension, `ui://` metadata and `text/html;profile=mcp-app`.

Setup and proof boundaries: [AGENTS.md](AGENTS.md). Architecture and usage:
[skill](skills/developer-observability/SKILL.md). No independently measured
latency/token improvement or general cross-project recommendation coverage is
claimed. Private bindings and telemetry are never part of this public package.

Design: [six-tool plan](docs/PLAN.md). Proof contract and bounded retrieval
measurements: [verification](docs/VERIFICATION.md). Installed/runtime observations
and screenshots remain private, not in this public repository.
