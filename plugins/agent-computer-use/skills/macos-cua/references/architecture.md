# macos-cua — Architecture boundary

Load when changing MCP/CUAService schema or understanding the two-tool surface. Keep [SKILL.md](../SKILL.md) thin.

MCP follows stable `2026-07-28`: stateless requests, per-request `_meta`, `server/discover`, structured results, and legacy fallback only at the adapter. Realtime uses the same two operations through local function calling; it does not create a second native engine or expose the Mac as a remote MCP server.

Keep the model surface at `state` + `act`. New macOS capability is an `act` step, not another model tool. CUAService serializes desktop requests and owns one native `execute_plan` RPC: before-state → all same-app steps → settle → after-state. The compact adapter evaluates the canonical structured postcondition against that native evidence and is the only completion gate.

Reuse the native operations CUAService already has: scroll, text selection, secondary AX actions, and drag. File, directory, URL, and application opening belongs in CUAService as an `act` step backed by `NSWorkspace` plus `FileManager` validation—not Finder keystroke choreography. Use AX first and one-shot ScreenCaptureKit vision only after an AX miss.

`compact_mcp.py` owns the canonical MCP input/output schema. Realtime schemas must be derived from it or parity-tested. Use explicit operation variants and structured `expect` predicates; never an unconstrained action string. Long or deferred work may use the MCP tasks extension only after ordinary bounded plans prove insufficient.
