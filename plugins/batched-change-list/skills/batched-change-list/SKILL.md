---
name: batched-change-list
description: Use when collecting related product findings for a reviewable change list and coordinated fix cycles.
---
# Batched change list

> **Self-validate after edits.** Run `python3 -m unittest discover -s scripts -p 'test_*.py'` from this skill directory.

Adopt the in-repo tracker once with `bcl --data FINDINGS.json adopt`. The live file under `$BCL_STATE_DIR` (default `~/.agents/state/batched-change-list`) is authoritative; HTML is its rendered view. Run `bcl --data FINDINGS.json <stage>` at each step. Stages write the live copy, then snapshot and commit JSON and HTML in the home worktree with a `Change list:` commit. `bcl merge` only checks snapshot freshness and never merges branches. `bcl merged` records an already completed merge and marks the list Merged / Done. `bcl rollover --new-pr N --worktree NAME` writes a separate next-list file; that list's viewer starts only when `bcl start` begins its Collect stage. The per-list viewer starts or restarts automatically for other adopted-list commands. Recovery and lower-level commands: [tracker.md](references/tracker.md). Viewer details: [viewer.md](references/viewer.md). Run `bcl install-hooks` for repository freshness checks.

When adding a finding, write `summary`, `impact`, and `done_when` in plain words. Put commands, SHAs, counts, timestamps, and log quotes in `observed` or `details`.

Collect and Present run once; then loop Plan & fix → Install → Verify → (full cycle) Introspect → Gate. Each plan group is a lane: fix → review → install → verify. A group enters Install as soon as its review passes (one review round; leftover nits are logged as new issues, not a second review). Installs and builds are serialized through one queue; groups that finish close together share one build. Live verify is serialized too (one Mini, island, and Comet) and uses the verify command stored on the group at Plan. The gate counts verified fixes as they land. The round ends when all lanes finish or block. The page shows each group's current phase on its plan row; the top timeline highlights the furthest phase any lane is in. `tracking.round` starts at 1 and increments when the gate loops back; `tracking.cycle` advances only on PR rollover. The gate counts every `verified_fixed` finding on this list that is not in `tracking.rollover.excluded_ids`, including WF- and other page-tool prefixes. Default threshold is 15 (`tracking.rollover_threshold`).

| Step | `bcl` command | Load this file | Trigger |
| --- | --- | --- | --- |
| Collect | `bcl start` | [collect.md](references/collect.md) | New list or added findings |
| Present | `bcl start` | [present.md](references/present.md) | After Collect |
| Plan & fix | `bcl plan --plan PLAN.json` then `bcl fix` | [plan-fix.md](references/plan-fix.md) | After Present, or gate loop-back. Store each group's verify command at Plan. |
| Install | `bcl install` (optional `--ids`) | [install.md](references/install.md) | A group enters Install as soon as its review passes. Serialize builds; batch groups that finish close together. |
| Verify | `bcl verify` (optional `--ids`) | [verify.md](references/verify.md) | After that group's Install. Run the stored per-group verify command. |
| Introspect | `bcl introspect --sections SECTIONS.json` | [introspect.md](references/introspect.md) | After a full cycle only |
| Gate | (checked before `bcl report`) | [gate.md](references/gate.md) | After Introspect, or when checking rollover |
| Report & roll over | `bcl report`, then `bcl merged` after the PR merges | [report-rollover.md](references/report-rollover.md) | When the gate is met |

Shared references: [tracker.md](references/tracker.md), [viewer.md](references/viewer.md).
