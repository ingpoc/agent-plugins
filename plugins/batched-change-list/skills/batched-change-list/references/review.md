# Review

Review each lane after its fix finishes. The main agent owns the issue list, plan, integration, report, and final evidence review; it does not write fix code. Run the Terra work-done review as a separate reviewer session using the coding CLI selected by `codex-cli`. Record `group.codex.implementer_session` and `group.codex.reviewer_session`; run `bcl job --phase review --result FILE -- COMMAND` to inspect the first `VERDICT:` line: only `VERDICT: PASS` passes, while `CHANGES` or a missing verdict fails and preserves the reviewer note. `bcl review --ids ID` starts the stage and refuses until the lane's fix job exits successfully. A one-file lane records `skipped_one_file` through `bcl review` and needs no reviewer session.

The review transition checks those session records in `check_install_sessions.py`.

Run the full source suite before Install with `bcl job --phase presuite -- python3 -m unittest discover -s <skill-dir>/scripts -p 'test_*.py'`; the page shows this as **Pre-push suite** within Review. The pre-push suite is a source check, not the post-install Verify stage. Install requires a passed suite and a passed review or recorded one-file skip for each lane.

Use one review round per group; log leftover nits as new issues and do not start a second review. The main agent reviews the result before changing issue status; source tests do not replace installed verification.
