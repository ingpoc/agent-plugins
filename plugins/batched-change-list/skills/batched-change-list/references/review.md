# Review

Review each lane after its fix finishes with `bcl job --phase review --model NAME --result FILE -- COMMAND`. The first nonblank line must be `VERDICT: PASS` or `VERDICT: CHANGES`, and the Must-fix section must say `none` for PASS or name an item for CHANGES. Log leftover nits as new issues, not must-fix items. `bcl review --ids ID` records a one-file skip or starts review after a successful fix.

Run the full source suite before Install with `bcl job --phase presuite -- python3 -m unittest discover -s <skill-dir>/scripts -p 'test_*.py'`; the page shows this as **Pre-push suite** within Review. The pre-push suite is a source check, not the post-install Verify stage. Install requires a passed suite and a passed review or recorded one-file skip for each lane.

`check_install_sessions.py` validates the selected lane and review receipts before Install.
