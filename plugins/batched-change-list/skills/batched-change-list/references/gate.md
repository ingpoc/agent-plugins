# Gate

Run `python3 <skill-dir>/scripts/check_gate_count.py --data <live-tracker.json>` for the authoritative count; `bcl status` uses the same check.

The gate counts `verified_fixed` findings on **this list** except IDs in `tracking.rollover.excluded_ids` and completed `WF-` records marked `fixed_in: Introspect`. Those records only document a review. Real process and page-tool findings still count, including older unresolved `WF-` findings.

The shared exit gate is met when the count reaches `tracking.rollover_threshold` (default 15), or when no counted findings remain open. The same gate authorizes report, rollover, and merge; a count at or above the threshold is always met.

When the gate is met, the workflow strip shows a green check, the exit is active, and the return loop is dashed. When it is not met, the strip shows the amber gate arc and a solid return loop to Plan & fix. After Introspect, if the gate fails, return to Plan & fix and increment `tracking.round`; `tracking.cycle` advances only on PR rollover. Round history records the fixed count and end time.

`bcl report`, rollover, and marking the previous PR merged stay blocked until the shared gate is met (unless `--force` records an override). See [report-rollover.md](report-rollover.md).
