# Changelog

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
