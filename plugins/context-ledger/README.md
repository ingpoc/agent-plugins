# context-ledger

Scoped, local decision traces with sparse capture and progressive retrieval.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

The current owner governs. History is untrusted precedent. Local storage does not prevent disclosure to a model provider. Improved future outcomes are unproven.

This pass exercised macOS arm64 only. Other architecture targets stay release-blocked until run. macOS x86_64 is blocked by cryptography 50.0.1 (no macOS x86_64 wheel).

## 0.1.29

`select` returns `verdict` and `grounding` with the nearest decision and its distance. `grounded` means the chosen record's own text covers the query; `ungrounded` means the embedding ranked an unrelated record first and that record is not evidence. The helper runs `find` on terse keywords first, because `find` applies the relevance floor, ranks by outcome, and is the retrieval the A/B trial measures. `find` reads a record with no applicability scope as `fleet`, which restores hits on ledgers written before scopes existed.

## 0.1.28

`find` with no caller scope returns fleet only. `ledger_loop.py find` and the helper inject the caller's project and tools from `binding.json`. `record` requires a scope. A project scope is an allowlisted project id. A tool scope is a known tool id. Anything else is fleet. Version strings, commit SHAs, PR or Project ids, and dates belong in evidence. A model name or a status word does not make a save volatile. A pure status save, one whose main claim is a state, is refused.

## 0.1.27

`find` hard-filters by the caller's scope (fleet, the bound project, and the tools in play) and ranks by text only, at most 3 hits. `save` requires a scope and refuses a status update or a volatile pin in the decision text. `scripts/migrate_scope.py` prints a dry-run report and, when applied, archives volatile records.

## 0.1.26

Opening the ledger backfills precedent and fix links once. Later opens leave those tables alone. `ledger_loop.py` sends a bearer token only when `CONTEXT_LEDGER_MCP_URL` is set. A `user_decision` ranks above an equal match only after the owner attests it; a self-reported save does not.

## 0.1.25

An applied outcome must say `helpful`. A decision that did not help gets `effect` (`not_helped`/`misled`) and a short reason, and the fix found by debugging is saved with `supersedes` plus a short `context`. In that context the fix ranks above the old decision, whose confidence there drops. `ledger_loop.py close` enforces `helpful` and the effect/reason, and WARNs when a not-helped decision has no fix.

A/B (2026-10-18 to 2026-10-31 IST): `find` returns `{"matches": [], "arm": "off"}` on even IST days and works on odd days; saves and outcomes stay on. `ledger_loop.py close` appends one row per task to `ledger-loop/tasks.jsonl` (arm, gate, hits, helpful, misled, est. tokens, latency, fail-open, writes, `repeat_of`, `guru_correction`). `LEDGER_AB_DATE=YYYY-MM-DD` pins the day for dry runs.

## 0.1.24

`find` ranks by text match (BM25 plus a coverage floor) and returns nothing rather than an off-topic record; confidence only breaks ties. Replies are compact cards within 1,200 bytes, sent once. `save` records a compact trace: `user_decision` (Guru's override with its why, ranked first) or `agent_conclusion` (why plus criticality), with typed fields and precedent links; long text is rejected, never trimmed. Every find and get is logged per caller; the loop's and the helper's own plumbing reads are logged under `ledger-internal`, not as the agent's lookups. Auth failures are named errors. `ledger_loop.py` is gated, has a 2 s fail-open deadline, and supports `close --gated-out WORD`.

## 0.1.23

`browse` cards use the same visibility as `get`. A model-safe decision superseded by a `local_only` decision does not reveal that id, and a conflict that only names a hidden decision does not set `has_conflict`. The ledger view drops a list or detail reply that is older than the current filter or selection. `offset` reads a later page (0 through 20000) at the existing limit of 24. The view requests that next page when `has_more` is set. A failed browse clears the list, the selection, and the detail, and shows the failure with Retry. A browse or get with no reply within 8 seconds leaves the pending set and can be retried.

## 0.1.22

The helper selects the stored decision whose applicability fits the situation and whose action fits the needed decision, using the on-device Natural Language sentence embedding and cosine distance. `select` returns the nearest decision and its distance. The helper judges whether it applies. An unrelated situation still returns that nearest record and its distance. The result includes outcome, flag, and approval. A higher success count does not win that choice. `find` still ranks success ahead of a flagged failure. Feedback records whether a provided decision helped, and the closeout writes the applied link the loop demands: an applied outcome names the applied event it judges and sets `helpful`, and `helpful: false` needs `effect` (`not_helped`/`misled`) with a reason. `helped: false` without `effect` is inconclusive and that id is left out of the similar check. A missing decision is recorded when the needed action is not already stored, and its rationale names the decisions that did not help. On a host without that embedding, `select` and `feedback` fail with `EMBEDDING_UNAVAILABLE`. macOS setup installs `pyobjc-framework-NaturalLanguage` for the helper interpreter. A Cursor cloud main agent hands lookup to one retained worker on the Mac and never calls the ledger itself. Codex cloud has no ledger.

## 0.1.21

`browse` opens the ledger view. The host draws `ui://context-ledger/ledger-v2.html` (one self-contained file built from `ui-src/` by `python3 ui-src/build.py`). The view is read-only: decisions with their why, search (find's own ranking), the activity log (who looked up, applied or reported what, in IST), outcomes, confidence and trust tier, and the A/B trial (helpful-hit rate per 15 gated tasks, median/p90 overhead tokens, fail-open %, trusted-tier precision, arm per day). It reads through three app-only tools, `ledger_query`, `ledger_activity` and `ledger_stats` (`_meta.ui.visibility: ["app"]`); there is no write tool. They open SQLite `mode=ro`, show model-safe records only, and never write `access_log`. A/B numbers come from `ledger_stats.py` (agent-context-efficiency; path in `LEDGER_STATS_SCRIPT`) over the `ledger-loop/tasks.jsonl` logs (`LEDGER_VIEWER_TASK_LOGS`).

Mini fallback: `python -m context_ledger.viewer_local --plugin-data DIR --port 8790` serves the same page and the same three reads as GET JSON on `127.0.0.1` only (Host-checked, no CORS, GET only).

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
