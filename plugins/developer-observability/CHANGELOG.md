# Changelog

## 0.1.3 (2026-11-03)

- Restored to the collection. Self-contained: the read-side backend (`observability_api`, `contract`, `finding_text`, `recommendations`) is vendored in `runtime/backend/` from the producer project (kindle-project on Cursor Origin, 5e9c459, same bytes as cf2b097); no external checkout is imported.
- One install step: `runtime/install.py` creates the pinned venv and writes a default config only when none exists.
- Generic config: home-relative data dir, optional Decider, project discovery from findings; `configure.py` takes `--runtime`, `--decider`, `--project`, `--no-discover`, `--print`.
- Empty state with a producer hint when no `findings.json` exists; `optional_services` reports producer and Decider presence.
- Manifests without `cwd`: sh resolver for spec clients and Cursor, plus Claude Code and Grok manifests.
- Tests: ported backend tests, manifest/resolver, install and drift checks.

## 0.1.2

- Unreleased branch build (MCP Apps dashboard, occurrence timeline, privacy hardening).
