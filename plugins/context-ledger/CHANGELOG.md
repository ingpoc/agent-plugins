# Changelog

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
