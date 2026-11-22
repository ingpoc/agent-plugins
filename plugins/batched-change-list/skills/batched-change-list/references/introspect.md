# Introspect

After every round's Verify, review every step performed, including successes and orchestration: each group's coding-CLI write and review, parallel group count, polling, rework, tests, install, and verify. Record uneventful steps under `none`.

Start from the recorded run with `bcl introspect --draft SECTIONS.json`. This writes an editable file only; it does not record Introspect or change the live tracker. Review the performed steps and all four sections, add evidence-backed items or move reviewed no-change steps into `none`, then edit the file. The draft keeps `none` empty and uses conservative section reasons so it cannot be saved unchanged. `steps` includes the current plan groups and reached stages, including each group's write/review, the parallel count, and every stored `verify.command` when Verify was reached. Add observed polling, rework, and other details to `steps`.

Run `python3 <skill-dir>/scripts/check_introspect.py --data <live-tracker.json> --sections SECTIONS.json`, then `bcl introspect --sections SECTIONS.json` to save after review. Both the CLI and manager check coverage against tracker-recorded work, so a self-declared steps list cannot omit plan groups or reached stages. Sections keys must be ordered `delete`, `simplify`, `optimize`, `automate`. Each section is `{"none":"reason"}` or `{"items":[...]}`. Each item has `item` (an exact `steps` entry), `evidence`, and `outcome` (`fixed inline` requires a command or named test in `proof`; `issue` requires an existing non-WF `issue_id`). Skill edits from Introspect name the [principles](../principles.md) items they were checked against.

The call appends the ordered sections, steps, result, and timestamp to `tracking.introspect_history` and updates the current-round `tracking.introspect` record without creating a finding. Follow [gate.md](gate.md); `report_rollover` requires this round's Introspect after Install.

A `fixed inline` item needs a `proof` naming the command or test that shows it. Do not pass `issues` or `next_cycle` notes on the Introspect call. Create code follow-ups as separate non-WF findings before linking them from a section item.

Re-sorting open failures after a tool ship is a [release check](release.md), not an Introspect pass.
