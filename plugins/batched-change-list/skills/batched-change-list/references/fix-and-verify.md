# Fix and verification details

## Fix and verify

Record the work groups and dependency edges in `tracking.plan` with `bcl plan` before Install. The page shows a compact row per group with issue chips, status, duration/model and a short verification summary; open Details for full methods, grouping reasons, file notes and IST time ranges. The tracker rejects Install without a nonempty plan for the active cycle and rejects Verify until Install is recorded in that cycle. Run `bcl fix`, `bcl install`, and `bcl verify` to keep stage bookkeeping and the live timeline in sync.

When the scope is agreed or already authorized, group related fixes into one coherent plan with affected files, acceptance checks, and dependencies. Use existing project gates and the smallest number of build/install cycles consistent with safe verification. Do not assume every issue will be fixed in one cycle.

When debugging, follow [fix root causes](principles.md#fix-root-causes): reproduce, trace the first wrong decision or mishandled legitimate state, fix its owner, and check for the same fault elsewhere. When writing or changing tests, follow [test behavior, not implementation](principles.md#test-behavior-not-implementation). For ordering a multi-step batch, see [sequence verifiable units](principles.md#sequence-verifiable-units).

The main agent owns the issue list, batch plan, integration, report, and final evidence review. Route each task to the cheapest model and effort that can do it. Delegate independent, bounded investigations or fixes when the expected benefit exceeds coordination and review costs; give each worker issue IDs, owned files, acceptance criteria, and focused checks. Keep small or tightly coupled work inline. Use one operator at a time for each shared tracker, app, browser session, or device. Escalate uncertain root causes, conflicting evidence, or expanding scope to the main agent. Review worker results before updating issue status; source tests do not replace installed verification.

When concurrent workers could write the same state, see [separate before serializing shared state](principles.md#separate-before-serializing-shared-state). While waiting for user input, continue independent authorized work; see [never block on the human](principles.md#never-block-on-the-human).

Before building, make a compact verification matrix for each issue: ID, current status, owner, acceptance check, installed build, prerequisites, and whether the agent or user can perform it. Check shared prerequisites once (including input devices for voice), group checks by installed session, and perform all accessible checks before requesting user assistance. After installation, complete the issue-by-issue pass and add newly discovered issues before starting another fix cycle; stop early only for an urgent or blocking condition and retain the incomplete run. A missing prerequisite blocks only checks that depend on it. Run focused checks during edits, then the required full gate once for the frozen candidate; rerun failed stages after their cause is corrected.

Collect observability once before the relevant live batch and once afterward, using the same session and build identity where available. Treat older or unlinked telemetry as a lead. Record outcomes and evidence by issue in the existing JSON. Keep the user view concise and diagnostics in Agent details.

Use `Verified fixed` only after the acceptance criteria pass the relevant tests and validation, plus installed/live verification when the issue affects runtime. The agent performs available live checks and records evidence itself; request user testing only for a genuinely inaccessible or subjective check. If user confirmation is required, give exact actions and expected result, and retain `Awaiting user test` until confirmed. Link observability and independent readback when applicable; an absence of events, source tests, or a green aggregate snapshot alone does not prove a fix. If proof is missing or conflicts, use `Untested`, `Awaiting verification`, `Failing`, or `Blocked` as appropriate. Separate a failed release gate from confirmed product defects and retain failed runs and successful isolated retries.

Before declaring a fix complete, see [prove it works](principles.md#prove-it-works).

Before fixing a group, use `set-activity` in one `batch --render` with its stage, affected IDs, and a concise `now` description. It marks those findings `in_progress` and displays one timestamped `Now` line below the stage bar. If an affected finding is closed, include `"reopen":true` to reopen it explicitly. The navigation shows a working spinner on each `in_progress` item. The page is the user's only view of progress, so update it as each step starts. Move an item off `in_progress` only to `verified_fixed` with evidence, or back to `failing` or `blocked` with the reason.

Codex handoff: at the start of each stage change, before doing that stage's work, run `bcl --data FINDINGS.json <stage>` for `plan`, `fix`, `install`, `verify`, `introspect`, or `report`.

After each coherent test or verification batch, update the authoritative findings and regenerate the view; reconcile per-item status, navigation badges, counts, build/head and evidence together. Consolidate related report-only edits into one commit after the batch. Preserve unresolved findings for the next pass. An authorized PR is created or updated before installing when project policy requires an open PR head; install that exact head and verify its identity. Do not merge, publish, install, send messages, or run intrusive tests unless authorized by the user or applicable instructions.

Record issues found, fix/build/install/verification cycles actually run, and available wall time or token evidence. Report measured savings only with a comparable baseline and equal or better outcomes; otherwise describe batching as a potential saving, not a proven one.

## Fast cycle: batch, parallelize, test once

Proven on the PR #96 list (22 issues). Do not fix and test one issue at a time.

1. **Triage once.** List every open issue and label it: `test-now` (the installed build can already show pass or fail), `code-fix` (needs an edit), `needs-user` (voice, hardware, login) or `blocked`. Run all `test-now` checks in one live run before writing any code, so fixes target only what actually fails.
2. **Fix in parallel.** Give each independent `code-fix` group to its own coding job on disjoint files, with the cheapest model and effort that fits it. Put coupled issues in one job. The brief names the files, the exact strings or behavior wanted, the targeted test commands (never a known-hanging full suite) and a report under 12 lines. Run the jobs together and wait for all of them in one foreground loop, not one after another.
3. **One build per batch.** Review all job diffs together, run the focused tests once, bump the version once, commit, install once.
4. **One live check per build.** A single scripted run (start the product, drive typed turns, collect observability, take one screenshot) produces evidence for every issue in the batch. Map each observation to the issue IDs it proves; record it with `record-evidence` and set `verified_fixed` in one `batch --render`. An issue the run does not prove stays `in_progress`.
5. **Automate what repeats.** Anything done twice becomes a script (live check, job launcher, tracker batch builder), and the removed or automated step is logged on the list.
6. **Tell the owner of a tool when its output is wrong.** Send the exact run id, field and line; reinstall and re-run that same run to confirm.
