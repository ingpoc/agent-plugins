---
name: batched-change-list
description: Use when collecting related product findings for a reviewable change list and coordinated fix cycles.
---
# Batched change list

> Self-validate after edits with `python3 -m unittest discover -s scripts -p 'test_*.py'` from this skill directory.

Workflow: Collect and Present once; then loop Plan & fix → Install → Verify → Introspect → Gate. At 15+ fixes, Report & roll over → Merge previous PR.

Load one reference for the current trigger:

- New list or feedback: [collect.md](references/collect.md).
- After collection: [present.md](references/present.md).
- After Present or a gate loop-back: [plan-fix.md](references/plan-fix.md).
- After a group's review: [install.md](references/install.md).
- After its install: [verify.md](references/verify.md).
- After every round's Verify: [introspect.md](references/introspect.md).
- After Introspect or when checking rollover: [gate.md](references/gate.md).
- When the gate is met or the PR merges: [report-rollover.md](references/report-rollover.md).
- For tracker recovery: [tracker.md](references/tracker.md); for viewer problems: [viewer.md](references/viewer.md).
- For debugging, tests, sequencing, shared state, or proof: [principles.md](references/principles.md).
