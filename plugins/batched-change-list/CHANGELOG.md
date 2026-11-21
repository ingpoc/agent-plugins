## 0.1.57

- Skill release checks: before a release, include or defer in-flight lane fixes; after shipping, re-sort open failures so stale tool-fault blocks do not linger. A failed review or check is classified as a tool or code fault before it is counted. The old model-escalation ladder stays gone; quota-out and cut-off notes sit with the classify-before-count rule, and the review guide restores the quota-out fallback order (Codex, then free OpenCode, then ask the owner; BCL-86).
- `bcl release-check` lists in-flight lanes and refuses a clear ship while any remain. `bcl resort-failures` lists counted failures that current rules treat as tool faults, and `--apply --reason` uncounts them. Incomplete (cut-off) reviews are recorded as tool faults and are not counted; a reply whose first line is `VERDICT: CHANGES` but lacks a Must-fix section still counts as a code failure, so a malformed reviewer cannot loop a lane forever.

## 0.1.56

- Lane retry admission, plan-review receipts, and repeat-failure blocking now share deduplicated lane history; same-lane plan edits preserve it, and restore-main checks cover plan, group, and finding writes.

## 0.1.55

- A plan review's CHANGES keeps the lane's fix waiting for a passed plan review and is no longer counted toward the two-failure block (0.1.54 blocked lanes whose plan was only being refined).

## 0.1.54

- `bcl unblock` restores blocked lanes and lets the owner exclude tool-fault attempts from failure counts.

## 0.1.53

- Review replies with the Must-fix value on the heading line (`Must-fix: none`, `**Must-fix:**`) are read correctly; a section ends at the next heading or `Label:` line. 0.1.52 recorded such a PASS as incomplete.

## 0.1.52

- `bcl job` owns every lane step: `--phase plan_review|fix|review|presuite` runs the real command as its child, and the tracker refuses out-of-order steps (fix needs a plan; review needs a finished fix; presuite needs a passed or skipped review; a new fix voids earlier passes) (BCL-101, BCL-102).
- Strict review verdict: only a reply whose first line is `VERDICT: PASS` or `VERDICT: CHANGES` with a Must-fix section counts; cut-off or quoted verdicts are recorded as incomplete and fail (BCL-104).
- Changes requested sends the lane back to fixing instead of showing "Fix failed"; the page shows plan review and its model, review passed, and interrupted only for a dead job (BCL-102).
- Failure counter: each failed review or check is counted once per lane; the next fix needs a passed plan review, and a second failure blocks the lane with its reason. Replaces 0.1.51's `--failing-test`/`--evidence` retry flags (BCL-105).
- Every gate override (`--force` on batch set-stage, rollover, merged, machine load) needs a one-line `--reason` before anything is written (BCL-107).
- `bcl install` refuses a lane whose verify script is missing; verify commands share one validator and are refused before any reservation (BCL-96).
- `bcl cleanup --run-id ID --outcome ...` records teardown apart from verdicts; Introspect `fixed inline` items need a proof (BCL-97, BCL-106).
- `bcl archive-history` moves earlier cycles' run and Introspect history into the archive (BCL-108, partial: unstamped legacy rows stay).
- Docs drop rules the tool now enforces; review docs say version and changelog belong to integration (BCL-103).

## 0.1.51

- A finding marked as a regression against main must sit in a lane that restores main first (`restores_main`); a redesign is its own finding (BCL-95).
- After a failed Verify, `bcl job --phase fix` needs `--failing-test` (already written in the lane) and `--evidence` (an existing record) before a retry (BCL-98).
- Protected rounds: at most 4 open lanes per round; `bcl job` and `bcl verify` check the machine load (`BCL_MAX_LOAD`, `--force` overrides); `bcl stop-verify --reason` stops a run and keeps verified fixes counted (BCL-99).
- Rewriting a plan keeps a gated lane's identity, findings and retry history across rounds; legacy histories sort by recorded time and deduplicate attempts and review bindings. Two counted failures require `bcl unblock --reason`, which records covered attempt IDs so a later same-second failure stays blocked; ambiguous failures also need a fresh passed plan review (BCL-95, BCL-98, BCL-99).

