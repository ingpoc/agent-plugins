# context-ledger

Scoped, local decision traces with sparse capture and progressive retrieval.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

The current owner governs. History is untrusted precedent. Local storage does not prevent disclosure to a model provider. Improved future outcomes are unproven.

This pass exercised macOS arm64 only. Other architecture targets stay release-blocked until run. macOS x86_64 is blocked by cryptography 50.0.1 (no macOS x86_64 wheel).

## 0.1.24

`find` ranks by text match (BM25 plus a coverage floor) and returns nothing rather than an off-topic record; confidence only breaks ties. Replies are compact cards within 1,200 bytes, sent once. `save` records a compact trace: `user_decision` (Guru's override with its why, ranked first) or `agent_conclusion` (why plus criticality), with typed fields and precedent links; long text is rejected, never trimmed. Every find and get is logged per caller. Auth failures are named errors. `ledger_loop.py` is gated, has a 2 s fail-open deadline, and supports `close --gated-out WORD`.

## 0.1.23

`browse` cards use the same visibility as `get`. A model-safe decision superseded by a `local_only` decision does not reveal that id, and a conflict that only names a hidden decision does not set `has_conflict`. The ledger view drops a list or detail reply that is older than the current filter or selection. `offset` reads a later page (0 through 20000) at the existing limit of 24. The view requests that next page when `has_more` is set. A failed browse clears the list, the selection, and the detail, and shows the failure with Retry. A browse or get with no reply within 8 seconds leaves the pending set and can be retried.

## 0.1.22

The helper selects the stored decision whose applicability fits the situation and whose action fits the needed decision, using the on-device Natural Language sentence embedding and cosine distance. `select` returns the nearest decision and its distance. The helper judges whether it applies. An unrelated situation still returns that nearest record and its distance. The result includes outcome, flag, and approval. A higher success count does not win that choice. `find` still ranks success ahead of a flagged failure. Feedback records whether a provided decision helped. `helped: false` is inconclusive and that id is left out of the similar check. A missing decision is recorded when the needed action is not already stored, and its rationale names the decisions that did not help. On a host without that embedding, `select` and `feedback` fail with `EMBEDDING_UNAVAILABLE`. macOS setup installs `pyobjc-framework-NaturalLanguage` for the helper interpreter. A Cursor cloud main agent hands lookup to one retained worker on the Mac and never calls the ledger itself. Codex cloud has no ledger.

## 0.1.21

`browse` opens the ledger view. The host draws `ui://context-ledger/browse.html`. The list is the current model-safe decisions. A filter calls `browse` again. A row calls `get`.

## 0.1.20

Each `find` probes the ledger again, retries once, and does not reuse an earlier success. The remote probe counts only a JSON-RPC result whose id is `health`. `get` keeps the current record when the applied and outcome trace exceeds 8192 bytes, omits the oldest trace events, and sets `trace_continued`. An `applied` event's `decision_ids` must name decisions in scope. MCP reads omit ids the caller cannot see. Purging a decision removes that id from other applied events. A default export never rewrites an event: it omits a `local_only` decision and every decision that references one. Import accepts a missing id only with that decision's tombstone.

## 0.1.19

Install, refresh, and the one-ledger access check are `scripts/install-client.sh`, `scripts/refresh-client.sh`, and `scripts/check-ledger-access.sh`. A materialized plugin copy older than this package is replaced in place for Cursor, Codex, and Grok. The MCP resolver does not execute a root or cache copy older than the pinned version; if the pinned tag cannot be fetched, it exits `PLUGIN_BOOTSTRAP_FAILED`.

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
