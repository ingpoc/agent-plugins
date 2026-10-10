# context-ledger

Scoped, local decision traces with sparse capture and progressive retrieval.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

The current owner governs. History is untrusted precedent. Local storage does not prevent disclosure to a model provider. Improved future outcomes are unproven.

This pass exercised macOS arm64 only. Other architecture targets stay release-blocked until run. macOS x86_64 is blocked by cryptography 50.0.1 (no macOS x86_64 wheel).

## Install notes

Data defaults to `~/.context-ledger/` (`0700`), even when the client sets `PLUGIN_DATA`. Use explicit `--data` or `CONTEXT_LEDGER_DATA` for another root. One `context-ledger` server: a binding uses local stdio; no binding plus `CONTEXT_LEDGER_MCP_TOKEN` bridges to `serve-http`. The Cursor catalog entry spawns `${CURSOR_PLUGIN_ROOT}/bin/context-ledger-mcp`. Source `mcp.json` stays portable (`./bin/context-ledger-mcp`, `cwd` `./`). `doctor` accepts that `${CURSOR_PLUGIN_ROOT}` command with `cwd` `${CURSOR_PLUGIN_ROOT}` or `./`, and fails with `CURSOR_DEST` for a relative command or literal `${PLUGIN_ROOT}` / `${PLUGIN_DATA}`. `python3 scripts/install_cursor_dest.py` rewrites those dest files to the absolute launcher. Full steps: [`AGENTS.md`](AGENTS.md).

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
