# Changelog

## 0.1.33

- Two shipped test files never ran. `tests/test_http_server.py` (8 tests) and `tests/test_server_logging.py` were absent from `PLUGIN_TESTS` in `scripts/check.py`, so `check.py` ran 8 of the 10 files it shipped. Both are wired in now.
- The shim that re-execs into the locked runtime when the ambient interpreter lacks `mcp.server.mcpserver` lived only in `test_http_server.py`. It is `tests/_locked_runtime.py` now, shared by both, so `test_server_logging.py` no longer fails under the `PYTHONNOUSERSITE=1` that `check.py` sets.
- `scripts/bump-version.sh` owns the ten pinned version sites and exits nonzero when one is missed or keeps the old value. A release had edited them by hand and dropped a site before.
- `scripts/bench.sh` runs the frozen retrieval eval against the private case file, so the BEFORE/AFTER gate for `find` and `select` is one command instead of five flags.
- `AGENTS.md` item 2 now lists `save`; the model-facing write tool was missing from the list that named `record`. The owner-CLI list adds `bind`.

## 0.1.32

- The global `**Ledger gate**` line is a trigger, not a copy of the skill. It restated the four questions inline — "answer the four questions yourself (decision or lesson; which task applies it; would it find it; already recorded)" — inside the file whose own rule is to keep triggers compressed and move the rest to a retrievable doc. The loaded skill is that doc. The line now defers: "answer the four questions in the skill yourself and decide alone which entries to add; do not ask or report". A test pins the deferral, so the questions cannot be re-inlined later.
- `~/.codex/AGENTS.md` moves from 595 to 584 words against the 500-word lint budget, and the line itself from 39 to 28. The file was already 539 words before 0.1.31, so it stays over budget; this release does not fix that, it stops this line from adding to it. `ensure-global-triggers` rewrote the existing marked line in place — the first production use of the replace path for this mark.

## 0.1.31

- The four capture questions are now the agent's own step at the moment it considers adding entries after a long workflow, and not something it asks the user or narrates. The capture section says to answer them alone, keep only what passes, and decide which entries to add without asking and without reporting the questions. The earlier text said only "answer these four before proposing anything", which left room to hand the choice back to the user — the shape the live ledger shows, where nothing is recorded without a round of questions first.
- `ensure-global-triggers` carries that duty into every session: a `**Ledger gate**` line is inserted into `## AFTER` directly above the `**Ledger save**` line, and updated in place on later runs. The command's JSON result now carries `gate`.
- The `**Ledger lookup**` line an agent reads in `~/.codex/AGENTS.md` said "every new task: invoke `context-ledger` through one retained helper". It named the plugin but not the moment, and never said to load the skill, so the protocol it depends on — a stable `task_id`, the situation, the needed decision, one retained helper, parent reads JSON only — could go unloaded. It now names both ends: load `context-ledger` before the first step of a new task, run one lookup through a retained helper, and do not pre-judge applicability or ask the user.

## 0.1.30

- The capture gate now names the questions an agent answers before proposing an entry. The skill was strict about an entry *after* it is written — the receipt, `helpful`, `effect` and `reason`, the reason length limit — and said nothing about whether the entry should exist, which is the shape the live ledger shows: 146 records, 94 outcomes, 2 applied links, 0 outcomes that qualify as a tier observation, 0 attestations. The capture section now asks four questions and forbids saving anything that fails one: decision or lesson; which future task will apply it; whether a `find` for what that task would type would return it; whether it is already recorded and carries its `scope`. The last two were previously scattered — the duplicate sweep in `references/lookup-subagent.md` and the scope requirement in the loop gate — and the first two were absent entirely.
- The criteria are pinned by a contract test, so they cannot be dropped by a later edit without that test failing.
- Docs and tests only. No producer, store, ranking, or ledger-format change, and the A/B trial is unaffected.

## 0.1.29

