# HYG-01
- Files: `scripts/release_agent_computer_use.py`, `scripts/test_release_agent_computer_use.py`, `plugins/agent-computer-use/skills/macos-cua/tests/test_gate_hygiene.py`, `REPORT.md`.
- Clone-scoped findings resolve `git-common-dir`; placement findings stay scoped to the release worktree.
- The regression test creates a bare origin, two real clones, local-only commits, a dirty file, a linked worktree, and checks real hygiene scanner output.
- Tests: `test_gate_hygiene.py` 9/9 passed; `scripts/test_release_agent_computer_use.py` 20/20 passed.
- The fast gate was not rerun; its earlier run found two unrelated Swift fixture failures and a hygiene-test assertion later corrected and retested.
- Plugin version unchanged.
- Leftover risk: machine-wide hygiene still reports pre-existing local-only work across clones; Swift fixture failures remain outside this change.
