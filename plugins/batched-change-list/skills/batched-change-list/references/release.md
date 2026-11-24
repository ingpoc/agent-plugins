# Release checks

Before pushing or installing a release, run `bcl release-check --ids ID` for each finding shipped in this version (repeat `--ids` as needed). A named lane is included only after its fix and review pass; other in-flight lanes remain listed until included or deferred/parked with a reason.

For a lane release, run presuite with `CI=1 python3 scripts/check.py --jobs 6 --changed-since <last released commit or origin/main>` and record which plugins ran in the presuite job. Merge to main and Origin CI still run the full suite, so the gate is unchanged.

After `bcl install-local`, or any install of a newly shipped tool version, run `bcl release-check`. It also lists counted failures whose tool-recorded fault kind (cut-off or incomplete review, quota-out) makes them tool faults, and `ambiguous` legacy rows whose old reason text cannot tell a cut-off from a CHANGES reply; reviewer text is never classified. The only uncount path is `bcl unblock --ids ID --reason "why" --tool-fault ATTEMPT`; name an ambiguous row only after you read its reply. Leave real code failures counted. This sort is not Introspect and not rollover: a new list still carries existing `failing` and `blocked` statuses until this check runs.

Parking several overflow lanes in one batch is checked against the final plan, so a single intermediate count cannot refuse a park that lands under the cap. A deferred or parked lane is not in flight for a release of other finished lanes. A finished fix that still needs review enters review in the next round.
