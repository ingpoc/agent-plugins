# Port clash

When another process holds the preferred gateway port 8765, VoiceSupervisor moves the gateway to the next free port and re-points the island.

## Sub-features
- `port.move`: 8765 held by a non-voice pid → gateway on 8766+, logged as `gateway_port`.
- `port.island`: island follows the new port (`onGatewayPortChanged`).
- `port.stale`: a stale voice gateway on the chosen port is shut down (`stale_gateway_shutdown`).

## How to get to it (user POV)
- Turn Samantha ON while codex-mac-bridge is running.

## Driving it with the gateway
Preconditions: baseline; record `lsof -nP -iTCP:8765 -sTCP:LISTEN`.
- Holder: the 8765 listener is codex-mac-bridge (not voice-cua).
- Log proof: `tail -c +$((ABASE+1)) $ACT | grep gateway_port` → `port=8766 (8765 held by non-voice pid)`.
- Health: `curl -s 127.0.0.1:$PORT/health` on the discovered port, and no `island_stream_error` in the delta.

## Gotchas
- codex-mac-bridge owns 8765 on the Mini; never kill it and never hardcode 8765 (`gateway.py` DEFAULT_PORT, `island_facade.py` DEFAULT_GATEWAY default to it).
- Frozen-helper text runs use `VOICE_CUA_PORT=8799` to avoid both.
- Run OpenCode only with `--standalone`: plain `opencode stats/session` starts the background service, which resumes stale sessions that edit this worktree.
