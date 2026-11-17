---
name: batched-change-list
description: Use when collecting related findings into reviewable change lists and coordinated fix cycles.
---
# Batched change list

> **Self-validate after edits.** Regenerate the chart: `python3 scripts/statechart.py`; run `python3 -m unittest discover -s scripts -p 'test_*.py'`.

Workflow: Collect and Present once; then loop Plan & fix → Install → Verify → Introspect → Gate. At 15+ fixes, Report & roll over → Merge previous PR.

1. Collect
2. Present
3. Plan & fix
4. Install
5. Verify
6. Introspect
7. Gate
8. Report & roll over / Merge

| When | Load |
|---|---|
| New list or feedback | New list or feedback: `references/collect.md`; After collection: `references/present.md` |
| After Present or gate loop-back | After Present or gate loop-back: `references/plan-fix.md`; After group review: `references/install.md`; After its install: `references/verify.md` |
| After every round's Verify | After every round's Verify: `references/introspect.md` |
| After Introspect or checking rollover | After Introspect or checking rollover: `references/gate.md`; When gate is met or the PR merges: `references/report-rollover.md` |
| Recovery, viewer use, debugging, tests, sequencing, shared state, or proof | Recovery: `references/tracker.md`; For viewer use: `references/viewer.md`; For debugging, tests, sequencing, shared state, or proof: `references/principles.md` |

Install, Introspect, and Gate checks: `check_install_sessions.py`, `check_introspect.py`, `check_gate_count.py`.