## 0.1.50

- bcl merged --force --reason "why" records an already merged PR when the merge gate reads short (for example a stopped run zeroed it); the one-line reason is kept with the forced gate overrides (BCL-92).
- bcl report --force now reaches the gate and overrides an unmet report gate, recording the override (BCL-93).
- After a failed shared check, bcl verify --shared --reason "why" reruns it once per failure and keeps the failed attempt; a shared or lane verify command that is a missing or non-executable script is refused before anything is recorded, and bcl install refuses such a lane script (BCL-91).

## 0.1.49

- bcl correct-round --round N --reason "why" moves a wrong round counter forward and keeps the correction in the list history; no hand edits (BCL-88).
- bcl install and bcl verify take --model; while a lane installs or verifies, the model column shows that model, or No model (scripted check) (BCL-89).
- bcl verify --reason "why" reruns the check of a failed lane once per round. The earlier failure and the reason are kept, and a refused rerun spends nothing (BCL-90).

## 0.1.48

- The Stale badge follows the latest recorded activity and the earliest running job heartbeat, so a fresh update clears it and an expired heartbeat still shows it; a lane waiting for quota shows Waiting for quota until the reset time instead of Stale (BCL-83, BCL-86).
- A new round plan after Introspect advances the round once every lane is closed, including failed lanes, and bcl verify marks its lanes as Verifying while it runs (BCL-84).
- The skill adds a repeat-failure ladder and a quota-out fallback for fixes and reviews (BCL-85, BCL-86).
- bcl job --model records who runs a job; the lane model column shows the current job's model, then the reviewer while in review, then the fix model (BCL-87).

## 0.1.47

- Tests that failed at random on a busy machine no longer start real viewers they do not need, wait for the viewer to answer before checking it, and prove each rejection with a real answer (BCL-82).

## 0.1.46

- Restarting a lane inside a round, for example after an early Introspect, keeps the same round number and adds no round history; the round only moves on when new work is planned after a finished round (BCL-81).

## 0.1.45

- The page header shows only the worktree name; the full path appears under Details. Rollover stores the name but still registers the exact path it was given, so two worktrees with the same name stay apart (BCL-75).
- The gate step turns green only when enough fixes are verified. The report exception for a list with nothing open is now a separate note on the page and a separate field in bcl status; bcl report and the documented gate count keep that exception (BCL-76).
- The workflow bar marks the stage the active lanes are in (the earliest one any lane is still in), so a round whose lanes are all in review shows Review; bcl status reports the same stage (BCL-80).

## 0.1.44

- Verification completion checks only the findings the run started with; findings added later read Not checked this install and never reopen a finished run. Dead unfinished fix, review and presuite jobs read Interrupted, resume needed, on the page and in bcl status; plan updates keep the lane phase (BCL-67).
- A legacy blocked row with no named blocker reads Not checked; rows that name a blocker still read Blocked (BCL-66).
- Skill description shortened to the 200 character limit (BCL-73); the skill shape test finds the installed skill-refine tool, with an environment override (BCL-74).

## 0.1.43

- scripts/check.py names the failing unittest IDs and the last 20 output lines of a failed test file; timing-sensitive verifier, lock and viewer tests no longer race under parallel load (BCL-64).
- bcl verify --shared runs repo-wide checks once per verification run; lane verify is refused after a failed shared check and is unchanged when none is recorded (BCL-65).
- Verification reasons, titles, summaries, impact and done-when must be plain words; test names, flags, field names, paths and hashes go to details, which the page shows only under Details. Legacy raw text moves under Details, the footer path is hidden in Details, and rows recorded before a fix existed read Not checked (BCL-66).

## 0.1.42

