# Install

Run `bcl install` after a nonempty plan is recorded for the current cycle. The tracker rejects Install without that plan. `bcl install` updates the stage and the live page before any optional `--then` command runs.

An authorized PR is created or updated before installing when project policy requires an open PR head; install that exact head and verify its identity. Do not merge, publish, install, send messages, or run intrusive tests unless authorized by the user or applicable instructions.

After installation, complete the issue-by-issue pass and add newly discovered issues before starting another fix cycle; stop early only for an urgent or blocking condition and retain the incomplete run. A missing prerequisite blocks only checks that depend on it. Run focused checks during edits, then the required full gate once for the frozen candidate; rerun failed stages after their cause is corrected.

Record the installed build, revision, path, and install evidence on the verification run when you start [Verify](verify.md).
