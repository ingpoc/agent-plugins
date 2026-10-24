# Developer observability

Project-scoped observability, evidence-backed recommendations and local typed decisions with an MCP Apps dashboard.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

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
recommendation, typed Decider decision, and MCP Apps dashboard. Tool names carry
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
