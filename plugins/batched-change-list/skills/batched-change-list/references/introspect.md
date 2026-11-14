# Introspect

Run only after a **full cycle**: one that reached Install and live Verify. Skip it for small follow-up cycles (for example fixing findings that earlier Introspect work already logged); introspecting an introspect wastes the pass.

Keep it one cheap pass over what the run already recorded (failed commands, retries, reruns, stale or contradicting reports, checks that disagreed), not a re-read of the whole run.

`bcl introspect --sections SECTIONS.json` is required. The command refuses a missing, incomplete, or invalid `--sections` file. Do not pass an `issues` array or `next_cycle` notes on the Introspect call or on the WF- task.

## Four sections

`SECTIONS.json` must contain exactly these four keys: `delete`, `simplify`, `optimize`, and `automate`. Each section is either `{"none": "<reason>"}` or `{"items":[...]}` — never both, and never empty.

Each item has `item`, `evidence`, and `outcome`:

- `fixed inline` — the change was made during this Introspect pass (a few lines, no release, checkable at once).
- `issue` — anything needing code, a release, or a live test. Create that follow-up as a **separate non-WF issue** first, then link it with `issue_id`. The linked id must already exist and must not be a `WF-` id.

## One WF- task

The call always creates exactly one `WF-` task. The task is `verified_fixed` with `details.fixed_in` set to `Introspect`, and it stores the four sections on the task. Render them on the WF- task; do not list code follow-ups as `next_cycle` notes. The task's `issues` array stays empty. Linked issue ids may appear in `verification_refs`. A clean cycle (`none` in every section) still creates the WF- task and records result `Introspect: no issues`.

The call also closes the current round's plan (`tracking.plan.status = "closed"`) and records `tracking.introspect`. Then move to the [gate](gate.md). `report_rollover` remains blocked until this record exists after a full cycle.

## Rules

1. **No evidence, no issue.** Flag a step only when the run shows a concrete sign: a failure, a retry, a rerun, a silent failure (no error, wrong result shown as success, swallowed exception), or a stale or contradicting report.
2. **Silent failures are always addressed:** the owning script must exit non-zero with a one-line cause.
3. **Tell the owner of a tool when its output is wrong.** Send the exact run id, field and line; reinstall and re-run that same run to confirm.
