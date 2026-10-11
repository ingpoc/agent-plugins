# Changelog

## 0.1.18

- Before the first find, the MCP server checks that the ledger is reachable, retries once, and stops with an error that names the URL or the ledger file. It does not fall back to another store.
- Find ranks a latest outcome of success above pending, and ranks failed or superseded records below both. The rank uses that latest status even when the outcome has no `task_id`. Failed records carry `status failed`. Superseded records carry `superseded_by <id>`. Confidence still counts only outcome events that carry a `task_id`.
- `append_event` accepts `applied` with `task_id` and `decision_ids`. An outcome may set `applied_event_id` to that event. `get` returns applied and outcome events so coverage can be read from the ledger.
- `find` with `limit` above 3 returns `INVALID_ARGUMENT` and the message `find limit max is 3`.
