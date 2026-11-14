# Gate

The gate counts `verified_fixed` findings on **this list** that block rollover. A finding counts only when:

- it is not in `tracking.rollover.excluded_ids`, and
- its id prefix is not a process or page-tool prefix: `WF`, `BCL`, `PAGE`, `TRACK`, `FLOW`, `PLAN`, `RUN`, `HOOK`, or `TEST` (override with `tracking.rollover.gate_exclude_prefixes` only when a list needs a different rule).

Product prefixes such as `AGENT`, `OBS`, `ISLAND`, and `ISSUE` count. Open process or page-tool items do not keep the gate open, and their verified fixes do not inflate the count.

The threshold is `tracking.rollover_threshold` (default 15). The gate is met when the counted verified-fixed total is at least the threshold, or when no counted findings remain open. A count at or above the threshold is always met; never render `N of 15` as not met when `N >= 15`.

When the gate is met, the workflow strip shows a green check, the exit is active, and the return loop is dashed. When it is not met, the strip shows the amber gate arc and a solid return loop to Plan & fix. After Introspect, if the gate fails, return to Plan & fix and increment `tracking.round`; `tracking.cycle` advances only on PR rollover. Round history records the fixed count and end time.

`bcl report` and rollover stay blocked until the gate is met (unless `--force` is supplied, which records the override). See [report-rollover.md](report-rollover.md).
