# Samantha verification map

## Baseline preconditions
- CUAService running and validly signed; Samantha toggled ON by Guru; doctor passes.
- `$PORT` discovered from the voice-cua listener (never assumed 8765).
- Record `ABASE=$(wc -c < ~/.cache/macos-cua/samantha-activity.jsonl)` before every drive.

## Proof rules
- Assert on the activity-log delta, `/api/island/state`, and the target app's readback — not on HTTP `ok`.
- A journey that can't run reports the unmet precondition; never weaken it to pass.

## Features
- [Calculator via voice](./calculator-via-voice.md): text/voice turn → `cua_act` → Calculator result.
- [Island states](./island-states.md): every pill kind renders and reads back.
- [Port clash](./port-clash.md): gateway moves off 8765 when codex-mac-bridge holds it.
