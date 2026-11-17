# Install

Run `python3 <skill-dir>/scripts/check_install_sessions.py --data <live-tracker.json> --ids ID` before Install; repeat `--ids ID` for each finding in the group. It checks implementer and reviewer sessions and requires `codex.review_status: passed`; only a group marked `codex.one_file: true` may omit review. `bcl install` runs the same check and refuses invalid groups. Start Install after a group's review passes with `bcl install --ids ID` (repeat the flag per finding); other groups still fixing or reviewing do not block it. The tracker also requires a nonempty current-cycle plan. Installs and builds share one queue. `bcl install` updates the stage and live page before an optional `--then` command.

An authorized PR is created or updated before installing when project policy requires an open PR head; install that exact head and verify its identity. Do not merge, publish, install, send messages, or run intrusive tests unless authorized by the user or applicable instructions.

After installation, complete the issue-by-issue pass and add newly discovered issues before starting another fix cycle; stop early only for an urgent or blocking condition and retain the incomplete run. A missing prerequisite blocks only checks that depend on it. Run focused checks during edits, then the required full gate once for the frozen candidate; rerun failed stages after their cause is corrected.

Record the installed build, revision, path, and install evidence on the verification run when you start [Verify](verify.md).
