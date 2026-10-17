# Changelog

## 0.1.28

- A record is `project:<id>` only when `<id>` is on the allowlist: `agent-plugins`, `open-resource-network`, `aadhaarchain`, `brandgpt`, `hisab-pune`, plus ids named in `binding.json` (`project`, `projects`) or `data/project-registry.json`. A record is `tool:<id>` only for a known tool id. Every other entity, including a free-form topic, is `fleet`.
- When a record names both a project and a tool, the project wins only if the decision is about that project's own product or code. Otherwise the scope is the tool, or `fleet` when more than one tool is named. When unsure, the wider scope wins.
- Volatile tokens are version strings, commit SHAs, PR or Project ids, and dates. A model name or a status word is not volatile on its own. Migration strips volatile tokens into evidence and keeps the rule. It archives a row only when nothing durable remains. A pure status save is one whose main claim is a state.
- `find` with no `caller_scope` returns fleet records only. `ledger_loop.py find` and the helper `find` inject `caller_scope` from `binding.json`. `record` refuses a body with no scope.

## 0.1.27

- Every record has an applicability scope, `fleet`, `project:<id>`, or `tool:<id>`. `find` takes `caller_scope` and hard-filters to fleet plus that project and those tools, then ranks by text only. An entity or tag does not return a record with no text match.
- `save` requires a scope. The tool description asks whether the rule is still true in another project. Decision text is a lasting rule. Versions, SHAs, PR or Project ids, model names, and status words belong in evidence. A pure status save is refused and points at the tracker. A time-bound decision needs `expires_at` or `replace_when`. Expired decisions stay out of `find`.
- `find` returns at most 3 hits that pass the scope filter and the relevance floor. Zero hits is allowed.
- `scripts/migrate_scope.py` re-scopes records and rewrites or archives volatile ones. The default is a dry-run JSON report. Apply writes lifecycle `archived` into a dated archive and never deletes a row. It refuses `~/.context-ledger`.

## Unreleased: ledger viewer v1

- `browse` now draws `ui://context-ledger/ledger-v2.html`, the approved prototype on real data: decisions with why, search, activity in IST, outcomes, confidence, trust tier, and the A/B trial. `ui/browse.html` and its harness are removed.
- App-only read tools `ledger_query`, `ledger_activity`, `ledger_stats` (visibility `["app"]`, `readOnlyHint`). SQLite `mode=ro`, model-safe only, replies capped at 16 KB with integer cursors, no `access_log` rows. Tiers come from `Store._tier`; A/B maths from `ledger_stats.py` (`load`, `arm_stats`), loaded by path, not copied.
- `context_ledger.viewer_local`: the Mini localhost fallback (127.0.0.1, Host check, GET only).

## 0.1.26

- `Store.open` creates the v2 tables and backfills `links` and `fix_links` once. Later opens do not delete and rescan them. A projection write still updates links for that decision.
- `scripts/ledger_loop.py` has no default MCP URL. A `CONTEXT_LEDGER_MCP_TOKEN` with an empty `CONTEXT_LEDGER_MCP_URL` is not sent.
- `find` ranks a `user_decision` above an equal text match only when `approval.state` is `owner_attested`. A `self_reported` save does not get that boost.
- The one-copy find/get JSON shape lives in `tool_content`. `tests/test_v2.py` does not import the MCP server.
- Freeze (Guru 2026-10-17): `exception_for` is removed; `supersedes` is the only fix link. A not-helped applied decision with no fix makes `ledger_loop.py close` WARN with a count instead of failing.
- Each find card carries `tier`: `trusted` (3+ helped applied outcomes, failure (not_helped+misled)/applied 10% or less, and no fix supersedes it in this context; tune `TRUST_MIN_HELPED`/`TRUST_MAX_FAIL` in `ranking.py`), else `user_decision` for an owner-attested user_decision, else `hint`.
- A/B 2026-10-18 to 2026-10-31 IST: `find` over MCP returns `{"matches": [], "arm": "off"}` on even days; saves and outcomes stay on. `ledger_loop.py` applies the same rule and `close` appends one row per task to `ledger-loop/tasks.jsonl` (`--repeat-of`, `--guru-correction`, `--bot`). `LEDGER_AB_DATE` pins the day for dry runs.

