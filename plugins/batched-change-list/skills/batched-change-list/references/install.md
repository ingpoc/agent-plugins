# Install

Start Install with `bcl install --ids ID` (repeat the flag per finding). Its readiness is `bcl release-check` for the named lanes: each needs a passed fix, a passed review job, and a passed pre-push suite, and no other lane may be in flight. The tracker also requires a nonempty current-cycle plan. Installs and builds share one queue. `bcl install` updates the stage and live page before an optional `--then` command.

An authorized PR is created or updated before installing when project policy requires an open PR head; install that exact head and verify its identity. Do not merge, publish, install, send messages, or run intrusive tests unless authorized by the user or applicable instructions.

After installation, complete the issue-by-issue pass and add newly discovered issues before starting another fix cycle; stop early only for an urgent or blocking condition and retain the incomplete run. A missing prerequisite blocks only checks that depend on it. Run focused checks during edits, then the required full gate once for the frozen candidate; rerun failed stages after their cause is corrected.

Record the installed build, revision, path, and install evidence on the verification run when you start [Verify](verify.md).

A mid-cycle plugin or product release, and the re-sort of open failures after it ships, is a [release check](release.md), not this group's Install.
