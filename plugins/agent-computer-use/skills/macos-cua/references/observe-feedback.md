# Observe feedback (agent-computer-use ≥ 0.2.18)

How `act` / `state` speak to the model. **One path:** reuse act deltas; call
`state` only for discovery or after a miss. Never dump full AX into chat.

## Success

Verified effectful `act` returns a **compact AX delta** (often ~300 chars):

- Header: `Changes during action…` / `No changes during action…`
- Current IDs only on changed lines; omitted IDs are **stale** — resolve by
  label or one fresh `state`
- Long text edits: range + short **excerpt**, not the full `AXTextArea` value
- Screenshots: omitted on verified `expect` and typed failures. Attached only when pixels are the evidence (empty AX, Stage Manager thumb, or `screenshot:true`)
- Verification still uses full internal before/after trees (not returned)

## Failure taxonomy

Failed / incomplete acts set `error_type` (+ `failure{reason,tried,nearby,hint}`)
and a short `text` body.

| `error_type` | Meaning |
| --- | --- |
| `target_missing` | Label/control not found |
| `stale_id` | Element index/id no longer valid |
| `observation_incomplete` | Empty/partial settled observation |
| `action_failed` | Native step failed |
| `expect_unverified` | Dispatched but expect not new in settled observation |
| `verification_required` | Mutating act missing `expect` |
| `allow_unverified` | Dispatch-only escape; do not claim done |

Missing after-state discards old IDs.

## Window selection

CUAService prefers the **AX focused** window over the largest CGWindow
(multi-window TextEdit / proof fixtures). After `cmd+n`, type in the same
batched `act` so focus stays on the new document. If `state` shows the wrong
title, stop — do not mutate another document.

## Install roots

`plugin.json` version **0.2.18**. Live copies:

- `~/.agents/plugins/agent-computer-use`
- Codex: `~/.codex/plugins/cache/personal/agent-computer-use/local`

Engine: `~/.cache/macos-cua/CUAService.app` (rebuild via
`python3 service/install_service.py` after Swift changes).
