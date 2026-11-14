---
name: batched-change-list
description: Use when collecting related product findings for a reviewable change list and coordinated fix cycles.
---
# Batched change list

> **Self-validate after edits.** Run `python3 -m unittest discover -s scripts -p 'test_*.py'` from this skill directory.

Adopt the in-repo tracker once with `bcl --data FINDINGS.json adopt`. The live file under `$BCL_STATE_DIR` (default `~/.agents/state/batched-change-list`) is authoritative; HTML is its rendered view. Run `bcl --data FINDINGS.json <stage>` at each step. Stages write the live copy, then snapshot and commit JSON and HTML in the home worktree with a `Change list:` commit. `bcl merge` only checks snapshot freshness and never merges branches. The per-list viewer starts or restarts automatically. Recovery and lower-level commands: [tracker.md](references/tracker.md). Viewer details: [viewer.md](references/viewer.md). Run `bcl install-hooks` for repository freshness checks.

When adding a finding, write `summary`, `impact`, and `done_when` in plain words. Put commands, SHAs, counts, timestamps, and log quotes in `observed` or `details`.

Collect and Present run once; then loop Plan & fix → Install → Verify → (full cycle) Introspect → Gate. `tracking.round` starts at 1 and increments when the gate loops back; `tracking.cycle` advances only on PR rollover. The gate counts every `verified_fixed` finding on this list that is not in `tracking.rollover.excluded_ids`, including WF- and other page-tool prefixes. Default threshold is 15 (`tracking.rollover_threshold`).

| Step | `bcl` command | Load this file | Trigger |
| --- | --- | --- | --- |
| Collect | `bcl start` | [collect.md](references/collect.md) | New list or added findings |
| Present | `bcl start` | [present.md](references/present.md) | After Collect |
| Plan & fix | `bcl plan --plan PLAN.json` then `bcl fix` | [plan-fix.md](references/plan-fix.md) | After Present, or gate loop-back |
| Install | `bcl install` | [install.md](references/install.md) | After a recorded plan |
| Verify | `bcl verify` | [verify.md](references/verify.md) | After Install |
| Introspect | `bcl introspect --sections SECTIONS.json` | [introspect.md](references/introspect.md) | After a full cycle only |
| Gate | (checked before `bcl report`) | [gate.md](references/gate.md) | After Introspect, or when checking rollover |
| Report & roll over | `bcl report` | [report-rollover.md](references/report-rollover.md) | When the gate is met |

Shared references: [tracker.md](references/tracker.md), [viewer.md](references/viewer.md).
