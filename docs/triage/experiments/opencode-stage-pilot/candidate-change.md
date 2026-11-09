# Candidate skill change

Baseline: `~/code/worktrees/.agents/bcl-status-experiment/skills/batched-change-list`
Candidate: `~/code/worktrees/.agents/bcl-input-experiment/skills/batched-change-list`

The candidate already contained the single-stage `stage` command and a note in
`references/tracker-operations.md`. The pilot's first probes showed agents still
chose `batch`, so the candidate `SKILL.md` instruction was changed to make the
single-stage command the primary affordance.

Before:

```text
Before starting or re-entering each phase, write its `set-stage` value below with `batch --render`; clear it with `null` on pause or finish.
```

After:

```text
Before starting or re-entering each phase, set it with the single-stage command:
`python3 scripts/manage_findings.py stage FINDINGS <phase> --render REPORT`
(use `none` to clear). Use `batch` only when combining the stage change with
other operations.
```

The candidate tree hash used by the pilot is pinned in `plan.json`.
