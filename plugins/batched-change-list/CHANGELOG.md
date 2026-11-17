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
