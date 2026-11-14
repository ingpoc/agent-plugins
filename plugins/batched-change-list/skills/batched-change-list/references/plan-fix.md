# Plan and fix

Group related findings by root cause. Record the work groups and dependency edges in `tracking.plan` with `bcl plan --plan PLAN.json` before Install, then run `bcl fix`. Groups accept `id`, `name` or `label`, `finding_ids`, `depends_on`, `reason`, `worktree`, Codex job/model/effort/duration fields, optional `phase` (`pending`, `fix`, `review`, `install`, `verify`, `done`, `blocked`, `deferred`), and `verify` (`command`, `method`, `summary`, `failure`, `status`). Store `verify.command` on each group at Plan so Verify can run it. Always set `duration_s` when a job has finished. Each group is a lane (fix → review → install → verify) and can be in a different phase from the others; the page shows that phase on the plan row, and the top timeline highlights the furthest phase any lane is in. Open Details for full methods, grouping reasons, file notes and IST time ranges. Never show raw ISO timestamps in the user view.

When the scope is agreed or already authorized, group related fixes into one coherent plan with affected files, acceptance checks, and dependencies. Use existing project gates and the smallest number of build/install cycles consistent with safe verification. Do not assume every issue will be fixed in one cycle.

When debugging, follow [fix root causes](principles.md#fix-root-causes): reproduce, trace the first wrong decision or mishandled legitimate state, fix its owner, and check for the same fault elsewhere. When writing or changing tests, follow [test behavior, not implementation](principles.md#test-behavior-not-implementation). For ordering a multi-step batch, see [sequence verifiable units](principles.md#sequence-verifiable-units).

The main agent owns the issue list, batch plan, integration, report, and final evidence review. Route each task to the cheapest model and effort that can do it. Start a fresh coding session for each new job unless you are resuming the same unfinished job. Delegate independent, bounded investigations or fixes when the expected benefit exceeds coordination and review costs; give each worker issue IDs, owned files, acceptance criteria, and focused checks. Keep small or tightly coupled work inline. Use one operator at a time for each shared tracker, app, browser session, or device. Escalate uncertain root causes, conflicting evidence, or expanding scope to the main agent. Review worker results before updating issue status; source tests do not replace installed verification.

When concurrent workers could write the same state, see [separate before serializing shared state](principles.md#separate-before-serializing-shared-state). While waiting for user input, continue independent authorized work; see [never block on the human](principles.md#never-block-on-the-human).

Before building, make a compact verification matrix for each issue: ID, current status, owner, acceptance check, installed build, prerequisites, and whether the agent or user can perform it. Check shared prerequisites once (including input devices for voice), group checks by installed session, and perform all accessible checks before requesting user assistance.

Before fixing a group, `bcl fix` (or `set-activity`) marks the affected findings `in_progress` and displays one timestamped `Now` line below the stage bar. If an affected finding is closed, include `"reopen":true` to reopen it explicitly. Move an item off `in_progress` only to `verified_fixed` with evidence, or back to `failing` or `blocked` with the reason.

## Fast cycle: batch, parallelize, test once

Do not fix and test one issue at a time.

1. **Triage once.** Label every open issue `test-now` (the installed build can already show pass or fail), `code-fix`, `needs-user`, or `blocked`. Run all `test-now` checks in one live run before writing any code.
2. **Fix in parallel.** Give each independent `code-fix` group to its own coding job on disjoint files, with the cheapest model and effort that fits it. Put coupled issues in one job. The brief names the files, the exact strings or behavior wanted, the targeted test commands (never a known-hanging full suite) and a report under 12 lines. Run the jobs together. A round runs only independent groups; cap parallel groups at about 3–4.
3. **Review once, then install that lane.** Each group is its own lane: fix → review → install → verify. A group enters Install as soon as its review passes; do not hold a finished group until every group has finished Plan & fix. Use one review round per group; log leftover nits as new issues and do not start a second review.
4. **Serialize builds and live verify.** Installs and builds share one queue; groups that finish close together share one build. Live verify is serialized too (one Mini, island, and Comet) and runs each group's stored `verify.command`. Mark `verified_fixed` as evidence lands. The gate counts those fixes immediately. The round ends when all lanes finish or block.
5. **Automate what repeats.** Anything done twice becomes a script, and the removed or automated step is logged on the list.
6. **Tell the owner of a tool when its output is wrong.** Send the exact run id, field and line; reinstall and re-run that same run to confirm.

## Plan check

A round runs only independent groups. At Plan, if a group would do better after another group's fix, move it to the next round and mark it `deferred` (not planned or failed). Cap parallel groups in a round at 3–4 by default.

## Round rules

1. Process and workflow fixes must not outnumber product fixes in a round. Park new process issues for the next round unless they block the current one.
2. Default Codex fix jobs to Luna medium. Use max only for hard groups. Briefs name exact file paths.
3. Run tests and a diff check before Terra. Skip Terra for one-file fixes.
4. Job reports are 10 lines or fewer. Re-read only the tracker parts that changed.