- bcl job --phase review --result FILE records passed only for a fresh first VERDICT: PASS line; CHANGES, a missing, stale or unreadable verdict records failed (BCL-57).
- Viewer leak guard runs per test module, so a leaking file fails alone and under scripts/check.py (BCL-58).
- Rollover sets the new list home from --worktree and clears the old plan; bcl status restarts a viewer whose report path moved (BCL-59).
- Stale shows only for overdue active lanes, never while idle (BCL-60).
- The header summarizes archived and parked items in words and shows the viewer version from its record (BCL-61).
- Carried findings show Not checked on a new list; a blocked check must name its external blocker (BCL-62).
- bcl merged stops that list viewer by its verified recorded pid; bcl stop-viewer does the same for one list (BCL-63).

## 0.1.41

- Viewer-identity tests stop every viewer they start, and a suite-wide guard fails the run when any test leaves a viewer under its temp root (BCL-53, BCL-56).
- A failed lane Verify keeps its findings in progress with an inconclusive check, and a fix job can restart the failed lane after Introspect; the plan_fix check runs before the command starts (BCL-55).

## 0.1.40

- bcl job retries snapshot conflicts and keeps the child running when a heartbeat write fails; a passed fix shows Review, not Planned (BCL-50).
- The workflow strip keeps one row with the Review step and shows its Pre-push suite sublabel as separate text (BCL-51).
- test_bcl.py pins the temp home and no longer hardcodes process ids, so it passes under the full repo check (BCL-52).
- The viewer restarts when the installed scripts change even if the version is the same (BCL-53).
- bcl review starts with only the implementer session and a passed fix; install still requires the reviewer session or a one-file skip (BCL-54).

## 0.1.39

- Archive merged verified findings at rollover (and migrate an existing list) without changing the gate count; verification runs skip archived items.
- Record Introspect in introspect history instead of WF findings; migrate legacy WF Introspect records with their provenance.
- Park hardware-blocked findings in a parked file with what they need; parked items do not count and are not carried onto new lists.
- Hold rollover until the merge-previous-PR threshold is also met; report keeps its own gate; references name the gate fields instead of numbers.
- Let small independent docs or chart items share a round.
- Draw chart transitions as SVG paths with arrowheads and edge labels, verdict badges in both views, no markdown or overlapping labels.
- Add a Review stage (work-done review or one-file skip, then the pre-push suite) between Plan & fix and Install, read from the definition everywhere.
- Add bcl job: records a lane job, heartbeats while it runs and writes the lane result on exit, so the page is not Stale while work is live and Installing appears only after install starts.
- Stop test-owned viewers, resolve bin/bcl only inside the plugin, restart a viewer serving another worktree, and snapshot-commit only on stage change or rollover.

## 0.1.38

- Redesign the skill as an always-loaded rule set with a one-row-per-trigger reference router and step-local commands.
- Generate a real workflow statechart with Current/After views and Elon verdict badges.
- Add deterministic routing evals and read gate thresholds from workflow definition fields, including the separate merge-previous-PR threshold.

## 0.1.37

- Allow max Codex effort on current plan groups while keeping the about-4-issues size cap.
- Migrate a verified legacy viewer without instance identity onto a new port without shutdown; fail closed when a running viewer cannot be identified, and do not start a replacement.

## 0.1.36

- Windows-safe non-destructive viewer liveness probe in live_state, with Unix behaviour preserved.
- Named tests that Origin CI keeps running on draft pull requests and that plan-fix keeps the one-review-round policy.

## 0.1.35

- Draft Introspect sections from tracker-recorded work for review and editing before save; reject draft paths that alias the input or live tracker, including hard links and the in-repo snapshot, and refresh the skill statechart shape check to match the current skill text.

## 0.1.34

- Show one Verification-by-version row per version from the latest check with Checked, Expected, Observed, and Verdict because; reject empty reason parts, merge duplicate outcome records at write time, keep distinct checks distinct, and put earlier checks plus hashes, paths, timings, and the receipt under Details.

## 0.1.33

- Limit `bcl sync-main` stashing to leftover hook/check dirt, preserve unrelated work, and catch recreated hook dirt at previously stashed paths.
- Add `bcl install-local` for installing clean plugin copies into the `~/.agents`, Cursor, and Codex caches with old-copy cleanup and tree-hash checks; install only the skill subtree at the skill destination.

