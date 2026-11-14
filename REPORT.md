# AGENT-22 / AGENT-23
Changed: `plugins/agent-computer-use/skills/macos-cua/scripts/compact_backend.py`, `plugins/agent-computer-use/skills/macos-cua/tests/test_compact_mcp.py`, `plugins/agent-computer-use/runtime/voice-cua/tests/test_activity_audit.py`, `REPORT.md`.
Tests: AGENT-22 stale-window/AX-lag unit cases — 3 passed; prior typed-text logging regression — 1 passed.
Lint: `workflow lint` passed with one existing global word-budget warning; diff check clean.
AGENT-23: Existing logging merges typed-text fields once; regression now covers cancel, confirm, TextEdit result, and TextEdit error rows.
Leftover risk: No live runtime check; stale recovery is mock-tested and AX verification waits up to 2 seconds. Strict hygiene flags this branch as local-only; no remote is configured.
Live retest: In a focused, already-open Terminal prompt, type `echo ACU110` via `cua_act` with `expect="echo ACU110"`; expect one insertion and `verified: true`.
Do not press Return; clear the prompt with Ctrl+U afterward.
Follow-up: Retry requires explicit `dispatched: false` and the original window ID; ambiguous, partial, and changed-window failures do not retype.
