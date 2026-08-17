# agent-computer-use

Native macOS computer use through a 5-tool MCP facade over compact AX-first macos-cua workflows.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: [`AGENTS.md`](AGENTS.md). Collection routing: repo-root `AGENTS.md`.

## Benchmarks

Five graded axes, each 0–10. Overall is their unweighted mean. Accuracy and visibility are trust gates, not averaged in.

Measured this session: warm discard then `python3 scripts/run_benchmarks.py --repeat 5 --rate`. File `~/.cache/macos-cua/benchmarks-latest.json` written `2026-08-17T03:42:52Z`. Kept: `seed_snapshot` into asserted plans + Calculator/WhatsApp probes reuse the button/closed tree (floor AX); dropped redundant Calculator post-`app_state`. Rejected: seeding postcondition expects from a pre-mutation tree (false pass on stale display). Model: `skills/macos-cua/references/entry-contract.json` `rating_model`. Do not loosen floors to force a pass.

| | |
| --- | --- |
| Suite overall /10 | **8.0** |
| Rows passing | **4 / 4** |
| Trust-gate zeros | **0** |
| Target overall | **9.5** |

Suite-level graded means: speed 4.8, reliability 9.5, robustness 10.0, efficiency 8.2, context efficiency (`token_efficiency`) 7.5.

### Overall by row (five graded axes)

```mermaid
xychart-beta
    title Overall by row /10
    x-axis [Calculator, Folder, TextEdit, WhatsApp]
    y-axis "Overall /10" 0 --> 10
    bar [9.4, 9.5, 6.5, 6.5]
    line [9.5, 9.5, 9.5, 9.5]
```

### Graded axes /10

```mermaid
xychart-beta
    title Graded axes /10
    x-axis [Calculator, Folder, TextEdit, WhatsApp]
    y-axis "Score /10" 0 --> 10
    bar [7.2, 9.5, 1.7, 1.0]
    bar [10, 8.0, 10, 10]
    bar [10, 10, 10, 10]
    bar [10, 10, 6.0, 6.7]
    bar [10, 10, 5.0, 5.0]
```

Series order: Speed, Reliability, Robustness, Efficiency, Token efficiency.

### Rating table

Accuracy and visibility are gates. Folder visibility is n/a (observe-only). Folder reliability 8.0 is the p95/p50 spread penalty; all five repeats still passed.

| Row | Overall | Speed | Reliability | Robustness | Efficiency | Token | Accuracy | Visibility |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Calculator | 9.4 | 7.2 | 10 | 10 | 10 | 10 | 10 | 10 |
| Folder | 9.5 | 9.5 | 8.0 | 10 | 10 | 10 | 10 | n/a |
| TextEdit | 6.5 | 1.7 | 10 | 10 | 6.0 | 5.0 | 10 | 10 |
| WhatsApp | 6.5 | 1.0 | 10 | 10 | 6.7 | 5.0 | 10 | 10 |

### Reliability

`10 * pass_rate`; subtract 2.0 when p95/p50 duration > 1.5. Suite 9.5.

### Robustness

`10 *` fraction of repeats with `measured.robust`. Suite 10.0.

### Context efficiency

Rating key `token_efficiency`. Suite 7.5.

### Speed

Suite 4.8. Binding constraint on TextEdit and WhatsApp.

```mermaid
xychart-beta
    title Speed floor versus p50 seconds
    x-axis [Calculator, Folder, TextEdit, WhatsApp]
    y-axis "Seconds" 0 --> 10
    bar [1.582, 0.358, 1.284, 0.125]
    bar [1.581, 0.377, 6.301, 1.316]
```

Series order: Floor (s), p50 duration (s).

### Efficiency

Suite 8.2. Calculator at floor (`ax_snapshots` 2).
