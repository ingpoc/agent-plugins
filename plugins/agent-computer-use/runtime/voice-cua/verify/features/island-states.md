# Island states

The menu-bar pill shows Samantha's state (listening, thinking, acting, speaking, done, error, reconnecting) without stealing focus.

Routine conversation states use a stable 74 pt minimum and do not resize with
loudness or replay the pinch/blur transition on each turn. With no target, the
voice slot starts 9 pt from the left and the 6 pt dot ends 9 pt from the right;
idle, listening, thinking and speaking keep both x positions. The clear slot-to-dot
gap is at least 8 pt, so the pill grows if content needs it. When controlled, the
24 pt gap begins at the ring's outer stroke edge.

The voice slot remains reserved from listening through done plus 2 s; done is a
harness badge, not a pill glyph. Width grows immediately and shrinks after 400 ms
of stable content. Target and app activity are held through the turn. A speaking
slot without audio returns to idle after 3 s. Successful tool results remain
compact; click opens details. Music resumes after three seconds without voice/work
activity. Errors and approvals retain their attention treatment, and reconnecting
remains visible immediately.

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
- Pixel proof: `screencapture -x -R0,0,$W,40 <private-evidence-dir>/island-<kind>.png` per kind.
- Stream check: `curl -s -D - -o /dev/null --max-time 2 127.0.0.1:$PORT/api/island/stream` → event-stream content type.

## Offline design checks
- `swift test --filter Island` from the service package covers state priority, content-sized cards, named recovery controls, and layout bounds.
- `CUA_ISLAND_RENDER_DIR=<absolute-directory> swift test --filter 'IslandP7RenderTests|IslandFixtureRenderTests'` renders sample voice and activity states without microphone or API calls.
- `IslandVoiceSlotDesignTests`, `IslandVoiceAppsInsideTests`, `IslandHarnessMotionTests`, `IslandTargetGlyphTests` and `IslandLayoutLogTests` cover minimum width, 1×/2× geometry, voice holds, stable harness slots, no-tile marks, and log fields.
- `voice.log` lines carry ISO 8601 timestamps with offset; each listening/thinking/speaking/idle change emits an `island_voice_state` JSONL event beside `island_layout`.
- Listening uses a microphone cue; speaking uses a speaker cue. Reconnecting stays ahead of music, calendar, file-tray, HUD, and peek activity; approvals retain priority.
- Volume/brightness use a 120 pt pill with an 8 pt icon-to-bar gap. Quick Peek fits its title, count, and age up to 244 pt. Music/calendar/tray compact pills use 74 pt; the icon-only calendar secondary uses 44 pt.
- Consent cards grow to show the full approval description; uncertain actions use a question mark and inspection cue, without suggesting retry. Reconnecting offers a labeled Turn off control.
- The expired card exposes Dismiss and Open in chat; dismissal keeps the source reminder. Armed agent stops read Confirm stop.
- Live voice and network reliability remain a separate check; offline preview success does not establish either.

## Gotchas
- The island only shows after the menu-bar Samantha toggle (StatusBarController starts streaming on ON); publishing before that changes state but renders nothing.
- `island_stream_error ... not the voice gateway` means the island points at a foreign port (see port-clash).
- Unit tests must set a temp `VOICE_CUA_ACTIVITY_LOG`, or they write into the live `samantha-activity.jsonl` and pollute proof deltas.
