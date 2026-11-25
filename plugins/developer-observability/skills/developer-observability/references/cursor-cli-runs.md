# Cursor CLI runs

Cursor CLI headless runs (`agent` / `cursor-agent`) are captured by default once this plugin is installed in Cursor. Nobody writes rows by hand.

## How capture works
- **Hooks (default, every Cursor session):** `hooks/cursor-hooks.json` runs `bin/developer-observability cursor-hook` on `sessionStart`, `stop` and `sessionEnd`. Each row keeps `conversation_id`, `model`, `model_id`, `model_params`, `cursor_version` (a dated version such as `2026.11.19-…` marks the CLI), status or reason, `duration_ms`, and a scrubbed error line with its kind. Prompts, transcripts, email and tool data are dropped. The hook always exits 0 and prints `{}`, so it can never block a session. Set `DEVELOPER_OBSERVABILITY_CURSOR_HOOK=0` to switch it off.
- **Tokens (Cursor's own result):** hooks carry no tokens. Cursor reports per-turn `usage` only in the terminal `result` of `-p --output-format json|stream-json`, so run headless jobs through the wrapper:

  ```
  bin/developer-observability cursor-capture --format json -- -p --model <model> "prompt"
  bin/developer-observability cursor-capture --format stream-json --quiet-events -- -p --model <model> "prompt"
  ```

  Everything after `--` goes to Cursor. Do not put `agent` there: the wrapper finds `agent` (or `cursor-agent`, or `CURSOR_AGENT_BIN`) itself, and drops a leading `agent` or `cursor-agent` word if one is given. Use `--model`; Cursor CLI 2026.11.19 rejects `-m`. The wrapper also runs `agent --version` once per run (5 s limit) and stores it as `cursor_version`; if that lookup fails the version stays null and the run is unaffected. It adds `-p --output-format …`, forwards Cursor's output, and keeps `usage`, `duration_ms`, `request_id`, the init event's model name and the exit code. The result text is dropped. `cursor-usage --ingest FILE --exit-code N [--stderr-file F]` records an output that was already saved.
- **Refusals:** a run that fails before a result envelope (`ActionRequiredError: …`, exit 1) gets `error_kind`: `usage_limit` (out of usage, spend limit), `plan_refusal` (named models unavailable on the plan), `model_blocked`, `unknown_model`, `action_required`, `auth` or `error`. Plain text is trusted as a CLI error only when no result envelope came first.
- Storage: `~/.local/share/developer-observability/task-usage.sqlite3`, tables `cursor_runs` and `cursor_hook_events` (mode 0600). Hook rows and a capture join on `session_id`.

## Read back
`bin/developer-observability cursor-usage [--run UUID | --session ID | --since ISO --until ISO] [--last N] [--client cli|ide]`, or the MCP tool `observability_cursor_runs(run_id, session_id, since, until, limit)`. Each run has:
`model` (hook `model_id`, else `--model`), `cursor_version` (hook, else the wrapper's `agent --version`), `model_display`, `requested_model`, `model_version: null` with `model_version_source: not_reported`, `tokens {input, output, total, uncached_input, cache_read, cache_write}`, `tokens_in`, `tokens_out`, `tokens_total`, `tokens_source: official|none`, `tokens_method: harness_usage`, `cost: null` with `cost_source: not_reported`, `wall_ms` with `wall_source` (`cli_result`, `hook`, `wrapper` or `hook_times`), `exit_code`, `status`, `error_kind`, `error_message`, `source` (`capture`, `ingest`, `hook`, joined with `+`).
`tokens.input` = `inputTokens + cacheReadTokens + cacheWriteTokens`, because Cursor's `inputTokens` counts only uncached input. Tokens are official only when Cursor's `usage` object is present, and they are never estimated. Cursor reports no cost, so cost is never filled. `totals` sums official tokens only.

## Tests
Run the plugin tests with `python3 -m unittest discover -s tests` (from `plugins/developer-observability`). Do not use pytest on the Mini: its global plugins (anchorpy and pytest_xprocess) conflict and pytest fails to load. Leave the global Python setup alone.
