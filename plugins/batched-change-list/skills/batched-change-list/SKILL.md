---
name: batched-change-list
description: Use when collecting related product findings for a reviewable change list and coordinated fix cycles.
---
# Batched change list

> **Self-validate after edits.** Run `python3 -m unittest discover -s scripts -p 'test_*.py'` from this skill directory.

Use the existing findings JSON as the sole tracker; HTML is its rendered view. Preserve work, user priorities, authorizations, issue IDs, evidence, and verification history. Never infer acceptance from unlinked telemetry or source tests alone.

The page is the user's only view of progress. Adopt each in-repo tracker once with `bcl --data FINDINGS.json adopt`; this creates the authoritative copy under `~/.agents/state/batched-change-list` (or `$BCL_STATE_DIR`) and starts its viewer. Run `bcl --data FINDINGS.json <stage>` at each workflow step instead of calling tracker scripts by hand. Stages read and write the live copy, then snapshot and commit the JSON and HTML in the adopting home worktree with a `Change list:` commit. A second worktree must use the live path or an in-repo snapshot that still matches it. Use `start`, `plan --plan PLAN.json`, `fix`, `install`, `verify`, `introspect`, `report`, `status`, and `merge`; `merge` only checks snapshot freshness and never merges branches. The per-list viewer starts or restarts automatically. Install requires a recorded plan for the current cycle, and Verify requires Install to have been reached. Run `bcl install-hooks` to install repository pre-commit and pre-push freshness checks. Use [tracker operations](references/tracker-operations.md) for recovery and lower-level tracker maintenance.

Codex handoff: at the start of each stage change, before doing that stage's work, run `bcl --data FINDINGS.json <stage>` for `plan`, `fix`, `install`, `verify`, `introspect`, or `report`.

When adding a finding, write `summary`, `impact`, and `done_when` in plain words for a non-technical reader. Put commands, SHAs, counts, timestamps, and log quotes in `observed` or `details`; the manager warns when a summary looks like log text or is missing.

Each list uses Collect and Present once, then loops through Plan & fix, Install, Verify, and (after a full cycle only) Introspect. Collect stays open for new findings; pick them up on the next Plan & fix. The strip shows `Round tracking.cycle`; each tracker starts at round 1 and a rollover advances it. The gate passes at 15 `verified_fixed` findings by default, configurable with `tracking.rollover_threshold`, or when no open findings remain. If it fails, return to Plan & fix; if it passes, report and roll over to a new list at Collect. Keep group labels, issue IDs, status, duration/model and concise verification summaries in `tracking.plan`; full verification methods and raw times are collapsed under each compact plan row.

1. **Collect and present.** Identify product, build, scope and expected behavior; run `bcl start` to record Collect, then again to record Present. Record findings and evidence in the authoritative tracker; load [collection and reporting](references/collection-and-reporting.md) for evidence handling.
2. **Plan and fix.** Group findings by root cause, record their IDs, worktrees, dependencies, model/effort and verification method in `PLAN.json`; run `bcl plan --plan PLAN.json --ids ISSUE-01`, then `bcl fix`. Load [fix and verify](references/fix-and-verify.md) for acceptance requirements.
3. **Install and verify.** Run `bcl install` after the recorded plan; install the exact authorized PR head when required. Run `bcl verify` after Install and use [verification runs](references/verification-runs.md) for evidence and acceptance requirements.
4. **Introspect and report.** After a full cycle, run `bcl introspect --result "no issues"` or provide an issues file. `bcl report` is guarded by the rollover gate and full-cycle Introspect requirement. See [tracker operations](references/tracker-operations.md) for compaction and rollover details.
