# Report and roll over

Run `bcl report` only when the [gate](gate.md) is met and, after a full cycle, Introspect has been recorded. This marks Report & roll over as the current stage and snapshots the report.

After the PR is actually merged, run `bcl merged`. It records the merge commit SHA and time, moves the workflow to terminal Merged / Done, and retargets open stacked PRs based on this branch to `main` when the configured `gh` or Origin CLI can do so. If listing or retargeting is unavailable, the command records a pending TODO and prints the manual action.

Start a new list with `bcl rollover --new-pr N --worktree NAME` (add `--new-url URL` if the PR URL cannot be derived, or `--force` to override an unmet rollover gate). Rollover writes a separate live file and leaves the old list file and viewer untouched. It carries the old list's verified-fix exclusions and unresolved IDs, saves merge metadata as the rollover source, advances the cycle, clears cycle-only state, and resets carried `in_progress` findings to `untested`; existing `failing` and `blocked` statuses remain. The new list starts at Collect with no activity message.

The new list's page is not published during rollover. Run `bcl --data NEW_FINDINGS.json start` (or use its new live path) to begin Collect and switch that list's viewer. Only findings started on the new list should then show work in progress.

This package does not authorize merging a product PR. Follow the product repository's merge authorization and gate before running `bcl merged`.
