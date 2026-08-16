# .optimization/

Agent-owned optimization gate. Skill: `optimization-ledger`.

Commands: `/optimization-ledger` · `add` · `observe` · `evaluate`

- `when:now` — judge immediately after stress (WIP-style)
- `when:sessions` — trial; `/evaluate` when `i≥n` (`n≥10`)
- Every gated line needs `crit` written at add time
- Rejected optimizations are removed safely and re-verified
- `surfaces.jsonl` · `ledger.jsonl`
