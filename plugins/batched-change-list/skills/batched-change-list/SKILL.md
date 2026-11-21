---
name: batched-change-list
description: Use when collecting findings one at a time, managing build-install-test fix cycles, handling PR rollover or merging, or to view the change-list page.
---
# Batched change list

- The orchestrator does not write fix code, so the fix and its review stay separate.
- Codex routing lives in the `codex-cli` skill, so model and effort stay in one place.
- Only the orchestrator writes the tracker and commits, so the list and its history have one writer.
- Run only 3 or 4 independent groups and defer dependent ones, so one fix does not break another mid-round.
- Status accuracy comes first; every “already present” item needs its own named test, so fixed or present claims are proved.
- Show one fix total with no product/process split, so the gate count is the number the owner sees.
- Run Introspect after every Verify, so each round is reviewed in delete, simplify, optimize, automate order before the gate.

| Step | When | Load | Run |
|---|---|---|---|
| Collect | New list or feedback | `references/collect.md` | `bcl start` |
| Present | After collection | `references/present.md` | `bcl start` |
| Plan & fix | After Present or gate loop-back | `references/plan-fix.md` | `bcl plan`, `bcl job --phase fix` |
| Review | After a group's fix | `references/review.md` | `bcl review; bcl job` |
| Install | After group review | `references/install.md` | `bcl install`, `check_install_sessions.py` |
| Verify | After its install | `references/verify.md` | `bcl verify` |
| Introspect | After every round's Verify | `references/introspect.md` | `bcl introspect`, `check_introspect.py` |
| Gate | After Introspect or checking rollover | `references/gate.md` | `bcl status`, `check_gate_count.py` |
| Report & roll over | When gate is met | `references/report-rollover.md` | `bcl report`, `bcl rollover` |
| Merge previous PR | When the PR merges |  | `bcl merged`, `bcl install-local` |

| When | Load |
|---|---|
| Before a release or after shipping | `references/release.md` |
| Recovery | `references/tracker.md` |
| Viewer use | `references/viewer.md` |
| Debugging, tests, sequencing, shared state, or proof | `references/principles.md` |

Self-validate: regenerate the chart with `python3 scripts/statechart.py`; run `python3 -m unittest discover -s scripts -p 'test_*.py'`.
