# Observe feedback (agent-computer-use ≥ 0.2.18)

How `act` / `state` speak to the model. Keep this file loaded when debugging
bloated AX dumps, wrong-window verifies, or opaque failures.

## Success

Verified effectful `act` returns a **compact AX delta** (often ~300 chars), not
the full settled tree:

- Header: `Changes during action…` / `No changes during action…`
- Current IDs only on changed lines; omitted IDs are **stale** — resolve by
  label or a fresh `state`
- Long text edits: range + short **excerpt**, not the full `AXTextArea` value
- Screenshots: `screenshot_before` / `screenshot_after` still attached
- Verification still uses the **full** internal before/after trees

## Failure taxonomy

Failed / incomplete acts set `error_type` (+ `failure{reason,tried,nearby,hint}`)
and a short `text` body. Do **not** expect a full AX dump.

| `error_type` | Meaning |
| --- | --- |
| `target_missing` | Label/control not found |
| `stale_id` | Element index/id no longer valid |
| `observation_incomplete` | Empty/partial settled observation |
| `action_failed` | Native step failed |
| `expect_unverified` | Dispatched but expect not new in settled tree |
| `verification_required` | Mutating act missing `expect` |
| `allow_unverified` | Dispatch-only escape; do not claim done |

Missing after-state discards old IDs. Re-`state` when you need the full tree.

## Window selection

CUAService prefers the **AX focused** window over the largest CGWindow
(multi-window TextEdit / proof fixtures). After `cmd+n`, type in the same
batched `act` so focus stays on the new document. If `state` shows the wrong
title, stop — do not mutate another document.

## Install roots

Plugin package version is in `plugin.json` (currently **0.2.18**). Live copies:

- `~/.agents/plugins/agent-computer-use`
- Codex: `~/.codex/plugins/cache/personal/agent-computer-use/local`

Engine binary: `~/.cache/macos-cua/CUAService.app` (rebuild via
`python3 service/install_service.py` after Swift changes).
