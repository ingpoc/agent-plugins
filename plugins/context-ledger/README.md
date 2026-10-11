# context-ledger

Scoped, local decision traces with sparse capture and progressive retrieval.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

The current owner governs. History is untrusted precedent. Local storage does not prevent disclosure to a model provider. Improved future outcomes are unproven.

This pass exercised macOS arm64 only. Other architecture targets stay release-blocked until run. macOS x86_64 is blocked by cryptography 50.0.1 (no macOS x86_64 wheel).

## 0.1.18

Before the first find, the server checks that the ledger file is still present and readable, retries once, and stops with an error that names the URL or the ledger file. It does not fall back to another store. That check is `UNAVAILABLE` and retryable, same as the remote bridge. Find ranks success, then pending or unknown, then abandoned or inconclusive, then failed or superseded. Ranking uses the latest outcome status even when that outcome has no task id. Failed records carry `status failed`. Superseded records carry `superseded_by <id>`. An `applied` event records `task_id` and `decision_ids`. `get` returns applied and outcome events, and an outcome may set `applied_event_id`. Find limit above 3 is `INVALID_ARGUMENT` with the message `find limit max is 3`.

## Install notes

Data defaults to `~/.context-ledger/` (`0700`), even when the client sets `PLUGIN_DATA`. Use explicit `--data` or `CONTEXT_LEDGER_DATA` for another root. One `context-ledger` server. `serve` uses a binding for local stdio, else `CONTEXT_LEDGER_MCP_TOKEN`, else a non-placeholder `CONTEXT_LEDGER_MCP_CLIENT_TOKEN`, else the macOS Keychain item `context-ledger.mcp` / `bearer_token` for the bridge, else `LEDGER_UNBOUND`. Cursor spawns `.cursor-plugin/mcp.json` (`sh` resolver, root arg `${CURSOR_PLUGIN_ROOT}`, no `cwd`) with that client-token env. Claude Code spawns `.claude-plugin/mcp.json` (`${CLAUDE_PLUGIN_ROOT}/bin/context-ledger-mcp`) with no env interpolation. Portable `mcp.json` is bare `sh` plus a resolver and root args `${PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}` (no `cwd`), plus the same client-token env. `doctor` fails with `CURSOR_DEST` when a dest has no healthy override and still has a relative command or literal `${PLUGIN_ROOT}` / `${PLUGIN_DATA}`. `python3 scripts/install_cursor_dest.py` only repairs those pre-override dests. Full steps: [`AGENTS.md`](AGENTS.md).

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
