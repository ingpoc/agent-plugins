# Evaluating a skill recommendation

Purpose: decide **keep / remove / hold / out of scope** at the lowest cost that
can actually settle the claim. The test type follows the claim type. Running an
agent is the most expensive step and is reserved for claims about agent behavior.

## Classify the claim first

| Claim | Required evidence | Observed cost | Decision rule |
| --- | --- | --- | --- |
| The behavior already exists | Read-only baseline inspection of code, tests, rendered output | Minutes | Close as already present; do not build a candidate |
| Contract / correctness / recovery | Deterministic harness, plus a negative control against the old version | 0.4 s for 26 checks | Keep only if every check passes; remove or hold on any failure |
| The agent must choose the mechanism | 1–2 adoption probes on the current provider | 101 s for 2 OpenCode runs | Mechanism must appear and the artifact must be correct; otherwise fix the affordance and re-probe before measuring |
| Faster / cheaper than the baseline | Predeclared matched pairs, balanced order, failures retained, primary endpoint + guardrails | 470 s for 12 runs | Adopt only on a primary-endpoint win; a tie is not adoption |
| Policy / prose / threshold | Owner review and an explicit scope decision | Minutes | Do not benchmark; decide and record the owner |

## Stages

1. **Frame.** Name the owner, the observable behavior, the evidence tier the
   claim needs, and a spend ceiling. Write the acceptance condition before
   building anything.
2. **Baseline check.** Inspect the current code, tests, and rendered output. If
   the behavior is already present, stop and record it. The status view stopped
   here: status, source, latest-check label, and version timeline already exist.
3. **Isolate the candidate.** Copy the skill to a separate worktree, pin the tree
   hash and fixture hash in a plan, and record the change as a diff summary. Do
   not edit the installed skill.
4. **Deterministic contract.** Exercise the happy path, failure window, retry,
   idempotency, metadata conflicts, and render failures. Run the same harness
   against the old version as a negative control. This is cheap and should
   precede any agent run.
5. **Adoption probe.** Only when the agent must select the mechanism. Two runs
   are enough to see whether the instruction is followed. If the mechanism is not
   used, the later comparison would measure nothing; fix the affordance first.
6. **Matched comparison.** Only for an efficiency claim. Predeclare pairs,
   cases, order balance, endpoints, and the decision rule. Record per-pair wins
   and medians; keep failed or rejected runs. Never infer savings from total
   input when cache composition differs.
7. **Decide and record.** Use one of: `keep`, `remove`, `hold`, `out of scope`.
   State the evidence tier: source test, installed build, or live run. Record the
   cost and the trigger for revisiting. Update the tracker and the rendered view.

## Stop rules

- Do not run an agent to prove a deterministic contract.
- Do not run matched pairs before adoption is observed; otherwise both arms may
  use the old mechanism.
- Do not adopt on a tied primary endpoint, even if secondary metrics improve.
- Do not benchmark prose or a policy threshold.
- Do not promote a one-pair result to a general efficiency verdict.
- Do not claim installed or live evidence from source tests.
- Keep the failures; they are the reason the decision is credible.

## Applied to this session

| Recommendation | Furthest stage reached | Outcome | Cost |
| --- | --- | --- | --- |
| Stage shortcut | 12-run matched pilot, both providers | **Remove / do not adopt**: 6/6 adoption on OpenCode, but median calls tied 8 vs 8; time and uncached input improved | 470 s pilot + review |
| Rollover command | Contract fix + 2-run adoption probe | **Keep in experiment**: 26/26 checks, negative control fails 15/26, 2/2 OpenCode adoption; efficiency still directional from one Codex pair | 0.4 s + 101 s |
| Provenance status view | Baseline inspection | **Out of scope for this PR**: behavior largely present; needs only a provenance label | Minutes |
| Authorization / 10-finding threshold | Baseline inspection | **Out of scope for this PR**: no code enforces it; it is an operational instruction | Minutes |
| Structured input convenience | Command check, folded into stage pilot | **Remove as a separate item**: no distinct agent trial | Included above |

The ordering is the point: the rollover claim was settled in under two minutes
because it was a contract, while the stage claim needed roughly eight minutes of
agent runs and still ended in a tie on its primary endpoint.
