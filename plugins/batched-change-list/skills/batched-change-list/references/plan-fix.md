# Plan and fix

Group related findings by root cause. Record the work groups and dependency edges in `tracking.plan` with `bcl plan --plan PLAN.json` before Review, then run each fix with `bcl job --ids ID --phase fix -- COMMAND`. Groups accept `id`, `name` or `label`, `finding_ids`, `depends_on`, `reason`, `worktree`, Codex job/model/effort/duration fields, optional `phase` (`pending`, `fix`, `review`, `install`, `verify`, `done`, `blocked`, `deferred`), and `verify` (`command`, `method`, `summary`, `failure`, `status`). Store `verify.command` on each group at Plan so Verify can run it. Each group is a lane (fix → review → install → verify) and can be in a different phase from the others; the page shows the recorded active phases. Open Details for full methods, grouping reasons, file notes and IST time ranges. Never show raw ISO timestamps in the user view.

After a counted code failure, the next fix is refused until `bcl job --ids ID --phase plan_review --result FILE -- COMMAND` passes (a plan review's CHANGES keeps the fix waiting and is not counted). A lane gets one plan review: after it, a review CHANGES reply with ROOT CAUSE and FAILING TEST sections opens the next fix instead, so post-fix review briefs ask for both. Only a post-fix review's CHANGES counts; a failed fix job is recorded uncounted and the lane stays in fix, and three failed jobs in a row block it. A second counted failure blocks the lane. `bcl unblock --ids ID --reason "why"` returns it; `--tool-fault ATTEMPT` uncounts a tool fault that was already recorded. Classify the fault before it is counted; see [Review](review.md).

When the scope is agreed or already authorized, group related fixes into one coherent plan with affected files, acceptance checks, and dependencies. Use existing project gates and the smallest number of build/install cycles consistent with safe verification. Do not assume every issue will be fixed in one cycle.

When debugging, reproduce, trace the first wrong decision or mishandled legitimate state, fix its owner, and check for the same fault elsewhere. Tests assert behaviour that fails without the fix, not private helpers or call order. Order a batch so each step is checked before the next depends on it.

Before building, make a compact verification matrix for each issue: ID, current status, owner, acceptance check, installed build, prerequisites, and whether the agent or user can perform it. Check shared prerequisites once (including input devices for voice), group checks by installed session, and perform all accessible checks before requesting user assistance.

Fix briefs call `bcl report-step -- failed_first "TEST fails: ..."` before changing code and `bcl report-step -- root_cause "..."` after tracing; Verify records `fix_verified` itself, and the lane Details show the trail.

Before fixing a group, `bcl fix` (or `set-activity`) marks the affected findings `in_progress` and displays one timestamped `Now` line below the stage bar. If an affected finding is closed, include `"reopen":true` to reopen it explicitly. Move an item off `in_progress` only to `verified_fixed` with evidence, or back to `failing` or `blocked` with the reason.

Regression against main: a finding whose details say `regression_against_main: yes` goes in a lane with `restores_main: true` that puts main's behaviour back first; file any redesign as its own finding (`bcl plan` refuses otherwise).

## Fast cycle: batch, parallelize, test once

Do not fix and test one issue at a time.

1. **Triage once.** Label every open issue `test-now` (the installed build can already show pass or fail), `code-fix`, `needs-user`, or `blocked`. Run all `test-now` checks in one live run before writing any code.
2. **Fix in parallel.** Run each independent group through the coding CLI set by `codex-cli`; put coupled issues in one job. The brief names files, behavior, focused checks, and a report under 12 lines. `bcl job`/`bcl verify` refuse while the machine's load is above `BCL_MAX_LOAD` (default: CPU count).
3. **Serialize builds and live verify.** Installs and builds share one queue; groups that finish close together share one build. Live verify is serialized too (one Mini, island, and Comet) and runs each group's stored `verify.command`. Mark `verified_fixed` as evidence lands. The gate counts those fixes immediately. The round ends when all lanes finish or block.
4. **Automate last.** Script a repeated step only when automation removes or replaces at least as much work; log the removed step.
5. **Tell the owner of a product tool when its output is wrong.** Send the exact run id, field and line; reinstall and re-run that same run to confirm.

## Plan check

A round runs only independent groups. Small docs-only or chart-only items with no code dependency and no shared file join the nearest group with a named test and verify. Move a blocked lane only to parked with a reason; it stays blocked next round until `bcl unblock`. Run `bcl refresh-worktrees --repo LIST_WORKTREE` to replay lane commits at list head (`--dry-run` previews; `--no-record` lets the next job record its cwd). A round runs at most 4 open lanes; avoid overlapping files and record fix order with `bcl fix-order`. Rollover carries gated lanes under the same id. Hard groups may use max Codex effort; cap each fix group at about 4 issues.

**Integration handoff:** The plugin version and changelog belong to integration, not to a lane-review must-fix.
