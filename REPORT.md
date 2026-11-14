ISLAND-03 — Fast-mode Island label
Root cause: `turn_context.payload.thread_settings.service_tier` was `priority`; the parser read only `payload.service_tier`, and SQLite had no tier.
Changed: `IslandCodexThreads.swift` reads the nested field and keeps flat-field compatibility.
Tests: focused suite 13/13 passed; full CUAService 608 run, 17 skipped, 2 failed on unlocked fixture assertions in `IslandGateRedesignTests` and `IslandGateStateTests`.
Repo gate: `scripts/check.py --fast` stopped before tests because package policy requires a version bump, forbidden by this task.
Risk: installed/live UI proof remains unverified; the unrelated fixture failures remain.
Capture: after an approved build/install, run equivalent `acu_exec` jobs with `service_tier=priority` and default; while each Island row is visible, save `screencapture -x /tmp/island-fast.png` and `/tmp/island-default.png`.
