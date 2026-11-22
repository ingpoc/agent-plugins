---
name: verify-samantha
description: Use when testing Samantha, investigating a reported failure, or validating a voice, browser, credential, Island, or Kindle change.
---

# verify-samantha
Editing this skill? Read [principles.md](principles.md) first so edits pass it.
Keep one driver per live surface.
Never put credentials in prompts, transcripts, tool args, screenshots, or logs because those records may persist. Live API tests need operator authorization because they can use external services.

| Step | Trigger | Reference | Script |
| --- | --- | --- | --- |
| 1, 3, 4 | When testing Samantha, investigating a failure, verifying a candidate, running a dummy form check, or closing out a live run | `references/test-run.md` | `python3 plugins/agent-computer-use/skills/macos-cua/scripts/runtime_contract.py` |
| 2 | When checking voice, browser, credential, or Island behavior | `features/README.md` | |
