---
name: verify-samantha
description: Verify Samantha (voice-cua) end to end on the Mac Mini — gateway, island pill, voice/text turns that drive macOS apps. Use before handing off any voice-cua or island change, or when Samantha "looks off".
---

# verify-samantha

Read [`features/README.md`](features/README.md) first; each feature file is the recipe. Full staged run: `evidence/phase2-live-plan.md` (worktree root).

## Launch
- Mini must be free (no other worker on screen) and `~/.cache/macos-cua/visual-focus.lock` absent or ours.
- CUAService running (`pgrep -x CUAService`). Guru turns Samantha ON from the menu bar; agents never click it.
- Ready = `curl -s 127.0.0.1:$PORT/api/session/status` has `"ready": true` **and** the activity log shows `island_listening`.

## Doctor (read-only, run first and after any failed drive)
```zsh
python3 skills/macos-cua/scripts/runtime_contract.py
```
The launcher runs this same gate before exposing MCP. A missing build record,
changed package, or stale running service blocks startup; rebuild, sync and
restart from one candidate. The signed app records the package fingerprint;
the running service returns the fingerprint captured at launch. Operator status
is optional and does not decide native service health.

Discover the voice gateway from the active helper listener and confirm its
`/health` reports `service=voice-cua` before using `/api/session/status`.
Do not use a port merely because it accepts connections. Source tests and
activity-log history are not current voice evidence.
Never hardcode 8765 — see [`features/port-clash.md`](features/port-clash.md).

## Drive
- Text turn (no mic): `POST /api/session/text {"text": "...", "timeout": 60}`.
- Frozen-helper one-shot (no GUI): `VOICE_CUA_NO_AUDIO=1 VOICE_CUA_PORT=8799 "$H" --text "..."`.
- Island: `POST /api/island/publish {"kind","title","detail","app"}`; read back `GET /api/island/state`.
- Tool call: `POST /api/tools/call {"name":"cua_state","arguments":{"app":"Finder"}}`.

## Evidence
Write to `evidence/p2/` (timestamped). Proof = action + resulting state: activity-log delta from a recorded byte offset (`tail -c +$((ABASE+1)) $ACT`), `/api/island/state` readback, menu-bar strip PNG, and the target app's own readback (e.g. Calculator display), never the tool's `ok`.

## Cleanup
Kill only what you started (by PID). Guru turns Samantha OFF. Never delete `evidence/p2/`; confirm it still exists after cleanup.

## Unit tests
`python3 -m unittest discover -s tests` from `runtime/voice-cua`, always with a temp `VOICE_CUA_ACTIVITY_LOG`.