## 0.1.32

- Flag or split plan groups that are too large or set to Luna max; determine exemptions by status, not phase.
- Add `bcl sync-main` to merge main, resolve deleted skill references, re-render HTML, and stash hook/check dirt including changes created after the merge; refuse when `docs/triage/` is dirty.

## 0.1.31

- Use the lane outcome field for the visible issue status.
- Reload `render.py` when its contents change in a running viewer.

## 0.1.30

- Advance the plan round on `set-plan` after Introspect, even when the new plan still names the current round.
- Keep a finding's outcome separate from its lane plan state in the renderer.
- Require a tracked HTML report for every indexed triage tracker.
- Emit `data-definition-hash` on the generated skill statechart.

## 0.1.29

- Fix statechart initialization and assign each reference its specific load trigger.

## 0.1.28

- Keep the skill trigger map and workflow statechart current, and expose the numbered critical path and stage checks to skill-refine.

## 0.1.27

- Keep failed and blocked lanes from completing the Install workflow tick; report denied viewer PID probes during Plan without a traceback.
- Generate and shape-check a skill loading statechart with current and trigger-loaded views.

## 0.1.26

- Require every current group's stored verification command in Introspect coverage after Verify.

## 0.1.25

- Require a recorded passing review before install, except for one-file groups, and keep Install progress pending until lanes actually pass review and complete installation.
- Validate Introspect coverage against the current plan groups and reached stages; route stage checks through the workflow definition.

## 0.1.24

- Route each group's fix and review through the configured coding CLI; require implementer and reviewer session records before install, except review may be skipped for a marked one-file group.
- Require ordered, complete Introspect coverage of every performed step and expose tested session, coverage, and gate-count checks as scripts.
- Generate the skill loop and workflow strip from one workflow definition; show lane progress and merge state from tracker state.

## 0.1.23

- Require this round's Introspect, tagged with cycle and round, before returning to Plan & fix or entering Report & roll over after Install. An earlier round's record is not enough.
- Skill wording matches the page: run Introspect after every round's Verify. Drop the full-cycle definition and the skip for small follow-up cycles.
- After Install, leftover issue checks show as checks pending rather than an unqualified blocked lane. A completed check pass clears that blocker.
- Require Verify before recording Introspect, guard new-round `set-plan` returns while allowing current-round plan edits without rewinding the workflow stage, and preserve unrelated blocked lanes after checks pass.

## 0.1.22

- State the delivery, status and measurement outcomes used to judge the workflow.

## 0.1.21

- Reject concurrent verification starts for the same tracker while allowing verifier tracker writes.

## 0.1.20

- Reserve managed tracker writes through final verification prerequisite checks and command launch. Duplicate concurrent starts remain a tracked limitation.

## 0.1.19

- Reject stale verification updates, preserve failures and serialize cooperating tracker writes and snapshots. Writes and snapshots require `fcntl`; Windows support remains pending.
- Show four lane phases, distinguish issue and verification status, and explain uncertain verification dates and gate exclusions.
- Reject dependency cycles and attempts to start a lane before its prerequisites are verified. Verification must use the reviewed command recorded in the plan, and rejects common placeholder commands.
- Explain lane dots, avoid highlighting planned lanes as active, identify missing viewer version, and keep finding edits from refreshing the workflow activity clock.
- Status reads the live tracker without starting a viewer; concurrent browser connections no longer block the live report.
- Verification requires a known build and revision, and the timeline retains each recheck on the same build.
- The rollover count excludes introspection records; real page and process fixes still count. The redundant lane-status grid is removed.
- The skill handles feedback during every stage and links each step to its on-demand reference.
- Snapshot and viewer startup failures now report an error and preserve recovery information; the viewer is checked before success is reported.
- Viewer restarts verify the process, instance, and tracker identity before signalling; unverified legacy viewers require a safe manual stop.
- Recover a prior PR's committed page with `scripts/previous_report.py` for comparison.
- Issue pages show the finding's outcome separately from the last recorded plan state, so a parked failing issue still reads as failing.
- An issue's recorded build is visible alongside its verification history, with a clear message when no verification result is recorded.
- The duplicate lane-status list is collapsed, lane details include the saved verification command, and the narrow workflow no longer overlays its loop label on the steps.

