# Changelog

## 0.1.8 (2026-11-04)

Automatic clearing and storage visibility (Guru 2026-11-04 11:39 IST):

- New `runtime/collector/retention.py` (stdlib only). It runs at the end of every `observability_collect`, and on demand through the new `observability_storage` tool (`dry_run`, default true) or `bin/developer-observability collect --storage [--prune]`.
- What it clears: the collector's own outputs keep the live `findings.json` and drop snapshots and scratch older than 7 days; plugin caches (`~/.cache/observability`) drop files older than 7 days, while backups and configs (`*.bak*`, `config*.json`, `*.toml`) are kept for 30 days; configured `retention.trace_paths` (none by default) keep 7 days.
- How it clears: expired files are moved to `~/.Trash/developer-observability-pruned/<stamp>/`, never `rm`'d. A run stops moving once that folder holds `max_trash_bytes` (2 GiB), and the result reports `capped`. Space comes back when the Trash is emptied.
- Hard exclusions, which are never moved or written: every `[protected].paths` entry in `~/.agents/machine.toml` (read on each run and skipped quietly when the file or list is missing), `~/.cache/macos-cua` and other ACU-owned dirs, Samantha's `samantha-activity*.jsonl` and `voice.log` (Samantha/ACU rotates them), the runtime venv and `~/.config/developer-observability`. Tests cover each exclusion.
- Storage report: disk use of collector outputs, the runtime venv, plugin caches, trace dirs, the pruned-trash folder, Samantha log sizes (stat only) and any Apple container VM named `*signoz*`. A path over `retention.warn_bytes` (1 GiB) becomes a `storage` finding (`observability_issues kind=storage`).
- Settings live in config `retention` (`enabled`, `days`, `backup_days`, `cache_dirs`, `trace_paths`, `trash_dir`, `max_trash_bytes`, `warn_bytes`), and each has a default.
- `backend_root` (the legacy kindle-project checkout) is not used at runtime. The plugin runs only on its vendored backend. Only the optional drift test reads the key, and it skips when the path is missing.

## 0.1.7 (2026-11-04)

Found by replaying Samantha session ca44bfcb (2026-11-03 23:44–23:47 IST):

- Root-cause rollup: errors of one subsystem (tool family such as `ledger_*`, or a `<family>_*error` kind) in the same session and turn within 30 s become one finding. The root is the internal error (for example `ledger_helper_error`), else the first error; the other errors are attached as `effects` (`count`, `incidents`, `calls`, `retries`, `kinds`) and the tool calls of that incident are not reported again as redundancy. One ledger save failure was 5 findings; it is now 1.
- Duplicate calls: a `tool_start` is a duplicate only when the tool and its normalized arguments match (arguments from the row or its `*_call` companion). A repeat with different or unknown arguments is a `retry`.
- Failure ownership: tool and app errors are `app_error`. `collector_failure` is only the collector's own parse or IO errors (unparsed rows, missing or unreadable sources), one finding per source.
- Stable ids: finding ids are hashes of project, kind and the normalized signature, never timestamps, rows or counts; ids match across runs (tested).
- New `turn_latency` finding: each session's slowest turns over 10 s (top 3, with the tools and error count of each turn).
- New `island_layout` finding from the `island_layout` activity event (schema 1): a visible overlay with `expected_inside_island` outside the island rectangle (2 pt tolerance), or any visible rectangle off its screen. One finding per overlay kind and violation.
- `observability_issues`: `limit` accepts 1–100; larger values clamp to 100 with `limit_note`. Error hints name the real fix (`invalid_limit` no longer suggests checking the backend or Decider).

## 0.1.6 (2026-11-03)

- `observability_issues`: `active`, `by_status` and `by_source` are computed over the filtered rows (status and kind), so `total == sum(by_status) == sum(by_source)` for every query. `by_kind` stays the breakdown before the kind filter.
- Text-log prose that names a phase in words (`mic preflight failed: …`, `voice-cua stack: mic preflight failed`) joins the structured failure of that phase in the same session instead of forming its own findings; the cluster keeps the counted source's name and message.

