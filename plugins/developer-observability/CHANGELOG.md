# Changelog

## 0.1.29

- Treat sources read in the current collect as collected, scope coverage by project, and group retired legacy snapshot gaps.
- Add run-indexed task lookup, bounded task facets, compact coverage, and snapshot-bound detail sidecars while retaining reads of old monolithic findings files.
- Join tool lifecycle events by call ID, include task outcomes and Ledger summaries, and rank recent failed tasks first.
- Link CI check logs to explicit reporter attempts and move superseded snapshot sidecars into retention staging.
- Match CI and JUnit fix guidance to the finding kind: failure, latency hotspot, or redundancy.
- Live measurements (Mac Mini, 2026-11-11, ~45.5k events, 62 findings, before = 0.1.28): `findings.json` 81.3 MB -> 0.42 MB (session/scope/task detail now in lazily read per-snapshot sidecars, 57 MB / 323 files on disk vs 81 MB before); collect 6.1 s / 295 MB RSS -> 5.5 s / 250 MB; `observability_issues` 22.4 KB -> 19.8 KB (Samantha) and 17.7 KB -> 15.1 KB (agent-plugins), coverage block ~2.5 KB -> one line under 100 B, read latency 483 ms -> 7 ms; one task view: Samantha 1.7 KB (the session view was 72 KB) and a CI run 3.8 KB, each naming the first failing step and its typed error.

## 0.1.28

- Reject malformed-stream totals for direct measurement and expose bounded failed-event usage observations.

## 0.1.27

- Expose individual Codex captures and record artifact-bound external checks with explicit evidence limits.

## 0.1.26

- Reject private summary fields and avoid reporting totals for ambiguous or failed turns.

## 0.1.25

- Report failed task-usage persistence as a capture failure.

## 0.1.24

- Add opt-in Codex task usage capture and a read-only usage view.

## 0.1.23

- Prioritize active diagnosis findings and explain when CI test failure output was not retained.

## 0.1.22

- Attribute events and timeline items to the installed build identity.
- Trace browser begin/act/end and Ledger select/save/supersede stages with opaque ids.
- Add task identity, run duration, first failed stage and evidence completeness to session summaries.

## 0.1.21

- Filter collected observations by project, observed version and attribution, keeping unknown versions separate.
- Add version-aware diagnosis and dashboard filtering; scoped views exclude legacy primary findings and do not verify deployments.

## 0.1.20

- Keep routine diagnosis guidance concise and load specialized guidance on demand.

## 0.1.18

- Show privacy-bounded Ledger scope categories in task timelines.

## 0.1.17

- Collapse explicitly identified mirrored startup events while retaining provenance.


## 0.1.16 (2026-11-06)

- Add task-linked summaries, Ledger diagnostics, microphone state, and island transition evidence.

## 0.1.15 (2026-11-06)

- Offer a compact session summary with event, failure, Ledger, outcome, and version counts.

## 0.1.14 (2026-11-06)

- Preserve native action events in session timelines and paginate collected session events.

## 0.1.13 (2026-11-06)

- Expose task outcomes in session timelines and add opt-in, bounded error and Context Ledger diagnostics.

## 0.1.12 (2026-11-06)

- Add bounded session stage timing and privacy-safe ledger metadata.

## 0.1.11 (2026-11-06)

- Preserve occurrence-level evidence, attribution and bounded layout diagnostics.

## 0.1.10 (2026-11-04)

- Island activity: every MCP tool call posts `call_started` / `call_finished` for `developer-observability` with the calling harness, so the ACU Dynamic Island (agent-computer-use 0.2.87) shows its trace-waterfall mark outside on the right while a call runs, and nothing inside the pill. Posting never fails a tool; set `DEVELOPER_OBSERVABILITY_ISLAND=0` to turn it off.

## 0.1.9 (2026-11-04)

- Reads Samantha's rotated logs. agent-computer-use 0.2.86 rotates `samantha-activity.jsonl` and `voice.log` at 50 MB and keeps 5 gzipped copies (`samantha-activity.1.jsonl.gz` … `.5`, `voice.1.log.gz` …). The built-in Samantha sources now include those copies, oldest first, and a plain `.N` a crash left before its gzip. They are read under the live file's source id, so coverage and issue IDs stay one source per log. `.gz` files are streamed with `gzip`.

## 0.1.8 (2026-11-04)

Automatic clearing and storage visibility (Guru 2026-11-04 11:39 IST):

- New `runtime/collector/retention.py` (stdlib only). It runs at the end of every `observability_collect`, and on demand through the new `observability_storage` tool (`dry_run`, default true) or `bin/developer-observability collect --storage [--prune]`.
- What it clears: the collector's own outputs keep the live `findings.json` and drop snapshots and scratch older than 7 days; plugin caches (`~/.cache/observability`) drop files older than 7 days, while backups and configs (`*.bak*`, `config*.json`, `*.toml`) are kept for 30 days; configured `retention.trace_paths` (none by default) keep 7 days.
- How it clears: expired files are moved to `~/.Trash/developer-observability-pruned/<stamp>/`, never `rm`'d. A run stops moving once that folder holds `max_trash_bytes` (2 GiB), and the result reports `capped`. Space comes back when the Trash is emptied.
- Hard exclusions, which are never moved or written: every `[protected].paths` entry in `~/.agents/machine.toml` (read on each run and skipped quietly when the file or list is missing), `~/.cache/macos-cua` and other ACU-owned dirs, Samantha's `samantha-activity*.jsonl` and `voice.log` (Samantha/ACU rotates them), the runtime venv and `~/.config/developer-observability`. Tests cover each exclusion.
- Storage report: disk use of collector outputs, the runtime venv, plugin caches, trace dirs, the pruned-trash folder, Samantha log sizes (stat only) and any Apple container VM named `*signoz*`. A path over `retention.warn_bytes` (1 GiB) becomes a `storage` finding (`observability_issues kind=storage`).
- Settings live in config `retention` (`enabled`, `days`, `backup_days`, `cache_dirs`, `trace_paths`, `trash_dir`, `max_trash_bytes`, `warn_bytes`), and each has a default.

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