## 0.1.18

- `bcl merged` records the merge commit and time, moves the workflow to Merged / Done, and retargets stacked open PRs to `main` when provider CLI access is available; otherwise it records and prints a TODO.
- `bcl rollover` writes a separate live list without changing the old list or viewer, resets carried `in_progress` findings to `untested`, and defers the new viewer until `bcl start` begins Collect.
- Viewer port selection reuses 53366 when free and unreserved (or already owned by the list); finding writes resolve captures from the report directory and can save status updates when a capture is missing.
- Plan groups are lanes (fix → review → install → verify): a group enters Install when its review passes, one review round per group with leftover nits logged as new issues, and groups that finish close together share a serialized build. Live verify uses the `verify.command` stored at Plan. The plan row shows each group's phase; the top timeline highlights the furthest phase any lane is in.
- An unmet Fixes gate no longer uses current-step emphasis; only the current stage (and a met gate) get that marker.
- A pending group's leftover verify state does not advance its plan row or the top timeline; the timeline only moves with a lane that has actually entered Install or Verify.
- A round runs only independent groups: dependents are marked deferred (not planned or failed) and moved to the next round, with a default cap of 3–4 parallel groups.

## 0.1.17

- Store adopted trackers outside git, snapshot stage changes only in the home worktree, and add a non-merging freshness guard.
- Start and restart a per-list live viewer from `bcl`; show viewer metadata in its live page footer.
- Replace the plan display with compact, responsive group rows.
- Count every `verified_fixed` finding toward the 15-fix gate, including WF- Introspect records and page-tool prefixes; only `tracking.rollover.excluded_ids` are omitted. A count at or above the threshold is always met.
- Put the linked PR, worktree, and installed agent-plugin chips in a compact top-right header block; hide non-collection plugins and drop the full-width tracking cards.
- Collapse the round plan (summary, group rows, Once-per-round tail) under the workflow timeline in a native `<details>` that stays closed by default and remembers open/closed in `localStorage` across live reloads.
- `bcl verify` and finding-status writes update each plan group's verify state to verified, failed, or partial with counts. Introspect closes the current plan.
- `bcl introspect --sections` requires four headings (delete, simplify, optimize, automate), always creates one `verified_fixed` WF- task, and links code follow-ups as separate non-WF issues.
- Compact `SKILL.md` to a per-step command table; workflow detail lives in per-step references plus `tracker.md` and `viewer.md`.

# Changelog: batched-change-list

## 0.1.16

- `bcl fix`, `install`, and `verify` update and render the stage before running an optional command; Codex handoffs now require the page update at the start of each stage.
- Added recorded execution plans with deterministic parallel group rows and current-cycle Install / Install-before-Verify guards.
- Added the `bcl` stage command and idempotent repository `bcl install-hooks` backstop that chains existing hooks and checks rendered triage freshness.

## 0.1.15

- Added a configurable 15-fix rollover gate and round counter to the workflow strip, with a return loop to Plan & fix; rollover advances the cycle and starts the next list at Collect.
- The manager blocks report rollover before the gate unless `--force` is supplied, and records forced overrides.

## 0.1.14

- The live viewer fingerprints tracker and inventory inputs instead of rendered timestamps, so HEAD polls stay stable while those inputs are unchanged. Reload only after the fingerprint changes and preserve the selected issue and view position.

## 0.1.13

- Installed plugin versions are discovered from package manifests at live render time; stored versions remain labeled `As recorded` only when discovery cannot find that plugin.
- Workflow timestamps render in IST with a current render-time `Now` value; `render.py` accepts deterministic `--now` and opt-in `--live` inputs, and the data-backed viewer refreshes both on every request. Finding writes advance `Last reported` without changing the workflow stage.
- `plan_fix`, `install`, and `verify` require a logged finding in progress; `set-activity` marks its listed findings in progress before checking the gate.