## 0.1.25

- An applied outcome (one with `applied_event_id`) must set `helpful` true or false. `helpful: false` needs `effect` `not_helped` or `misled` and a one-line `reason` of 20 words or fewer. Otherwise the outcome is rejected and nothing is written.
- `save` takes `supersedes` or `exception_for` (the old decision id) plus `context` (10 words or fewer: where the old decision did not help). The link lives in a new additive `fix_links` table, rebuilt from traces.
- In that context, meaning the query covers half the context words or the fix itself matches, `find` halves the old decision's text score, ranks the fix above it, and flags the old card `not_helped here`. A fix card shows `supersedes`/`exception_for` and `context`. `get` lists `fixed_by` with `not_helped` and `confidence_here`.
- `ledger_loop.py close` fails when an applied outcome has no `helpful`, when `helpful: false` has no effect or reason, or when an applied decision did not help, the task succeeded, and no fix in this task links to it. `save` gains `--supersedes`, `--exception-for` and `--context`.

## 0.1.24

- `find` ranks by text match first: BM25 over decision (2.0), why (1.5) and the other body fields, with an IDF-weighted coverage floor (0.40, and at least 2 matched terms for queries of 3+ terms). A record below the floor is not returned. Confidence and outcome only break ties and never lift an off-topic record.
- Confidence counts an outcome only when it carries a real task id. A routine "Hourly ..." check-in without an applied event, and a placeholder task id, are not uses.
- `find` returns compact cards only: `decision_id`, `type`, `decision` (15 words), `why` (20 words), optional `crit`, `rule`, `exception`, `override_by`, `repeats`, up to 3 entities, `outcome`, `flag`, and up to 2 on-topic one-hop `related` precedents. A find reply stays within 1,200 bytes; related entries and then trailing hits are dropped first. MCP `find` and `get` send the JSON once (text only), not twice.
- `entities`, `constraints`, `task_id` and `actor` are optional in `find`.
- New `save` tool records a compact decision trace: `type` `user_decision` (Guru's accept or override; needs `why`; ranked above agent conclusions) or `agent_conclusion` (needs `why` and `criticality` 1-5), plus typed fields `rule`, `exception`, `override_by`, `approver`, `inputs`, `state`, `outcome`, `precedent_ids`, `entities`. `decision` over 15 words or `why` over 20 words, or either spanning lines, is rejected with a named error; nothing is trimmed.
- Additive schema: `links` (precedent edges, rebuilt from traces) and `access_log` (every MCP find and get: caller, task, query, result count, bytes, ids, ms; also one stderr JSON line). `repeats` counts traces with the same rule and exception (automation-candidate signal). An outcome may set `helpful`.
- Each caller may pass `actor`; provenance uses it instead of the shared binding actor.
- HTTP 401 names `AUTH_MISSING` or `AUTH_INVALID` and says it is an auth failure, not an empty result. The stdio bridge passes that code through.
- `scripts/ledger_loop.py` is gated and never blocks: `find` and `save` use a 2 s deadline (`LEDGER_TIMEOUT`) and fail open with one WARN line; `--audit` or `LEDGER_AUDIT=1` fails loud. `close --gated-out WORD` passes for a simple or one-shot task. Placeholder task ids are rejected.

## 0.1.23

- `browse` builds each card from the same record `get` would return. MCP omits a `local_only` replacement id and does not set `has_conflict` for a hidden conflict. Owner reads still see those links.
- The ledger view applies a list reply only for the latest filter request, and a detail reply only for the latest selection. An older error is dropped the same way.
- `browse` accepts `offset` from 0 through 20000. `limit` stays 1 through 24. A `has_more` page is the next offset. The view asks for that page.
- A failed browse clears the rows, the selection, and the open detail, and shows the failure. Retry sends the current filter again.
- A browse or get request that draws no reply within 8 seconds is removed from the pending set. Retry sends it again.

## 0.1.22

- Helper selection is `scripts/lookup.py select`. It compares the situation with applicability and the needed decision with the action using the on-device sentence embedding (cosine distance) and returns the nearest decision with that distance. The helper judges whether it applies. An unrelated situation still returns the nearest record and its distance. Success count does not choose the result. `find` outcome order is unchanged.
- `scripts/lookup.py feedback` records whether provided decisions helped. A precedent is similar only when the needed action is near the stored action as well as the situation. Ids marked `helped: false` in that feedback are left out of the similar check. A different action is recorded, and its rationale names the decisions that did not help. `helped: false` is inconclusive. An unknown id rejects the whole batch before any write and exits nonzero. `select` returns `outcome`, `flag`, and `approval`. Without the on-device embedding, both commands fail with `EMBEDDING_UNAVAILABLE`. macOS setup installs `pyobjc-framework-NaturalLanguage` for the helper interpreter. A skipped test off macOS is not proof of `select` or `feedback`.
- A Cursor cloud main agent hands lookup to one retained worker on the Mac and never calls the ledger itself. Codex cloud has no ledger. `select` and `feedback` stay on that worker.

## 0.1.21

- `browse` is the ledger view. It declares `ui://context-ledger/browse.html` (`text/html;profile=mcp-app`). The host draws that page. The page reads through `browse` and `get`. MCP still hides `local_only`.

## 0.1.20

- Each `find` probes the ledger again and retries once. A previous success is not reused. The remote probe accepts only a JSON-RPC result whose id is `health`; HTTP 200 with an error body is unreachable.
- `get` stays within 8192 bytes when applied and outcome events grow. It drops the oldest trace events and sets `trace_continued`. The current record remains.
- `applied` `decision_ids` must name decisions in the same scope. MCP append refuses a `local_only` or missing id. MCP `get` omits trace ids the caller cannot see. Purging a decision removes that id from other applied events. A default export never rewrites an event: it omits a `local_only` decision and every decision that references one, so event ids stay tied to the original JSON. Import accepts a missing id only when that decision's tombstone is in the same scope.

## 0.1.19

- `scripts/install-client.sh`, `scripts/refresh-client.sh`, and `scripts/check-ledger-access.sh` install or refresh Cursor, Codex, and Grok and check the one ledger. They do not create a data root. An older materialized copy is replaced in place. If that cannot be done, the script exits nonzero.
- The stdio resolver executes a plugin root or cache copy only when its `plugin.json` version is at least the pinned version. An older copy is not executed. Tag bootstrap failure is `PLUGIN_BOOTSTRAP_FAILED`.

## 0.1.18

- Before the first find, the MCP server checks that the ledger is reachable, retries once, and stops with an error that names the URL or the ledger file. It does not fall back to another store.
- Find ranks success, then pending or unknown, then abandoned or inconclusive, then failed or superseded. The rank uses that latest status even when the outcome has no `task_id`. Failed records carry `status failed`. Superseded records carry `superseded_by <id>`. Confidence still counts only outcome events that carry a `task_id`.
- The local probe stats the ledger file and queries a fresh connection. Removing the file after startup fails with `UNAVAILABLE`, retryable, and the path in the message. `CORRUPT_LEDGER` is not rewritten as unreachable.
- `append_event` accepts `applied` with `task_id` and `decision_ids`. An outcome may set `applied_event_id` to that event. `get` returns applied and outcome events so coverage can be read from the ledger.
- `find` with `limit` above 3 returns `INVALID_ARGUMENT` and the message `find limit max is 3`.
