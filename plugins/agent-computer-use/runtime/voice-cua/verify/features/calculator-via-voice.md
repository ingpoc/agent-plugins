# Calculator via voice

A user asks Samantha to open Calculator (and compute); she drives it through macos-cua and says the result.

## Sub-features
- `calc.open`: "Samantha, open Calculator" launches/focuses Calculator.
- `calc.compute`: "what is 8 times 8" → display reads 64.
- `calc.island`: pill goes listening → thinking → acting·Calculator → done → speaking → listening.

## How to get to it (user POV)
- Speak to Samantha after turning her ON in the menu bar.
- Text turn via the gateway (same session, no mic).

## Driving it with the gateway
Preconditions: baseline; Calculator may be closed.
- Text turn: `POST /api/session/text` accepts waits up to 1800 seconds. Use `{"text":"Open Calculator and compute 8 times 8","timeout":60}` for a bounded synchronous wait, or add `"async":true` and poll `GET /api/session/turn?id=<turn_id>` with `X-Voice-CUA-Approve`; a wait timeout leaves the turn running.
- Typed confirmation: use the per-process token header for `GET /api/confirm/pending` and `POST /api/session/approve`. Island buttons continue to use `POST /api/confirm`.
- Log proof: `tail -c +$((ABASE+1)) $ACT | grep -E 'tool_start|tool_result|tool_error'` → `cua_act` with `app=Calculator`, no `tool_error`.
- App proof: `pgrep -x Calculator`, then an independent macos-cua `state` on Calculator showing `64`.
- Voice path: the operator speaks; verify recognized input, Calculator display, audible answer and visible Island behavior. Follow the test-run workflow for evidence and cleanup.

## Gotchas
- `python/voice_cua/inject.py` is the Keychain clipboard secret inject (`secrets_inject`), **not** a way to drive a turn; use `/api/session/text`.
- Tool `ok` is not proof — read the display back.
- Skip if `visual-focus.lock` belongs to another run.
