# comet-control

Drive web apps through one leased visible Comet window. Use when an agent must control or verify pages in Comet without touching Google Chrome or Chrome profiles.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: [`AGENTS.md`](AGENTS.md). Collection routing: repo-root `AGENTS.md`.

## Benchmarks

This plugin has no 0–10 `run_benchmarks.py` suite yet. Scores stay **unmeasured** until a grader writes them. Live proof is the test files below — pass/fail, not a 0–10 fill. Do not invent scores.

| Axis | Score |
| --- | --- |
| Reliability | unmeasured |
| Robustness | unmeasured |
| Context efficiency | unmeasured |
| Speed | unmeasured |
| Efficiency | unmeasured |

### Reliability

Evidence, not a score: `plugin/comet_control/tests/test_dashboard_interactions.py` (click + cursor-at-target). Isolation: `test_multi_agent_isolation.py` must exit 0 on two consecutive runs.

### Robustness

Evidence: lease token private to one process; SPA remount recovery in `skills/comet-control/references/optimize.md`; broker fails closed unless the logged-in Comet runtime is connected.

### Context efficiency

Evidence: `page_context` compact; diagnostics opt-in (`test_developer_diagnostics.py`); `network_summary` stays bounded.

### Speed

unmeasured. No official duration floors.

### Efficiency

Evidence: one durable `durable_lease_controller` per campaign; short-lived shells that drop the lease token are rejected as an antipattern in `SKILL.md`.
