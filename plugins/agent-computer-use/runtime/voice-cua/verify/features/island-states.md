# Island states

The menu-bar pill shows Samantha's state (listening, thinking, acting, speaking, done, error, reconnecting) without stealing focus.

## Sub-features
- `island.publish`: each kind renders with title, and `· detail` for acting/done.
- `island.stream`: CUAService consumes `/api/island/stream` (SSE) from the voice gateway.
- `island.nonkey`: the panel never becomes key window.

## How to get to it (user POV)
- Turn Samantha ON in the menu bar; the pill appears under the menu bar.

## Driving it with the gateway
Preconditions: baseline; activity log shows `island_stream_begin` and `island_listening`.
- Publish: `curl -s -X POST 127.0.0.1:$PORT/api/island/publish -H 'Content-Type: application/json' -d '{"kind":"acting","title":"Acting","detail":"open Calculator","app":"Calculator"}'`.
- Readback: `curl -s 127.0.0.1:$PORT/api/island/state` → same kind/detail.
- Pixel proof: `screencapture -x -R0,0,$W,40 evidence/p2/07-island-<kind>.png` per kind (phase2-live-plan step 7).
- Stream check: `curl -s -D - -o /dev/null --max-time 2 127.0.0.1:$PORT/api/island/stream` → event-stream content type.

## Gotchas
- The island only shows after the menu-bar Samantha toggle (StatusBarController starts streaming on ON); publishing before that changes state but renders nothing.
- `island_stream_error ... not the voice gateway` means the island points at a foreign port (see port-clash).
- Unit tests must set a temp `VOICE_CUA_ACTIVITY_LOG`, or they write into the live `samantha-activity.jsonl` and pollute proof deltas.
