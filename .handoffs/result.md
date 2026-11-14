# ACU 0.2.143 group A+C result

- AGENT-26: typed responses retain their turn association, settle a uniquely matched waiter when response metadata is missing, and time out after five minutes without response or tool progress.
- AGENT-29: reconnect notes replay repo, branch, current task, and last assistant result into the new conversation.
- OBS-28: typed errors and assistant replies are returned and logged without the 240/2000-character truncation.
- AGENT-18: instructions use completed transcripts as the source of truth for names and replies.
- AGENT-19: folder hints resolve to the local checkout; “batch change list” refers to the plugin/viewer.
- AGENT-27: `/dev/null` and `2>&1` redirects are safe, read-only `find` and simple inline print commands run without approval, and destructive `find`/inline interpreter commands still require approval. Dev instructions route reads and edits through `file_edit`.
- OBS-25: follow-on microphone transcripts attach to the active task run.

Targeted tests: `python3 -m unittest tests.test_typed_turn_api tests.test_live_transcripts tests.test_dev_tools tests.test_session_hub_startup tests.test_reconnect_comet` — 138 tests passed.