## 0.1.12

- Issue page: "Verification by version" always shows in the main card, below Done when and above the collapsed Evidence section, never inside Evidence.
- Each version is a clean timeline row: version chip, coloured status chip (Fixed / Failing / Blocked / Inconclusive), one-line summary (click to expand), and a muted line with the install time in IST and the build.
- Suggested fix moves into Evidence as "Suggested fix · where to look" (agent-facing); the main card keeps What's wrong, Why it matters, Done when and Verification by version. The issue article carries `data-finding-id` and `data-status` for agents.
- Done when no longer falls back to log-like verification text; that text stays in the version timeline and Evidence. "Issues found" uses the same section heading style; the install-check badge no longer stretches full width.
- Repo check (`scripts/check.py`): the current `docs/triage/pr-N.html` must be byte-identical to a fresh `render.py` output of its tracker, so pages cannot be hand-edited.

## 0.1.11

- `introspect` adds its `WF-` task to an open verification run as pending, like `add`, so rendering no longer rejects it mid-run.
- Version bump after merging with 0.1.10 (readable issue detail); no other behavior change.

## 0.1.10

- Add plain-language finding summaries, impact and completion checks; show issue details in a readable hierarchy with one collapsed Evidence section.
- Warn when summaries are missing or look like logs; preserve detailed observations and verification evidence.

## 0.1.9

- `add` refuses `WF-` ids: the only way to create an Introspect task is the single `introspect` call, so one Introspect always means one task.

## 0.1.8

- One task per Introspect: `introspect` takes `issues` `[{issue, evidence, outcome: fixed|next_cycle}]` and creates a single `WF-` task listing them all (fixed when every issue was fixed during Introspect). Evidence is required for each issue. The task page shows them as an "Issues found" bulleted timeline (Fixed in green, Next cycle in amber), each with its evidence.
- `rollover` closes any open verification run into history, so the next list never shows a stale "Checked 0 of N".

## 0.1.7

- Introspect gate: after a full cycle (one that reached `install`), `set-stage`, `set-activity` or `rollover` to `report_rollover` fails until an `introspect` record exists. New batch action `{"action":"introspect","result":"<one line>"}`; a clean run is `"no issues"`. Small cycles that never reached install are not gated. Rollover clears the record for the next cycle.
- Introspect guidance: no evidence, no issue; small safe fixes are done during Introspect and logged as fixed `WF-` findings with `details.fixed_in`; larger ones become `WF-` findings for the next cycle.

## 0.1.6

- Add the Introspect stage between Verify and Report and roll over: the agent reviews the finished cycle, deletes, simplifies, optimizes or automates each slow, redundant or silently failing step, and records each as a `WF-` finding on the same tracker. Stage id `introspect` is accepted by `set-stage` and `set-activity` and shown in the stage bar.

## 0.1.5

- Add the `rollover` batch operation: at merge time one call excludes every verified-fixed finding, lists the rest as carried over, points the page header at the new PR and worktree, and sets the stage (it replaces five manual edits that left the header stale).
- Document the merge sequence: mark the PR ready, wait for the required CI check on the head, then merge (Origin: `--auto --expect-head` removes the wait loop), rename the worktree and restart the viewer on the new path.

## 0.1.4

- Add the Fast cycle (triage once, parallel fixes, one install and one combined live check per build) to fix-and-verify.

## 0.1.1

- Store optional merge authorization in the product repository’s agent instructions, where it survives plugin refreshes.

## 0.1.0

- First portable package: the `batched-change-list` skill with four references, `manage_findings.py`, `render.py`, `serve.py`, the HTML template, an example tracker and 32 tests.
- Portability: script commands are skill-relative (no `~/.agents/skills/...` paths), the six external principle links are replaced by bundled `references/principles.md`, and the package has no dependency on other skills.
- Merge policy moved out of the skill: this package ships no standing merge authorization. An owner records one in the product repository’s agent instructions; otherwise the skill proposes the merge and asks.
