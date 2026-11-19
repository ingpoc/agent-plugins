# Gate

Run `python3 <skill-dir>/scripts/check_gate_count.py --data <live-tracker.json>` for the authoritative **report gate** count, including its no-open-findings exception. `bcl status` reports `report_gate` eligibility and `verified_gate` threshold completion separately; `rollover_gate` shows the higher rollover threshold.

The gate counts `verified_fixed` findings on **this list** except IDs in `tracking.rollover.excluded_ids` and completed `WF-` records marked `fixed_in: Introspect`. Those records only document a review. Real process and page-tool findings still count, including older unresolved `WF-` findings.

The report gate is met when the count reaches `tracking.rollover_threshold` (default `gate.default_threshold` in `scripts/workflow_definition.json`), or when no counted findings remain open. Rollover requires the count to reach the higher of the rollover threshold and `gate.merge_previous_pr_threshold`; it has no no-open-findings exception. Merging the previous PR also requires `gate.merge_previous_pr_threshold` and does not inherit the no-open-findings exception.

The workflow strip shows a green check only when the verified count reaches the threshold. The report no-open-findings exception may allow `bcl report` while the strip remains not met; the exception appears as a plain note. When the verified threshold is not met, the strip shows the amber gate arc and a solid return loop to Plan & fix. After Introspect, if the gate fails, return to Plan & fix and increment `tracking.round`; `tracking.cycle` advances only on PR rollover. Round history records the fixed count and end time.

`bcl report` and rollover use the report/rollover gate; `bcl merged` uses `gate.merge_previous_pr_threshold` and does not inherit the no-open-findings exception. See [report-rollover.md](report-rollover.md).
