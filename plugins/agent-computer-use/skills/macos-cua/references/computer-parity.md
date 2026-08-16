# Bundled Computer Use parity ledger

Last contract comparison: 2026-08-16. Reference owner:
`~/.codex/plugins/cache/openai-bundled/computer-use/1.0.1000717`. Load this
ledger only for backend selection, regression review, or a parity claim.

2026-08-16 research (steal mechanisms, not catalog):

| Mechanism | Verdict | Why |
| --- | --- | --- |
| Name-first resolve; `list_apps` only if the app is unknown | Already ours; keep | Codex SKILL says the same; preflight `list_apps` is rejected |
| AX text default; screenshot on demand; diff after first observe | Already ours (`--compact` / `--diff`) | Matches Sky `get_app_state` + `disableDiff` |
| Batch actions, then one state | Already ours (`run`) | Do not add a second observe/act catalog |
| Disable the standalone Computer Use MCP when a facade exists | Already ours (5-tool facade) | Do not load raw 54-tool `cua-driver mcp` beside it |
| Sky / `node_repl` ~10 methods | Rejected | Non-goal; copy neither catalog nor architecture |
| Codex ~1s automatic post-action wait | Rejected | We already rejected default `settle_ms` 150; next snapshot waits |
| Screenshot-first / coordinate fallback as an equal path | Rejected | Watched path is glide then AX; pixel needs `MACOS_CUA_PIXEL_CLICK=1` |
| Chrome via Computer Use | Rejected | Browser MCP owns Chrome; Hermes coexistence blocks ACU |

| Capability | Bundled Computer Use | macos-cua | Status |
| --- | --- | --- | --- |
| App discovery and launch | List apps; name/path/bundle id; transparent launch | Same, plus PID/window identity cache and stale-window rejection | Above |
| Observe | AX text plus screenshot; full or diff-oriented use | Compact indexed AX text by default; optional structured elements/tokens; screenshot/raw proof; query/max controls | Parity |
| Click | AX index is background-safe; coordinates use CGEvent | AX label/index/token is background-safe; point paths are explicit and user-interruptive | Parity |
| Drag and double click | Drag; click count | Direct drag and double-click commands, foreground fallback | Parity |
| Keyboard and typing | Type text and xdotool-style keys; warns that newline may submit | Labeled typing visibly focuses before dispatch; newline fails closed by default; normalized key combos and background/foreground delivery | Above |
| Set/select text | Set value; contextual substring/cursor selection | Same, with native selected-range readback | Above |
| Scroll | Element-scoped directional pages | Element or point-scoped direction, line/page amount | Above |
| Secondary AX actions | Invoke an action exposed by fresh state | Native advertised press/show menu/pick/confirm/cancel/open/increment/decrement/raise/zoom | Parity |
| State/action freshness | Re-observe after actions; indices are fresh-state scoped | Same; asserted plans safely reuse a successful postcondition snapshot, otherwise re-snapshot | Parity |
| Automatic outcome wait | Runtime waits after actions | Native foreground acknowledgement plus polling postconditions with timeout | Parity |
| Multi-step proof | Agent-managed loop | One-process asserted plan, failure capture, final assertions | Above |
| Token efficiency | AX diffs by default; screenshots on demand | Compact packets, `--query`/`--max`, `--diff` after first observe, AX-only mode | Parity after `--diff` |
| MCP surface | Sky client + persistent `node_repl` (~10 methods). Standalone `[mcp_servers.computer-use]` is `enabled=false` so tools are not double-loaded | Packaged `./bin/agent-computer-use-mcp` (cwd `.`): `start_session`, `state`, `act`, `verify`, `end_session`. Raw `cua-driver-mcp` is diagnostic-only. CLI `state`/`run` is the default AX loop | Parity on catalog size; CLI batches still cheaper |
| Visible agent pointer | Host-integrated software cursor; not evidence of a second system pointer | Labeled Hermes overlay is default for labeled plans and must acknowledge field focus before typing; coordinate CGEvents still use the one system pointer | Honest boundary |
| PiP/operator state | Host PiP and Computer Use indicator | Signed all-Spaces PiP, exact target ring, app/harness/status label, Hide/Refresh/End | Above |
| Menu bar | Host-owned control indication | Cursor icon plus controlled app; detailed menu and session actions | Above |
| Secondary displays | Runtime follows target | Explicit display discovery, overlay alignment, local/global conversion, pinned display option | Above |
| Harness portability | OpenAI host runtime | Shell/driver contract plus shared links for Codex, Cursor, and other harnesses | Above |
| Safety | Action-time confirmation policy | Same action-time boundary in `references/safety.md` | Parity |

## Design sources

- [OpenAI computer-use guidance](https://developers.openai.com/api/docs/guides/tools-computer-use)
  batches coherent actions, then returns fresh computer state; this skill uses
  asserted plans and fresh/postcondition snapshots for the same bounded loop.
- [Apple NSRunningApplication](https://developer.apple.com/documentation/appkit/nsrunningapplication)
  exposes time-varying activation state; foreground readiness is acknowledged
  from live AppKit/AX state instead of a fixed sleep.
- [cua-driver validation](https://cua.ai/docs/concepts/how-cua-driver-is-validated)
  distinguishes protocol acceptance from application-owned end-to-end proof;
  this skill keeps both static contract tests and live app graders.

## Live evidence

- Bundled Computer Use read Calculator and the `macos-cua Operator` panel live;
  the latter exposed its floating window and Hide, Refresh, and End controls.
- `tests/test_live_computer_parity.py` proved Calculator stays on the Dell
  secondary display, the labeled Hermes pointer and proof image, compact state,
  signed operator state, text value/selection/type/key behavior, advertised
  secondary action, visible menu disclosure, scroll, double click, drag,
  coordinate click, right click, and clean closeout.
- `tests/test_live_pointer_isolation.py` proves the overlay and AX dispatch
  path. It does not prove coordinate isolation. The driver contract says
  `element_index` is AX/no cursor move/no focus steal, while x/y is CGEvent.
  `--preserve-pointer` only restores position after that interruptive interval.
- `scripts/validate-macos-cua.py --live --progress` additionally owns Finder
  state/scroll, operator menu Hide/Show, Cursor harness state, and launchd
  self-healing checks.

## Aim

Beat bundled Computer Use on watched speed, accuracy, visible cursor,
robustness, and tokens — not by copying Sky/`node_repl` or exposing raw
`cua-driver mcp`. Driver-field and MCP-catalog decisions live only in
[`cua-driver-mcp.md`](cua-driver-mcp.md). Current losses: Cursor host
resolves plugin `./bin` against the workspace (dest rewrite is the owner
workaround); Catalyst composers still need Voice→Send or screenshot
proof. Current wins: 5-tool MCP facade, asserted `run`, `--query`/`--diff`,
equal-weight live suite, portable CLI.

## Claim boundary

Parity is valid only when static validation, the live parity test, the opt-in
live validator, and current visual capture all pass against the installed
signed operator and live cua-driver. Source inspection or compilation alone is
not evidence of Computer Use parity.
