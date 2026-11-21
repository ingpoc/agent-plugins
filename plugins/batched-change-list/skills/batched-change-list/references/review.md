# Review

Review each lane after its fix finishes with `bcl job --phase review --model NAME --result FILE -- COMMAND`. The first nonblank line must be `VERDICT: PASS` or `VERDICT: CHANGES`, and the Must-fix section must say `none` for PASS or name an item for CHANGES. Log leftover nits as new issues, not must-fix items. `bcl review --ids ID` records a one-file skip or starts review after a successful fix.

When a review, fix, or check does not pass, classify the fault before it is counted. A tool fault (cut-off or incomplete verdict, quota-out, a broken check, or a runner crash) is recorded and not counted. A code fault (CHANGES, `not_fixed`, or a real test fail) is counted. After a counted failure, the next fix needs a passed plan review; a second counted failure blocks the lane (see [Plan and fix](plan-fix.md)). If a tool fault was already counted, uncount it with `bcl unblock --ids ID --reason "why" --tool-fault ATTEMPT`. Quota-out is blocked with `quota_wait_until` set to the reset instant, not failed; fall back Codex, then the latest free OpenCode models, then stop and ask the owner. Keep work that needs no model moving. Do not use a separate model-escalation ladder; the two-counted-failure gate and plan review are the retry path.

Quota-out fallback: fixes and reviews run on Codex first; if the Codex quota runs out, free OpenCode models (the latest, per the opencode-free-implementer skill) take the remaining fixes and reviews; if OpenCode is unavailable or out of quota too, stop and ask the owner. A quota-out is a tool fault and is not counted; three tool faults in a row (plan reviews included) block the lane until `bcl unblock --reason`.

Run the full source suite before Install with `bcl job --phase presuite -- python3 -m unittest discover -s <skill-dir>/scripts -p 'test_*.py'`; the page shows this as **Pre-push suite** within Review. The pre-push suite is a source check, not the post-install Verify stage. Install requires a passed suite and a passed review or recorded one-file skip for each lane.

`check_install_sessions.py` validates the selected lane and review receipts before Install.
