---
name: macos-cua
description: Use when operating or validating native macOS apps through CUAService or its shared Dynamic Island.
allowed-tools: Bash, Read
---

# macos-cua

CUAService is the only visible-cursor owner; its MCP exposes `state` and `act` for computer use.

## Critical path

Act-first when labels or outcomes are known: one batched `act` per app, then verify the result. Compact `state` is for discovery or after a miss. Reuse the returned delta. Encode friction in the app-agnostic `scripts/fast_path.py` linter/grader so later runs fail on the old trace. Handle in-place label changes such as Clear → All Clear from the delta.

Failed Samantha starts remain visible in the menu and Dynamic Island with a short category and detail. A new attempt clears the failure; a successful start clears it as well.

## Hard bans

Never use browser DOM or terminal-only commands for native-app actions because they bypass the visible CUA path. Never stop another user's active voice session because it may belong to their work.

## Load map

| Step | When | Reference | Script |
| --- | --- | --- | --- |
| 1 | When operating an app, recovering a miss, or checking service health | `references/fast-workflow.md`, `references/actions.md`, `references/observe-feedback.md`, `references/troubleshooting.md` | `scripts/fast_path.py`, `scripts/bench_jev_act.py`, `scripts/bench_load.py`, `scripts/bench_speed.py`, `scripts/bench_native_sdk.py` |
| 2 | When changing MCP/backend behavior, assessing parity, handling multiple displays or special surfaces, or running the hardening gate | `references/architecture.md`, `references/cua-driver-mcp.md`, `references/computer-parity.md`, `references/displays.md`, `references/special-surfaces.md`, `references/entry-contract.json` | `scripts/bench_mcp_runtime.py`, `scripts/gate_cleanup.py` |
| 3 | When reporting task status, requesting approval, saving a credential, using generic MCP activity hooks, or verifying Samantha | `references/dynamic-island.md`, `references/lifecycle.md`, `references/island-polish-bar.md`, `references/safety.md`, `../../docs/island-plugin-hooks.md`, `../../runtime/voice-cua/verify/SKILL.md` | |
| 4 | When a site/app choice may use a saved preference or the user states a durable preference | `references/context-ledger.md` | |

WhatsApp send/attach → `$whatsapp`; this skill does not send messages. Run `./scripts/validate.sh` from this directory after edits. Install and release instructions belong to the [plugin guide](../../AGENTS.md).