- `close <task> --applied <id>` (repeatable) records the applied link, one applied event per decision, and only then demands each id's outcome. `close` accepts that outcome only through its `applied_event_id`, because ranking counts an outcome solely by that link (`Store._tier`): a final, evidenced, `helpful` outcome without the link no longer closes the task. The FAIL names the applied event id the outcome has to cite. `--applied-none "<why>"` declares that nothing was applied; an omitted declaration is logged `unknown` and is never read as `none`, and `--audit` requires `--applied` or `--applied-none` once the lookup returned hits. One applied event per decision: an `applied` event naming several ids does not stand in for the per-id links ranking reads.
- The `trusted` tier now needs one observation per **distinct task**: the latest outcome for a task that names the applied event it judges, with a final status and usable evidence (the same bar closeout applies). Previously it counted the latest outcome per applied event, so one agent on one task writing three applied events and three helpful outcomes reached `trusted` with no independent check, and evidence-free or still-pending outcomes counted. `trusted` is labelled self-reported usefulness in the tier docs, the viewer, and SKILL.md: it is the applying agents' own record, not independent verification, and it is not attestation-gated. The viewer's tier filter, legend and KPI say so.
- A corrected outcome is no longer swallowed as a replay. The `(task_id, decision_id)` retry comparison covered only `status`, `note`, `evidence` and `applied_event_id`, so appending the same observation again with `helpful` flipped, `effect` set or a new `reason` was treated as an identical retry and dropped, losing the very corrections the tier and confidence are read from. Every persisted outcome field is now compared, so an identical retry still replays and a correction is a new event.
- A partial applied link can no longer be reported as a clean closeout. Each append commits on its own, so `close` keeps the links already written, names the id that failed, verifies the committed links and fails loud on a check over them. Remote discovery seeds from `--applied` too: an id that arrives only through the flag has no start row and was previously never read, so `close` missed its outcome and wrote a second applied event. A revision conflict while linking now rereads once and reuses the existing link instead of failing with a message about the caller's scope.
- The normal producer can satisfy the loop. `scripts/lookup.py closeout` and `feedback` wrote neither `helpful` nor `applied_event_id`, so the outcome they wrote failed the close `check` and counted toward nothing the tier reads. A closeout item already asserts `applied`, so the helper now cites that task's applied event in `applied_event_id`; with no receipt it writes nothing and returns `write: unavailable`, `need: applied_event_id`, and the fix (record the link with `close --applied`, then resend). An item states `helpful` outright; when it is absent the closeout derives it from the status it was given (a `success` helped, anything else did not), and `effect` (`not_helped`/`misled`) with `reason` is accepted with `helpful: false`. `feedback` writes `helpful: true` for a decision that helped, and `helpful: false` with `effect` and reason only when the item names the effect; `helped: false` without `effect` stays the inconclusive observation it always was, and then it cannot cite the receipt, because an outcome naming `applied_event_id` must also set `helpful`. A verdict without the receipt is refused instead of written unlinked: the item is skipped, and the reply is `write: unavailable`, `need: applied_event_id` and the fix, so feedback cannot leave an outcome the task's next `close` demands but cannot accept.
- The loop's and the helper's own plumbing reads are recorded under the actor `ledger-internal` instead of `unnamed`: the revision an append needs, the confidence re-read, and the record `check` reads back are not the agent's lookups, and the access report can now separate them. The helper's candidate sweep (`_visible`, which holds every eligible record rather than the answer) and the append revision read are `ledger-internal` too, and the CLI's `find`, `get` and the decision `select` returns carry the binding's actor, so a helper request no longer lands in `unnamed` at all. The reads and their count are unchanged except in one place: `select` now also bills the decision it returns to the caller, so a select on a find-on day writes exactly one more access row than before this change while the sweep it already wrote (one read per eligible record) is only relabelled. An A/B analysis that counts access rows per arm should treat the date this change reaches the running copy as a boundary for `select`; the rows written before it remain in the `unnamed` bucket.
- `close` refuses `--applied` together with `--applied-none` before either is written: both answer the same question, so accepting them logged an `applied_none` row while linking ids, leaving the task row at `applied_declared="none"` with `applied_linked=1`. Audit mode now accepts a declaration the ledger already records for the task, so re-verifying a closed task no longer exits 2 for a link it already holds. `closeout` rejects, rather than trims, a caller `reason` over 20 words or 160 characters, names `applied: true` as the need when an item denies it instead of returning a bare `write: unavailable`, and a `closeout` or `feedback` reply with `write: unavailable` exits 1, so a producer that wrote nothing is no longer indistinguishable from success by exit code.
- `select` returns `verdict` and `grounding` with the nearest record and its distance. `grounded` means the chosen record's own text covers enough of the query's IDF mass; `ungrounded` means the embedding ranked an unrelated record first and that record is not evidence. `select` takes an optional terse `query` for that reading and falls back to the situation and need when it is absent. The nearest record is still returned, so the response shape is unchanged apart from the two added fields.
- The helper runs `scripts/lookup.py find --query "<terse keywords>"` first. `find` is the grounded, outcome-ordered, A/B-measured path; `select` is the semantic expansion that reports whether its choice is grounded.
- `find` reads a record with no applicability scope as `fleet`. Ledgers written before scopes existed store no `body.scope`, and the scope filter had excluded every one of those rows, so `find` returned zero hits on a v3 ledger with no error. Absent-scope-is-fleet restores v3's measured win: fillers 17 → 0, relevant 11/21 → 13/21, fleet lessons 7/10 → 10/10, mean bytes 751.7 → 521.9.
- `migrate_scope.py` scopes a record to its project only when the text is about that project's own delivery artifacts: its PR, its findings, the version it ships, its builds, contracts, schema. The cue list gains `pr`, `finding[s]?`, `ship*`, `version[s]?`, `build*`, `bug[s]?`, `contract[s]?`, `migration[s]?`, `schema[s]?`, and deliberately not `ci`, `merge`, `review`, `release`, `fix`, or `gate`: those name practice that is still true in another project, so "CI must stay passing before merge" must still read `fleet`. Two records that named one allowlisted project yet scoped as `fleet` were reaching callers bound to other projects, and they are the whole B6 leak and both unbound project hits. On a scratch copy the wider cues take cross-project leaks 3 → 0, unbound project hits 2 → 0, fillers 0, relevant 13/21, fleet lessons 10/10, mean bytes 561.0 → 481.7, with scoped `find` unchanged at 7/8 top1 / MRR 0.875. Cost: one fleet-arm rank shift, `find_fleet` top1 4/8 → 3/8 with top3 unchanged.
- `isolation_eval.py` reports a `verdict` line and an `unscoped_records` count, so a nominal gate cannot be mistaken for a passing one and a run with no relevance cannot read as a leak fix.
- `recall_check.py` orders `find` candidates with the same relevance floor and IDF corpus the server uses, so which records pass the floor and the hit counts agree with production instead of measuring an unfiltered search. The order stays approximate (`Store._rank`, FTS-capped candidates); exact ranks come from the real `find` in `retrieval_eval.py`.
- `find`'s last tie-break is the decision's newest event, not its projection row. Two records written in the same millisecond tie on every key above it, and the order then came from a random uuid; a projection row is also re-inserted by maintenance and by another record's conflict or supersession, so its order is not decision order. `events.seq` is append order and survives a rebuild. Measured neutral: `retrieval_eval.py` output is byte-identical to the previous tie-break on the same db and fixture, and isolation is unchanged (25 hits, 0 leaks, relevant 13/21, fleet 10/10).
- `select` applies `find`'s eligibility: the caller's scope (fleet plus its project and tools) and expiry. It read candidates itself and returned a record scoped to another project that `find` hides.
- The A/B treatment is the agent's lookup, so `select` returns `{"arm": "off"}` on an even day in the window with `find`. It bypassed the arm and left the trial with no control arm.

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
