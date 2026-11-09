# Working principles

Bundled so the workflow has no external skill dependency. Load this file when a batch reaches debugging, test writing, sequencing, parallel workers, or the completion gate.

## Fix root causes

Reproduce the failure, then trace to the first wrong decision or mishandled legitimate state. Fix the owner of that decision, not the symptom or a downstream guard, and then check whether the same fault exists elsewhere. A second code path that works around the bug is a new bug.

## Test behavior, not implementation

Assert the user-visible or contract-level result. Tests that name private helpers, internal call order, or literal log text fail on correct refactors and pass on broken behavior. One assertion that can fail for the reported reason is worth more than several that restate the code.

## Sequence verifiable units

Order the batch so each step leaves the system in a state that can be checked before the next step depends on it. Cheap deterministic checks first; expensive or user-facing verification last, once per frozen candidate. Never stack two unverified changes.

## Separate before serializing shared state

When workers could write the same tracker, app, browser session, device, or branch, give each a disjoint owned surface. Serialize only what genuinely must be single-operator, and say which surface that is. Coordination cost is a design signal, not a scheduling problem.

## Never block on the human

While waiting for input, test credentials, or a decision, continue the independent authorized work that does not depend on the answer. Report the blocking question once, with the exact action needed, and keep the rest of the batch moving.

## Prove it works

Completion needs the proof that matches the claim: a source test for logic, an installed or live run for runtime behavior, a readback for data. A green aggregate, an unchanged log, or an absent error event is not proof of a fix. If proof is missing or conflicting, report the weaker status and say what would settle it.
