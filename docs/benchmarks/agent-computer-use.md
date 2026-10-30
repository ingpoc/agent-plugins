# agent-computer-use benchmarks

Latest run: Agent Computer Use **0.2.49** (build 380), Mac Mini (Apple silicon), 2026-10-30 10:48 UTC.

Command: `python3 skills/macos-cua/scripts/run_benchmarks.py --repeat 5 --rate`. Results go to `~/.cache/macos-cua/benchmarks-latest.json`. Rating model: `skills/macos-cua/references/entry-contract.json` `rating_model`. Five graded axes (speed, reliability, robustness, efficiency, context efficiency), each 0–10; a row's overall is their mean. Accuracy and visibility are trust gates and are not averaged in.

## 0.2.49 results

| Row | Passed (5 repeats) | Overall | Speed | Reliability | Robustness | Efficiency | Context | Accuracy | Visibility | p50 s (floor) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Calculator 8×8 | yes | 9.2 | 6.2 | 10 | 10 | 10 | 10 | 10 | 10 | 1.64 (1.58) |
| Folder (Downloads) | yes | 9.9 | 9.6 | 10 | 10 | 10 | 10 | 10 | n/a | 0.37 (0.36) |
| WhatsApp new-chat sheet (observe only) | yes | 8.3 | 1.5 | 10 | 10 | 10 | 10 | 10 | 10 | 0.82 (0.13) |
| TextEdit right-click | not measured | — | — | — | — | — | — | — | — | — |

Mean overall of the three measured rows: **9.1**. Previous published run (2026-08-17, pre-0.2.49): 8.0 over four rows.

TextEdit was not measured. The row opens a second, isolated TextEdit process, and CUAService refuses to target one of several instances of the same app ("cannot target pid exactly") rather than risk acting on the wrong window. A TextEdit window was already open on the test Mac. Rerun with TextEdit closed.

WhatsApp speed is the binding axis: one step (opening the sheet) takes about 0.8 s against a 0.13 s floor.

## Known issue found by this run

If only WhatsApp's `ServiceExtension.appex` is running (the main app is closed), app resolution picks the extension's bundle id and the launch fails with `LSCopyApplicationURLsForBundleIdentifier() failed`. Opening WhatsApp first works around it. Fix planned for the next ACU release.
