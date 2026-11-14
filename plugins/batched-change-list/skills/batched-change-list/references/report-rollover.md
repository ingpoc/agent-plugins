# Report and roll over

Run `bcl report` only when the [gate](gate.md) is met and, after a full cycle, Introspect has been recorded. The command snapshots the verified-fix report in the home worktree.

At rollover, set `tracking.rollover.excluded_ids` to the IDs already verified fixed in the merged PR. Keep their records and history in the tracker; rendering excludes those IDs while retaining carried-over, newly added, and subsequently fixed findings. Render the new PR-named report and switch the owned viewer to it before removing the old worktree; verify the served bytes match the new report.

Use one tracker `rollover` operation (append `--force` only to override an unmet gate): `{"action":"rollover","source_pr":N,"pull_request":{"label":"PR #M","url":"…"},"worktree":"new-name","now":"…"}`. It sets `excluded_ids` to every `verified_fixed` finding (keeping earlier exclusions), writes `unresolved_ids`, updates the page header's PR and worktree, increments `tracking.cycle`, clears introspect and stages-reached, and starts the next list at `collect`. Do not hand-edit those fields.

Merge sequence when the verified-fix threshold is met, and only with standing authorization in the **product** repository: mark the draft PR ready and arm the merge in one step (on Origin: `origin pr ready N && origin pr merge N --auto --expect-head N=<head sha>`, which merges when required checks pass and refuses if the head moved; no waiting loop; if CI fails, fix the flaky test at its root and push), retarget any stacked PR to the base, open the roll-over PR from the new base, run `rollover`, rename the worktree folder to match, and restart the viewer on the new path so the page serves the new header. This package ships no standing merge authorization of its own.
