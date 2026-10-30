# agent-computer-use benchmarks

Latest run: Agent Computer Use **0.2.50** (build 389), Mac Mini (Apple silicon), 2026-10-30 11:23 UTC.

Command: `python3 skills/macos-cua/scripts/run_benchmarks.py --repeat 5 --rate`. Results go to `~/.cache/macos-cua/benchmarks-latest.json`. Rating model: `skills/macos-cua/references/entry-contract.json` `rating_model`. Five graded axes (speed, reliability, robustness, efficiency, context efficiency), each 0–10; a row's overall is their mean. Accuracy and visibility are trust gates and are not averaged in.

## 0.2.50 results

| Row | Passed (5 repeats) | Overall | Speed | Reliability | Robustness | Efficiency | Context | Accuracy | Visibility | p50 s (floor) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Calculator 8×8 | 5/5 | 9.1 | 5.7 | 10 | 10 | 10 | 10 | 10 | 10 | 1.80 (1.58) |
| Folder (Downloads) | 5/5 | 9.8 | 9.0 | 10 | 10 | 10 | 10 | 10 | n/a | 0.40 (0.36) |
| TextEdit right-click | 5/5 | 9.2 | 5.8 | 10 | 10 | 10 | 10 | 10 | 10 | 2.07 (1.28) |
| WhatsApp new-chat sheet (observe only) | 5/5 | 8.2 | 1.2 | 10 | 10 | 10 | 10 | 10 | 10 | 1.01 (0.13) |

Mean overall of all four rows: **9.1**. Previous run (0.2.49): 9.1 over three rows, TextEdit not measured.

Conditions: WhatsApp was not opened beforehand (only its `ServiceExtension.appex` was running), and a user TextEdit window stayed open during the TextEdit row, so the row's isolated TextEdit ran as a second instance.

- WhatsApp: 0.2.50 skips app extensions (`.appex`/`.xpc`) when resolving an app by name, so it launched the real WhatsApp app. The 0.2.49 `LSCopyApplicationURLsForBundleIdentifier() failed` issue is fixed.
- TextEdit: 0.2.50 addresses a twin instance by exact pid (`pid:<n>` selector) instead of refusing with "cannot target pid exactly".
- WhatsApp speed is still the binding axis: opening the sheet takes about 0.7–1.2 s against a 0.13 s floor.
