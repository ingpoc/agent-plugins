# Voice CUA runtime

Packaged Samantha runtime owned by `agent-computer-use`.

Testing entrypoint: [verify-samantha](verify/SKILL.md), including installed prompt journeys, credential acceptance and before/after project evidence when available.

- `python/voice_cua/`: gateway, GPT-Live session, audio, tools, island bus, settings, and Keychain metadata access.
- `config/secret-templates/`: metadata-only templates (label, Keychain service and account, no values); secret values remain in macOS Keychain. `config/.secret/` is local runtime state (for example `labels.json`) and is gitignored.
- `scripts/build_voice_helper.py`: builds the nested `voice-cua.app` from this package and the adjacent `skills/macos-cua` runtime.
- `tests/test_voice_cua.py`: source-level runtime checks.

Build and installation are owned by `skills/macos-cua/service/install_service.py`.

Recovery forks only after `session.closed` confirms the source recording ended;
an unfinalized transport loss starts a new session with saved text history.
Late spoken updates are added to a busy backend's next continuation without
starting another response or claiming its work was cancelled.

`assistant_reply.source` distinguishes backend text from the spoken transcript.
Audio latency is sampled once per locally completed speech burst (`turn_id`),
excluding overlapping speech. Output `item_id` values group audible bursts and
are synthetic, not server turn IDs. A `ws_closed` record includes safe request
and edge trace IDs when supplied, the last server event type, and whether a
WebSocket close frame was received. These diagnostics do not identify the
cause of an unframed transport loss by themselves.

A confirmed missing Comet lease clears Samantha’s local lease pointer. The failed action is not replayed; the next browser task must acquire a new lease with `comet_begin`. Timeouts retain the existing lease because ownership remains uncertain.

Connection-close diagnostics include successful client-send and microphone-append ages, plus ping/pong timing captured before transport teardown when available. They contain no audio payloads and do not establish a disconnect cause. Repository test runs write activity to temporary logs rather than the live Kindle feed.

Browser function schemas are available at delegation start; their connections remain lazy and actions require an active lease. Credential cancellation emits `tool_cancelled` without creating a workflow repair latch. Explicit acceptance checkers can emit validated `task_outcome` records; these are machine-check results, not inferred assistant completion.
