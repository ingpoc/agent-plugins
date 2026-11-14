# ACU 0.2.143 group B — Samantha tools

- **AGENT-17:** `app_not_running` is terminal for the action fingerprint. The backend returns a direct not-running result for RPC `-32001`; retry responses preserve the terminal error and instruct the model not to retry with a bundle ID.
- **AGENT-22:** After dispatched UI actions, expect verification now polls fresh, full AX observations for up to two seconds (up to 1,000 elements) so Terminal and TextEdit updates can settle. Failed follow-up observations retain the last usable state. Retry-cap guidance tells the model to change strategy.
- **AGENT-23:** Tool-result event fields are merged into one dictionary before calling `log_event`, and result logging exceptions are contained so they cannot turn a completed typing action into a tool failure.

Focused regression tests cover terminal app failures, resilient typed-text logging, and delayed Terminal output. Added test package markers so the prescribed unittest module paths resolve locally.

Validation: the requested targeted command passed (55 voice-CUA tests; 61 compact-MCP tests). No install, live verification, version bump, merge, rebase, or full repository check was run.
