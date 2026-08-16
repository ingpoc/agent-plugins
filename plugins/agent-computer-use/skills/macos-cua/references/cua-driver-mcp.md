# cua-driver and MCP contract

Sole owner for driver-field and MCP-catalog decisions. Load this before
changing `get_window_state` params, `call_driver` payloads, `compact_mcp.py`,
`mcp.json`, or adding tools. Do not copy these facts into other references.

Last verified: 2026-08-16 against **installed `cua-driver 0.19.2`**
(`dump-docs --type json`: 54 MCP tools) and MCP spec **2026-07-28**.
Hosted cua.ai pages already describe **0.20.0**. Installed `click` still
has no `target`. Do not code from hosted examples.

## What to check (in this order)

1. Installed schema — this beats hosted prose when they disagree:

   ```bash
   cua-driver --version
   cua-driver dump-docs --type json
   ```

   Read `inputSchema.properties`, `required`, and `additionalProperties`.
   `0.19.2` action tools are `additionalProperties: false`. An extra field
   is a hard error, not a no-op.

2. Hosted driver docs (may describe unreleased fields):
   [docs index](https://cua.ai/docs/llms.txt),
   [action policy](https://cua.ai/docs/reference/cua-driver/action-selection-policy),
   [MCP tool notes](https://cua.ai/docs/reference/cua-driver/mcp-tool-notes),
   [capture/delivery](https://cua.ai/docs/concepts/capture-and-delivery-modalities).

3. MCP spec (facade only):
   [2026-07-28 tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
   — `structuredContent` + text, `isError`, modern `server/discover`
   `ttlMs`/`cacheScope`. Cursor still needs legacy `initialize` + `ping`.

4. OpenAI computer-use is **not** this plugin's owner. Batch-then-one-screenshot
   is already `macos-cua.py run`. Do not add a second observe/act catalog to
   match Sky/`node_repl`.

## Practices already encoded

| Practice | Where | Do not replace with |
| --- | --- | --- |
| 5-tool facade | `compact_mcp.py`, source `mcp.json` | Raw `cua-driver mcp` (~54 tools) |
| `include_screenshot: false` on re-index | MCP `state`, CLI `--no-screenshot` | Defaulting every observe to pixels |
| AX + background first | `act`, `run`, `click-label-pointer` | Front-then-act as the normal path |
| `effect` / `escalation` (`px`\|`foreground`\|`page`) | `plan_contract.compact_step`, MCP `structuredContent` | Trusting dispatch `ok` |
| `suspected_noop` is not acceptance | `plan_contract.result_accepted` | Treating `ok: true` as landed |
| `degraded` tree = incomplete | `runtime_snapshot.snapshot`, MCP `verify` | Acting by index on an empty tree |
| Foreground only after background miss | `app_state` / Finder probes | `bring_to_front` + sleep as preflight |
| Sidebar names on the row/cell | `_attach_static_child_text` + native `parent_index` | Assuming “Downloads” missing means front |
| Active sheet/popover/dialog is the snapshot root | `choose_walk_roots` + one-window `_unique_ax_windows` | BFS of every `AXWindows` sibling on each observe |
| Open app-level `AXMenu` first, then one window | `choose_walk_roots` + `_open_ax_menus` | Passing `AXMenuBar` as a walk root (Apple-menu flood) |
| Snapshot reuse after accepted click | `runtime_plan` `state_reuses`; refresh once on label miss | Per-step `expect` or a fresh observe after every click |
| Glide uses popover/sheet/dialog/menu/largest containing frame | `_glide_container_frame` | AXWindow-only glide (misses sheets) |
| Labeled glide is window-local; no Python Quartz on the click path | `glide_operator_to_element` omits `cursor_screen_x/y` when an AXWindow exists; operator remaps normalized coords | Sending stale global screen points, or Quartz-reading on every AX click |
| Background `key` retries once on `off_space_or_ax_unresolved` or `escalation.recommended=foreground` | `runtime_plan` / `key` | Front-then-key as the normal path |
| Open-sheet filenames on the row | same attach, now includes `AXTextField` | System Events picker inside this plugin |
| Empty Catalyst type is not accepted | `typed_text_is_proven` / `ax_incomplete_value` | Trusting `typed_path` / `ok` |
| Session omitted on observe | `get_window_state` / MCP `state` | Injecting `MACOS_CUA_SESSION` on every RPC |
| Pixel/desktop clicks stay sessionless | `runtime_pointer_actions` | Passing the glide session (mints cyan `auto-*`) |
| Dest MCP uses absolute launcher, `cwd` `./` | `install_harness.py cursor-plugin` | Writing `~/.cursor/mcp.json` or dest `cwd` as an absolute path |
| TCC principal is signed `Cua Driver.app` (`com.trycua.driver`) | LaunchAgent + `check_permissions`. MCP `bin/agent-computer-use-mcp` is a Cursor-child Python facade only | Granting AX/Screen Recording to Cursor/Terminal or wrapping the facade as a second signed app |
| 5-tool facade, no default screenshots, no `list_apps` | Community-validated; MCP `state` is `--compact --no-screenshot` | Growing toward 54/56 tools or always-on pixels |
| Equal-weight live suite | `references/entry-contract.json` | Loosening a budget to force green |

## Do not rediscover

| Attempt | Why it was rejected | Revisit only when |
| --- | --- | --- |
| Register raw `cua-driver` in `~/.cursor/mcp.json` | Hosted “connect your agent” does this. Two catalogs (54 + 5) waste tokens and invite `list_apps` / `health_report`. Cursor then shows a standalone MCP, not the plugin card. | Never, unless the 5-tool facade is deleted |
| `list_apps` / `health_report` as preflight | Slow, huge, and not required to resolve by name/bundle | Driver schema removes name/bundle launch |
| Always `bring_to_front` + sleep before Finder | Docs forbid front as a normal step. Folder dropped from 8–14s to ~1s once native AX attached sidebar text | Escalation is `foreground` or the tree is `degraded` |
| Treat “Downloads not clickable” as a front/sleep bug | Live tree already had `AXStaticText` value `Downloads`. `find_clickable_index` only matches `CLICK_ROLES`. Fix is ancestor attach, not another snapshot | `_attach_static_child_text` regresses |
| Send `target: {kind:"window", pid, window_id}` | Hosted MCP notes describe it. **0.19.2 `click` has no `target`** and `additionalProperties: false` | Installed `click` properties include `target` |
| Stop sending `capture_mode` | Hosted docs: deprecated and ignored. Schema still lists it; tests assert `ax`/`vision`. Removing it is noise, not a win | Field disappears from `dump-docs` |
| Inject `session` on every `call_driver` | Schema: omit = cursor-less. Observe must stay cursor-less. Pixel/desktop paths must stay sessionless or a second cyan cursor appears | Docs make session sticky *and* observe-safe |
| Add `outputSchema` on all five MCP tools | Spec-optional. `structuredContent` is the 2026-07-28 win. Schemas would churn with every compact field | A client starts rejecting unschematized structured results |
| Widen MCP `state`/`act` timeouts | First All Clear ~32s was a missing 1.5s AX timeout, not a short MCP budget | A new live probe exceeds 30s after AX timeouts are proven |
| Silent Quartz / `right-click-point` fallback | Watched accuracy. `MACOS_CUA_PIXEL_CLICK=1` is required | User explicitly authorizes pixel fallback |
| Trust `ok` / `typed_path` on Catalyst | Native type now returns `ax_incomplete_value` when AX is empty. Voice→Send or a screenshot remains proof | App exposes a verified AX value |
| Walk every `AXWindows` as the default observe | Duplicate focused/main/all windows burned `--max` before the sheet. Sibling hunt after `window_id` made Calculator 16s | `choose_walk_roots` regresses |
| Default `settle_ms` 150 after every click | Next snapshot already waits; 4 clicks wasted 600ms | A specific plan needs inter-click delay |
| Treat `AXMenu` as an active surface | Closed menus exist in the tree; rooting there hides the document | App-level open context menus may be walked first. Closed menus stay compact-filtered |
| Walk `AXMenuBar` as a BFS root | FAILED 2026-08-16: flooded `--max` with Apple menu; Calculator/Finder/WhatsApp all failed. `choose_walk_roots(..., menubar=)` was deleted | Never. File menu is not a suite requirement. If a later case needs File, wrap installed `invoke_menu` (`path`+`pid`+`window_id`, fail-closed, no pixels) internally — do not BFS Apple menu and do not add a 6th MCP tool |
| Chrome via macos-cua | Hermes coexistence blocks; browser MCP owns Chrome | A non-Chrome native PID is the only concurrent exception |
| Require a second display in session preflight | Single-monitor is a supported runtime. The elon secondary-display check is lab-only | Never copy the lab gate into `start_session` / `workflow.py preflight` as a hard fail |
| Sixth MCP tool for displays | Topology is CLI `displays` and optional `start_session preflight:true` | A client cannot read CLI/`preflight` and still needs a 6th tool |
| Keep a change that made Calculator 17s / attach `display_packet` to every `state` | Official suite degraded (Calculator 5.23s → 17.21s). Extra NSScreen/CG/Quartz on every compact observe and labeled AX glide | Revisit only if a **warm** official suite is ≤ last green: best 5.23 / 0.73 / 5.79 / 2.40; post-revert 5.83 / 0.99 / 7.53 / 2.58. Topology stays on CLI `displays` and optional `start_session preflight:true` |
| Pin labeled glides with `cursor_screen_x/y` | Operator already parents normalized coords to live Quartz bounds. Absolute screen points stale-click after a window move | Point/desktop clicks may still send screen coords and fail closed if geometry changed |
| Poll `list_windows` every 50ms to follow a drag | Operator already polls its state file; follow is one `CGWindow` bounds read by known `window_id` | Never |
| `just run cua-driver mcp` from the IDE / grant TCC to Cursor | Grants key to the signed driver bundle, not argv0. The facade must stay a thin Cursor child | Cua Driver.app is no longer the AX/Screen Recording principal |
| Union-frame overlay / always-on screenshot | Token waste; MCP `state` is AX-only | A client cannot act without pixels |
| Global HID click as the happy path | Watched path is glide then AX. Pixels are window-local and this-snapshot scale | `MACOS_CUA_PIXEL_CLICK=1` plus a live-frame gate |
| Dock layer-20 / `kCGWindowSharingState=0` hit-test | Invisible Dock false-blocks clicks. Autohide is a workaround, not a product requirement | Never add a CG Z-order gate |
| Sequoia monthly Screen Recording SCK-probe in cheap preflight | Re-prompts the user. Preflight uses `check_permissions` only | A new 1-call probe is proven not to re-prompt |
| Hide-other-apps / dual-display lock (Anthropic CLI posture) | Codex/cua prove same-display works. Single-monitor is valid | Lab secondary-display gate only |
| Clipboard-paste typing from background | Community workaround; steals the user pasteboard | Never. Type stays AX / `type-text` |
| Lift raw `invoke_menu` into the 5-tool facade | Catalog growth; File menu is not an official-suite gap | A suite case needs File *and* wrapping it inside existing `act`/`run` is proven |
| Recreate `~/.agents/skills/macos-cua` as a copy | It is a symlink to this plugin skill | The symlink is gone and install is supposed to restore it |
| Lift WhatsApp Open-sheet osascript into this plugin | Attach completion stays `$whatsapp` `attach-file` | A generic Open-panel owner is proven on more than WhatsApp |
| Loosen suite budgets after one slow WhatsApp/pointer run | 18s WhatsApp and 47s pointer were cold/Catalyst flakes; reruns passed | A warm rerun still misses the budget |
| Cursor dest `cwd` as an absolute dest path | Host then ignored dest `mcp.json` | Cursor spawn contract changes |
| Plugin-relative `./bin` without dest rewrite | Cursor spawns `{workspace}/bin/…` (ENOENT) | Cursor resolves plugin `cwd` against the plugin root |

## What to update when a feature actually lands

Confirm on **this machine’s** `dump-docs` first.

| Installed schema change | Update these owners |
| --- | --- |
| `click`/`type_text` gain `target` | `native_input.py` + driver tests. Keep flat `pid`/`window_id` until the schema drops them |
| `capture_mode` removed | `runtime_snapshot.snapshot`; `tests/test_macos_cua.py` capture_mode assertions; this table |
| `element_token` on native snapshots | Driver fallbacks in `native_input.py` (already forwards a token when present). Do not invent tokens for native-only trees |
| Session becomes sticky or required | `call_driver` default inject, **except** pixel/desktop and observe |
| New `effect` / `escalation` values | `plan_contract.result_accepted`, `compact_step`, MCP `INSTRUCTIONS` |
| MCP spec adds a required tools/call field | `compact_mcp.call_tool` + `tests/test_compact_mcp.py` |
| Driver newer than 0.19.2 | Re-run the check list; rewrite the “last verified” line; do not assume hosted examples work |

## Facade shape (do not grow)

`start_session`, `state`, `act`, `verify`, `end_session`.

- `state`: always `--compact --no-screenshot --max` (default 80). Optional `query`/`diff`.
- `act`: label/index/plan. Glide then AX. Compact result must lift `effect` and `escalation`.
- `verify`: independent compact re-read. `ok` is false when `degraded` or expect misses.
- Modern `tools/call` returns `content[].text` **and** `structuredContent` (same payload).
- `bin/cua-driver-mcp` stays diagnostic-only.

## Cursor host (workaround, not a Vehicle fix)

`install_harness.py cursor-plugin` rsyncs
`~/.cursor/plugins/local/agent-computer-use`, rewrites dest `command` to the
absolute launcher, keeps dest `cwd` as `"./"`, and removes
`agent-computer-use` from `~/.cursor/mcp.json`. Do not “fix” this by adding
files to an application workspace.
