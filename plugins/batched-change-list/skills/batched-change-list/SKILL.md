---
name: batched-change-list
description: Use when collecting related product findings for a reviewable change list and coordinated fix cycles.
---
# Batched change list

> **Self-validate after edits.** Run `python3 -m unittest discover -s scripts -p 'test_*.py'` from this skill directory.

Use the existing findings JSON as the sole tracker; HTML is its rendered view. Preserve work, user priorities, authorizations, issue IDs, evidence, and verification history. Never infer acceptance from unlinked telemetry or source tests alone.

Before starting or re-entering each phase, write its `set-stage` value below with `batch --render`; clear it with `null` on pause or finish. Use [tracker operations](references/tracker-operations.md) for the exact command and recovery. The timeline reports the last recorded stage, not independent proof of activity.

For substantial batches, use the native plan tool if available, grouping collect, plan/fix, install/verify and report/rollover; otherwise keep a compact in-context plan. Record individual outcomes in the tracker.

1. **Collect** (`collect`). Identify product, build, scope and expected behavior; exercise accessible checks and record each finding once. Use `scripts/manage_findings.py` and load [tracker operations](references/tracker-operations.md) when reading or changing records. For feedback, screenshots, telemetry and user test cards, load [collection and reporting](references/collection-and-reporting.md).
2. **Present** (`present`). Render with `scripts/render.py` or `batch --render`. For live progress use `scripts/serve.py` and [collection and reporting](references/collection-and-reporting.md) for the fixed-port viewer and rendering contract. Verify navigation and show concise evidence.
3. **Plan and fix** (`plan_fix`). Group issues by root cause and acceptance checks; use existing gates and minimal safe build/install cycles. The main agent owns planning, integration, tracker and final evidence review. Route each task to the cheapest model and effort that can do it; delegate bounded independent work with owned files and focused checks when beneficial, keep small/coupled work inline, and use one operator per shared surface. Load [fix and verify](references/fix-and-verify.md) for delegation and gate details.
4. **Install** (`install`). Update the authorized PR before installing its exact head when required.
5. **Verify** (`verify`). After each install, load [verification runs](references/verification-runs.md), start a distinct run, then start and finish each issue check so its card appears first while checking. Perform accessible checks before asking the user; use [fix and verify](references/fix-and-verify.md) for acceptance requirements.
6. **Report and roll over** (`report_rollover`). Audit stale and repeated details, batch tracker changes, regenerate the HTML, and reconcile statuses, navigation and build identity. When work moves to a new PR, retain the authoritative JSON and its history, update its PR identity, and render a new PR-named HTML file; preserve the prior report and update current links. `verified_fixed` requires relevant tests plus installed/live acceptance when applicable; blocked or inconclusive is not proof. When at least 10 distinct findings in the current PR are `verified_fixed`, finish accessible verification, commit and push the verified-fix report, and propose the merge with its evidence. Merge only under an explicit user instruction or an applicable standing policy in the product repository’s agent instructions; absent either, stop there and ask. After a merge, verify it, then fetch the authoritative remote and fast-forward the local `main` checkout without overwriting local changes before starting the follow-up PR. Open a follow-up PR if unresolved findings remain, preserving all records and history in the same tracker. Do not otherwise merge, publish, install, send messages or run intrusive tests without authorization.
