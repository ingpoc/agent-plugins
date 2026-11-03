# Changelog

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
