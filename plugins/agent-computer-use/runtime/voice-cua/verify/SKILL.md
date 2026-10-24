---
name: verify-samantha
description: Verify Samantha end to end through installed GPT-Live text or voice prompts, local tools, Dynamic Island and Kindle findings. Use when testing Samantha, investigating a reported failure, or validating a voice/browser/credential/Island change.
---

# verify-samantha

Use [the test-run workflow](references/test-run.md) for preflight, prompts, proof and cleanup. The [feature map](features/README.md) routes deeper checks. Keep one driver per live surface.

## Invariants

- Test the installed candidate: source, signed app and running service must share a runtime identity. Run `python3 plugins/agent-computer-use/skills/macos-cua/scripts/runtime_contract.py` from the collection root before a live run and after any mismatch.
- Compare project findings before and after through the developer-observability MCP when configured. Use the Kindle project's owner doctor or skill for source details when available. Recommendations and old counts do not prove a current cause or fix; if no baseline source is available, say so.
- Live API tests require operator authorization. An explicit request to prompt Samantha authorizes a bounded prompt test; it does not authorize unrelated logins, messages, purchases or credential changes.
- Text prompts, direct tool calls, audio injection and real microphone/speaker tests prove different paths. Label the path actually exercised.
- HTTP success, a spoken claim and tool dispatch are insufficient: require the requested app/site outcome and cleanup. Inspect uncertain dispatched actions before retrying.
- Credentials use native secure Island entry and Keychain. Never put values in prompts, transcripts, tool arguments, screenshots or logs. Use dummy data for automated form checks; stop if the user intervenes, indices change or a secret could be present.
- Keep unit-test activity in temporary logs. Real end-to-end probe failures remain identifiable by session ID and prompt; do not inject fabricated failures into the live feed.
- Report unresolved failures and untested paths explicitly. Never mark sustained voice reliability fixed from a short clean test.
