# Candidate change: rollover recovery

Baseline: `~/code/worktrees/.agents/bcl-status-experiment/skills/batched-change-list`
Candidate: `~/code/worktrees/.agents/bcl-rollover-experiment/skills/batched-change-list`

The candidate adds `scripts/rollover_experiment.py`. The first version committed
the tracker before writing the report and rejected any retry once
`rollover.target_url` matched. A report-write failure therefore left a rolled-over
tracker, no report, and required a manual `render.py` step.

The change makes the applied state recoverable:

- If `rollover.target_url` already equals the requested URL, require the same
  label, URL, and worktree; render from the current tracker; write the report if
  it is absent; report `"recovered": true`.
- If a report already exists, require byte-identical content instead of
  overwriting it.
- Keep the first-run guard that refuses an existing report path.
- Keep the tracker-first commit order, but the failure message now says to fix
  the path and rerun the same command.
- Convert uncaught tracebacks into the same `rollover: <error>` exit-2
  convention used by `manage_findings.py` and `render.py`.

## Failure reproduced before the change

1. Report directory set to mode `0500`.
2. Helper run: tracker became PR 2 with `excluded_ids: ["ISSUE-02"]`; report was
   absent; process exited 1 with a traceback.
3. Retry after restoring permissions: rejected with
   `rollover target must differ from current PR`.
4. Manual `render.py` produced the correct report (`["ISSUE-01"]`).

## Evidence

- `verify.py`: 26/26 checks pass on the fixed wrapper.
- Negative control against the old wrapper: 11/26 pass, 15 fail.
- Existing skill suite: 32 tests pass.
- OpenCode `deepseek-v4.1-flash-free#low` adoption probe: 2/2 runs used the
  helper and produced the correct tracker/report pair.

The fix is in the experiment copy only. It has not been promoted to the installed
skill.