## 0.1.5 (2026-11-03)

Collector accuracy, so a finding shows whether a fix worked:

- One root-cause cluster per cause: the same failure logged as a startup step, a `realtime_error` and two voice.log lines counts once per session (largest source wins). Payload dumps after the reason (headers, dicts) no longer split clusters; status codes still do (403 ≠ 429).
- `island_error_raw` is a mirror, not a failure (counted as `mirrors` in collector stats); `tool_error` with `needs_confirmation` is not an error.
- Latency only from real durations (`speech_to_playback_ms`, `latency_ms`, `duration_ms`, ok `step_ms` of non-terminal phases). Close/exit/done/error/idle spans are excluded. Hotspots report p50/p95/n against `collector.latency_threshold_ms` (default 1 s; tests/CI use `collector.test_latency_threshold_ms`, default 60 s); no per-project cap.
- Status: `active` = seen in the last `collector.active_window_h` (default 24 h). When sessions carry a `version`, failures get `by_version` and `before_after`, and a failure with none on the latest version is `recovered`. Causes such as `no_input_device` become their own `environment_condition` finding.
- No silent cap: every finding is written, ranked (`rank`) and paginated. `collect` returns `total`/`active`/`by_status`; `observability_issues` adds `active`, `by_status` and `by_source` (collector/primary).
- Test sessions (`purpose=self_test`, canned RIGHT/WRONG replies with ≤ 2 ms responses) are excluded by default across sources; `--include-tests`, `include_tests` (MCP) or `collector.include_tests` include them.
- Duplicate-call redundancy uses `tool_start` only (companion `comet_call` rows doubled every call).

## 0.1.4 (2026-11-03)

- Portable collector bundled in `runtime/collector/` (stdlib only, OS-neutral): JSONL logs, text logs, JUnit XML, agent-plugins `check.py` CI logs, git state and `hygiene --json` reports.
- Finding kinds: `failure` clusters, `latency_hotspot` (p50/p95/max, n >= 5, p95 >= 1 s) and `redundancy` (bursts, retries, duplicate calls), each with `file:line` evidence pointers and a project tag (`owner:<project>`).
- Writes its own `~/.local/share/developer-observability/collected/findings.json` (0600, atomic); the read side merges it with an existing producer's file, so the Kindle/Mac collector keeps working unchanged.
- New MCP tool `observability_collect` and CLI `bin/developer-observability collect [--project-dir DIR] [--source TYPE:PATH:PROJECT] [--no-known]`; `observability_issues` gains `kind`, `by_kind` and `slowest`.
- Attribution fallback: worktree-style labels `<project>-<task>` resolve to the configured project.
- Privacy: transcript and audio fields are never read into findings; token-like strings, emails, bearer values and home paths are scrubbed.

## 0.1.3 (2026-11-03)

- Restored to the collection. Self-contained: the read-side backend (`observability_api`, `contract`, `finding_text`, `recommendations`) is vendored in `runtime/backend/` from the producer project (kindle-project on Cursor Origin, 5e9c459, same bytes as cf2b097); no external checkout is imported.
- One install step: `runtime/install.py` creates the pinned venv and writes a default config only when none exists.
- Generic config: home-relative data dir, optional Decider, project discovery from findings; `configure.py` takes `--runtime`, `--decider`, `--project`, `--no-discover`, `--print`.
- Empty state with a producer hint when no `findings.json` exists; `optional_services` reports producer and Decider presence.
- Manifests without `cwd`: sh resolver for spec clients and Cursor, plus Claude Code and Grok manifests.
- Tests: ported backend tests, manifest/resolver, install and drift checks.

## 0.1.2

- Unreleased branch build (MCP Apps dashboard, occurrence timeline, privacy hardening).
