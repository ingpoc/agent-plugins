# Changelog: batched-change-list

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
