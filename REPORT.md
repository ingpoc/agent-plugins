# HYG-01
- Files: `scripts/release_agent_computer_use.py`, `scripts/test_release_agent_computer_use.py`, `plugins/agent-computer-use/skills/macos-cua/tests/test_gate_hygiene.py`, `REPORT.md`.
- The release gate now keeps hygiene findings only when they reference the checkout being released.
- Tests: strict `test_gate_hygiene.py` discovery 9/9 passed; `scripts/test_release_agent_computer_use.py` 20/20 passed.
- `scripts/check.py --fast --jobs 2` exited 1 after 133 test processes; two Swift service assertions failed.
- Swift failures: `IslandGateRedesignTests.testFixtureStates` and `IslandGateStateTests.testEverySnapshotFixtureAvoidsKeychainAndNetwork`.
- The fast run also caught a hygiene-test assertion later corrected; strict discovery now passes.
- Plugin version unchanged.
- Leftover risk: machine-wide hygiene still reports pre-existing local-only work in multiple clones; the Swift failures remain outside this change.
