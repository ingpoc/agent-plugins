# Introspect

Run only after a **full cycle**: one that reached Install and live Verify. Skip it for small follow-up cycles (for example fixing an earlier Introspect's findings); introspecting an introspect wastes tokens. The tracker enforces this: after a cycle that reached `install`, moving to `report_rollover` (or `rollover`) fails until an `introspect` record exists.

Keep it one cheap pass over what the run already recorded (failed commands, retries, reruns, stale or contradicting reports, checks that disagreed), not a re-read of the whole run.

1. **No evidence, no issue.** Flag a step only when the run shows a concrete sign: a failure, a retry, a rerun, a silent failure (no error, wrong result shown as success, swallowed exception), or a step that turned out redundant. Never invent issues.
2. **Clean run:** one line and move on: `{"action":"introspect","result":"no issues"}`
3. **Small, safe fixes happen now** (a few lines, no release, checkable at once: a redundant step, a stale note, a wrong setting, a missed decision, leftovers).
4. **Anything needing code, a release or a live test** is left for the next cycle.
5. **Silent failures are always addressed**: the owning script must exit non-zero with a one-line cause.
6. **Record everything in one call; it creates exactly one `WF-` task** listing every issue (never one task per issue):
   `{"action":"introspect","issues":[{"issue":"...","evidence":"log/path or value","outcome":"fixed"},{"issue":"...","evidence":"...","outcome":"next_cycle"}]}`
   The task is `verified_fixed` when every issue was fixed in Introspect, otherwise `untested` with the next-cycle items as its recommendation. Then `set-activity` stage `report_rollover`.
