# Introspect

After every round's Verify, review every step performed, including successes and orchestration: each group's coding-CLI write and review, parallel group count, polling, rework, tests, install, and verify. Record uneventful steps under `none`.

Prepare `SECTIONS.json` with exactly `steps`, `none`, and `sections`. Include every performed step once; the check derives required steps from this round's plan groups and `tracking.stages_reached`, including each group's write/review and parallel count. Add polling, rework, and other observed details to `steps`; `none` lists reviewed steps with no change. `sections` keys must be ordered `delete`, `simplify`, `optimize`, `automate`. Each section is `{"none":"reason"}` or `{"items":[...]}`. Each item has `item` (an exact `steps` entry), `evidence`, and `outcome` (`fixed inline` or `issue` plus an existing non-WF `issue_id`).

Run `python3 <skill-dir>/scripts/check_introspect.py --data <live-tracker.json> --sections SECTIONS.json`, then `bcl introspect --sections SECTIONS.json`. Both the CLI and manager check coverage against tracker-recorded work, so a self-declared steps list cannot omit plan groups or reached stages. Try deleting first; simplify only what remains, optimize only what was simplified, and automate last. Add a step or script only when it removes or replaces at least as much work.

The call creates one `WF-` task with the ordered sections and full step coverage, closes the current round's plan, and records `tracking.introspect`. Follow [gate.md](gate.md). `report_rollover` requires this round's Introspect after Install.

Do not pass `issues` or `next_cycle` notes on the Introspect call. Create code follow-ups as separate non-WF findings before linking them from a section item. The task's `issues` array stays empty.
