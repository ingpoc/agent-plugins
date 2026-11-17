---
name: batched-change-list
description: Use when collecting related product findings for a reviewable change list and coordinated fix cycles.
---
# Batched change list

> **Self-validate after edits.** Run `python3 -m unittest discover -s scripts -p 'test_*.py'` from this skill directory.

Adopt once with `bcl --data FINDINGS.json adopt`; thereafter the live state is authoritative and the HTML and committed JSON are snapshots. Use `bcl --data FINDINGS.json <stage>` at each stage; on stale snapshots, missing evidence, or command failure, stop that lane, report the error, and recover using [tracker.md](references/tracker.md). Never treat missing output, a failed script, or a snapshot as proof of success.

Handle new messages throughout the cycle: answer status questions, incorporate corrections and preferences, record new findings before fixing them, and keep independent authorized work moving. A changed requirement invalidates affected plans, renders, reviews, and checks; update the tracker and rerun affected proof. Preserve issue history and distinguish the current finding status, last verified outcome, observed build, and verified build. Never invent missing evidence.

Collect and Present once, then loop Plan & fix → Install → Verify → Introspect → Gate. Prioritize product and process work by dependencies and impact; run independent groups in bounded parallel lanes. Each group moves through fix → review → install → verify; serialize builds and live verification. Keep summaries, impact, and done-when plain; put technical evidence in details. Apply [principles.md](references/principles.md) for debugging, testing, sequencing, shared resources, or proof.

Judge success by independently accepted delivery, accurate current status, and controlled execution. Claim time or token savings only from comparable end-to-end runs that include orchestration and verification; otherwise report them as unmeasured.

Load only the reference for the current step:

- New list or feedback: [collect.md](references/collect.md).
- After collection: [present.md](references/present.md).
- After Present or a gate loop-back: [plan-fix.md](references/plan-fix.md); record each group's verification command.
- After a group's review: [install.md](references/install.md).
- After its install: [verify.md](references/verify.md).
- After a full cycle: [introspect.md](references/introspect.md).
- After introspection or when checking rollover: [gate.md](references/gate.md).
- When the gate is met or the PR merges: [report-rollover.md](references/report-rollover.md).
- For tracker recovery or comparing a prior PR: [tracker.md](references/tracker.md); for viewer problems: [viewer.md](references/viewer.md).
