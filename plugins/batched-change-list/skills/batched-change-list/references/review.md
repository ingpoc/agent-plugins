# Review

Review each lane after its fix finishes. The main agent owns the issue list, plan, integration, report, and final evidence review; it does not write fix code. Run the Terra work-done review as a separate reviewer session using the coding CLI selected by `codex-cli`. Record `group.codex.implementer_session` and `group.codex.reviewer_session`; run `bcl job --phase review --model NAME --result FILE -- COMMAND` (the page's model column shows the job's model, then the lane's recorded reviewer while in review) to inspect the first `VERDICT:` line: only `VERDICT: PASS` passes, while `CHANGES` or a missing verdict fails and preserves the reviewer note. `bcl review --ids ID` starts the stage and refuses until the lane's fix job exits successfully. A one-file lane records `skipped_one_file` through `bcl review` and needs no reviewer session.

Normal lanes use `gpt-6-luna` Fast at max effort for fixes and `gpt-6.1-terra` at medium for reviews; skip review for one-file fixes. With no prior failure, a first review `CHANGES` returns to the same Luna session. Count `CHANGES` and Verify `not_fixed` per issue from its finding history; at two failures, follow the conditional escalation in [Plan and fix](plan-fix.md). If the Codex quota runs out, follow the quota-out fallback note there so reviews continue.

The review transition checks those session records in `check_install_sessions.py`.

Run the full source suite before Install with `bcl job --phase presuite -- python3 -m unittest discover -s <skill-dir>/scripts -p 'test_*.py'`; the page shows this as **Pre-push suite** within Review. The pre-push suite is a source check, not the post-install Verify stage. Install requires a passed suite and a passed review or recorded one-file skip for each lane.

Use one review round per group; log leftover nits as new issues and do not start a second review. The main agent reviews the result before changing issue status; source tests do not replace installed verification.
