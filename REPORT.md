# STATUS accuracy report
- Runtime files: `bin/bcl`; `live_state.py`, `manage_findings.py`, `render.py`, `serve.py`.
- Tests: `test_bcl.py`, `test_manage_findings.py`, `test_render.py`, `test_serve.py`.
- Docs: `SKILL.md`; `collect.md`, `report-rollover.md`, `viewer.md`; `CHANGELOG.md`.
- AUTO-03 follow-up: wait up to 1s in 50ms polls for an owned outdated viewer to release its port.
- Requested unittest discovery: 96 passed, including the running outdated-viewer release test.
- Repo gate: `python3 scripts/check.py --fast --jobs 1` passed (18 sequential test processes).
- Risk: provider retargeting used mocked CLIs; no live merge/API or installed-viewer acceptance was run.
- Pre-existing local-only-work hygiene FAIL remains; live 110.json, PID 28743, ports 53366/8765, and Samantha log were untouched.
