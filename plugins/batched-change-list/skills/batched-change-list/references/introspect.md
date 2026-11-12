# Introspect

Run once per batch, right after the last verification and before report and roll over, while the full cycle is fresh in context. Start with `set-activity` stage `introspect`.

1. **Walk the cycle.** List each step you actually ran in collect, plan/fix, install and verify, with the time or retries it took. Include every script, command, wait, hand-off and user question.
2. **Find the waste.** Flag a step that was redundant, slow, repeated by hand, needed a retry, waited on the user for something you could test yourself, or **failed silently** (no error, empty output, a wrong result shown as success, a swallowed exception, a log line nobody read).
3. **Decide one action per flagged step**, in this order of preference: *delete* it, *simplify* it, *optimize* it, *automate* it with a deterministic script. Check existing scripts and skills first; extend one instead of adding another.
4. **Silent failures are always fixed.** Every script and step must exit non-zero with a one-line cause and name the failing input; a check that cannot run reports `not run: <reason>`, never a pass. Add a test that proves the loud failure.
5. **Record on the tracker.** Add one finding per action (`add`, id prefix `WF-`, source `introspect`; title = the step, body = problem, chosen action, expected saving). Fix small ones inline now; larger ones join the next batch. Write what was removed or automated into the finding's evidence once done.
6. **Update the skill or script** that owns the step in the same PR, so the next agent starts with the better version.

Then `set-activity` stage `report_rollover` and continue.
