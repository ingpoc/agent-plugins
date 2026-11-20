# Plan and fix

Group related findings by root cause. Record the work groups and dependency edges in `tracking.plan` with `bcl plan --plan PLAN.json` before Review, then run each fix with `bcl job --ids ID --phase fix -- COMMAND`. After a lane's failed review or check, its next fix is refused until `bcl job --ids ID --phase plan_review --result FILE -- COMMAND` passes; a second failure blocks the lane. Groups accept `id`, `name` or `label`, `finding_ids`, `depends_on`, `reason`, `worktree`, Codex job/model/effort/duration fields, optional `phase` (`pending`, `fix`, `review`, `install`, `verify`, `done`, `blocked`, `deferred`), and `verify` (`command`, `method`, `summary`, `failure`, `status`). Store `verify.command` on each group at Plan so Verify can run it. Each group is a lane (fix → review → install → verify) and can be in a different phase from the others; the page shows the recorded active phases. Open Details for full methods, grouping reasons, file notes and IST time ranges. Never show raw ISO timestamps in the user view.

When the scope is agreed or already authorized, group related fixes into one coherent plan with affected files, acceptance checks, and dependencies. Use existing project gates and the smallest number of build/install cycles consistent with safe verification. Do not assume every issue will be fixed in one cycle.

When debugging, follow [fix root causes](principles.md#fix-root-causes): reproduce, trace the first wrong decision or mishandled legitimate state, fix its owner, and check for the same fault elsewhere. When writing or changing tests, follow [test behavior, not implementation](principles.md#test-behavior-not-implementation). For ordering a multi-step batch, see [sequence verifiable units](principles.md#sequence-verifiable-units).

Before building, make a compact verification matrix for each issue: ID, current status, owner, acceptance check, installed build, prerequisites, and whether the agent or user can perform it. Check shared prerequisites once (including input devices for voice), group checks by installed session, and perform all accessible checks before requesting user assistance.

Before fixing a group, `bcl fix` (or `set-activity`) marks the affected findings `in_progress` and displays one timestamped `Now` line below the stage bar. If an affected finding is closed, include `"reopen":true` to reopen it explicitly. Move an item off `in_progress` only to `verified_fixed` with evidence, or back to `failing` or `blocked` with the reason.

Regression against main: a finding whose details say `regression_against_main: yes` goes in a lane with `restores_main: true` that puts main's behaviour back first; file any redesign as its own finding (`bcl plan` refuses otherwise).

## Fast cycle: batch, parallelize, test once

Do not fix and test one issue at a time.

1. **Triage once.** Label every open issue `test-now` (the installed build can already show pass or fail), `code-fix`, `needs-user`, or `blocked`. Run all `test-now` checks in one live run before writing any code.
2. **Fix in parallel.** Run each independent group through the coding CLI set by `codex-cli`; put coupled issues in one job. The brief names files, behavior, focused checks, and a report under 12 lines. `bcl job`/`bcl verify` refuse while the machine's load is above `BCL_MAX_LOAD` (default: CPU count).
3. **Serialize builds and live verify.** Installs and builds share one queue; groups that finish close together share one build. Live verify is serialized too (one Mini, island, and Comet) and runs each group's stored `verify.command`. Mark `verified_fixed` as evidence lands. The gate counts those fixes immediately. The round ends when all lanes finish or block.
4. **Automate last.** Script a repeated step only when automation removes or replaces at least as much work; log the removed step.
5. **Tell the owner of a tool when its output is wrong.** Send the exact run id, field and line; reinstall and re-run that same run to confirm.

## Plan check

A round runs only independent groups. At Plan, if a group would do better after another group's fix, move it to the next round and mark it `deferred` (not planned or failed). A small independent docs-only or chart-only item with no code dependency and no shared file with another group joins the round's nearest group instead of waiting for its own round, and still gets its own named test and verify. A round runs at most 4 open lanes (`bcl plan` refuses more). Hard groups may use max Codex effort; cap each parallel fix group at about 4 issues, including max-effort groups.
