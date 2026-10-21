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
C=~/.cache/macos-cua; ACT=$C/samantha-activity.jsonl
PORT=$(lsof -nP -a -p "$(pgrep -x voice-cua | paste -sd, -)" -iTCP -sTCP:LISTEN | awk 'NR>1{sub(/.*:/,"",$9);print $9;exit}')
curl -s -m3 127.0.0.1:$PORT/health; curl -s -m3 127.0.0.1:$PORT/api/session/status
grep -E 'gateway_port|island_stream_error|scripts not found' $ACT | tail -3
codesign --verify --deep --strict $C/CUAService.app && echo app_sig=0
```
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
