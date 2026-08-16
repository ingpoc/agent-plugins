# Troubleshooting

Run `workflow.py preflight` to initialize and stabilize shared infrastructure;
inspect the target only with `state <app>` afterward.

After live probes, quit the test app (Calculator, Dictionary, Stickies,
Calendar, Clock, Weather, Stocks, Maps, Reminders, Chess, Tips, System
Settings, Preview of temp PDFs, extra TextEdit, Font Book, Freeform).
Do not quit Cursor, WhatsApp, or Chrome/Safari/Comet when they look like
the user's real session. Prefer `osascript` quit; `cmd+w` then quit for
Preview/TextEdit fixtures. Never force-kill Cursor.

## Eval / revert

Experiment is allowed. After a plugin change, warm
`python3 scripts/run_benchmarks.py`. Keep the change only if every official
row improves or holds vs the last warm green (duration / max-step / bytes).
If any official row is slower, revert. Do not loosen `budget_seconds` to
force green. A cold run after an operator rebuild is not a keep/revert
signal (37s lesson). Re-warm, then compare. Last warm green is the bar.

## Optimization learnings — do not rediscover

Verdict is measured, not argued. Warm numbers only.

| Idea | Verdict | Evidence |
| --- | --- | --- |
| Rewrite runtime in Rust/Go for speed | reject | All Python = ~55ms of a ~1000ms call (facade 24ms, 60-el AX snap 31-38ms, json 0.1ms). Dominant cost is `cua-driver` client process launch + codesign check, already compiled + third-party. Ceiling <3%. New binary = new TCC subject, loses `com.trycua.driver` grant |
| Cache/pool Python interpreter to kill spawn cost | reject | Premise false. Real interpreter 0.03s; the 0.46s was a pyenv shim in an interactive shell only. `compact_mcp.py` spawns via `sys.executable`, never pays it |
| Transport for driver tool calls | socket-only; no CLI call path | `call_driver` speaks the daemon socket exclusively. Wire shape is `{"method":"call","name":T,"args":{...}}` — the key is **`args`**, not `arguments`. Translation is `envelope["result"]["structuredContent"]`. When `result.isError` is set with no `structuredContent`, return `content[0].text` as `error`, never a generic wrapper. On socket failure: reconnect once, then restart the daemon once and retry, then error. CLI survives for daemon lifecycle (`launchctl kickstart`) only, because a down daemon has no socket to answer on |
| Socket args under key `arguments` | reject — silently breaks every argument-taking tool | The daemon reads `args`. Under `arguments` it receives an **empty** argument set and replies `{"ok":true,"result":{"isError":true,"content":[{"text":"Missing required integer field: pid"}]}}` in 0-86ms. Argument-free tools still pass, so the suite looks fast and healthy. Symptom: `perform_action: element not found`, dead `press_key`, and rows whose repeat 1 passes then 2-5 fail on leftover app state. Pin the wire key against the **live daemon** (`tests/test_macos_cua.py::test_live_daemon_parses_our_argument_envelope`) |
| Persistent UNIX socket as the big speed win | reject the claim, keep the transport on other grounds | The "socket 0-83ms vs CLI 5605/9689/6226ms" figure compared a **working CLI call to a broken socket no-op**. With `args` correct, `press_key` costs seconds on both transports: the cost is the driver's key delivery plus its bounded AX read-back, not the transport. Socket's real benefit is avoiding ~170ms client process launch per call, so it matters only in proportion to driver calls per row (calculator 1-2, whatsapp 3-4). Never size a transport from a tool whose payload you have not asserted |
| Expectation poll 200ms → 50ms | reject | Calculator max step got worse (276ms → 515ms) and reliability dropped to 8 from duration spread. Extra polls also take extra AX snapshots. Leave 200ms until the wait is event-driven |
| Calculator independent 64 via `_native_ax_snapshot` instead of `app_state query=AXStaticText` | reject | Trust gates went to 0 on a 5-repeat. Native tree missed the display value that the queried app_state catch. Keep the independent query |
| Folder always screenshots after AX already found Downloads | ADOPT | pass_signal is actionable ancestor, not pixels. Dropped second app_state+screenshot: ~1.2s/2 snaps → 0.35s/1 snap. Folder overall 7.8 → 10.0 |
| WhatsApp Escape-before-observe | ADOPT | Snapshot first; press only if heading open; teardown Escape without a second verify snap. Driver 3→2, AX 5→4 |
| Per-module telemetry counters | reject | The facade loads each runtime module with `importlib` and overwrites its own `sys.modules` entry, so a second facade load in one process gets a second `_COUNTS`. `tests/test_live_computer_parity.py` loads its own facade, so every `_native_ax_snapshot` and driver call inside `fresh_text_area` was invisible to the harness: textedit reported `ax_snapshots: 2` while its probe reads state 4+ times. Counters now hang off `builtins` under `_macos_cua_telemetry_counts`, so all facade loads share one store. Prove sharing with two `importlib` loads before trusting any counter-derived rating |
| Rating an axis on a counter that cannot see the work | reject as method | Same family as the argument-free parity check and hardcoded `asserted_batch`: the metric is structurally unable to observe the cost it claims to score, so it always flatters. Before adopting an axis, confirm every path that does the work increments the counter |
| Parity-checking a transport with argument-free tools | reject as method | `check_permissions`, `get_cursor_position`, `get_agent_cursor_state`, `get_screen_size`, `get_session_state` all take **no arguments**, so all five pass with a dropped argument payload. Prove a transport with a tool that requires arguments and assert a field that only real arguments can produce |
| Returning the socket `result` envelope as the driver payload | reject | Socket `result` is `{"content":[...],"structuredContent":{...}}` but `cua-driver call` stdout is the **structuredContent only** (`{"x":1763,"y":1400}`). Returning `result` gives callers no `accepted`/`effect`/`code`. Translate to `result["structuredContent"]` |
| Fake-vs-fake shape parity test | reject as method | A test comparing a fake CLI stdout to a fake socket envelope asserts the author's assumption, not reality. It happened twice: a unit test pinned `"arguments"` as the wire key and stayed green while every real argument-taking call failed. Pin real recorded payloads, and pin wire shape against the live daemon |
| `MACOS_CUA_CACHE_DIR` to isolate a benchmark run | reject | It relocates the operator overlay's cursor image and screenshots too, so a fresh dir has no pointer asset, the overlay cannot render, every glide burns a ~3.4s ack timeout and fail-closed refuses the click. Symptom is accuracy 0 / visibility 0 on 3 rows with `max_step_ms` pinned ~3400ms and 18-20 AX snapshots. Use the default cache dir and copy `benchmarks-latest.json` between runs |
| Attributing hot-path cost from one row | reject as method | Cost me a wrong call. Calculator makes 1-2 cheap driver calls, so it showed only 3% driver time and I generalized "transport does not matter". The keyboard rows were 60-100x worse. Attribute **per tool**, on every row, before accepting or rejecting a transport |
| Isolated-primitive latency as proof | reject as method | `get_cursor_position` CLI 169.8ms suggested a uniform ~170ms CLI tax. Real spread is 170ms to 9700ms depending on the tool. Never size a transport change from one cheap tool |
| Strip `_native_*` from non-compact CLI `state` | candidate | 60-el Finder tree: 12241 of 22468 bytes (54.5%) are `_native_element`/`_native_services`. Unusable by an agent, and alone exceeds the 12000 `bytes_budget`. Produced in `runtime_accessibility.py`; consumed in-process by `native_input.py` — strip at serialization only, never in the dict |
| Attach `display_packet` to every `state` | reject | Calculator row 5s → 17s. See [`cua-driver-mcp.md`](cua-driver-mcp.md). Topology stays on CLI `displays` + optional `start_session preflight:true` |
| Batch RPCs via a `cua-driver` CLI batch flag | reject | No batch mode in 0.19.2 CLI (`mcp, list-tools, describe, call, serve, ...`). Persistent socket is the substitute |

### Measured primitives — warm, do not re-measure

| Primitive | Cost |
| --- | --- |
| Calculator launch → AX-ready | 1.010s |
| TextEdit `open -na` → `AXTextArea` ready | 0.869s |
| Native AX press | 5.1ms |
| Native AX snapshot, 34-60 el | 21-38ms |
| `app_state` no screenshot | 138ms (117ms over raw snapshot = driver RPCs for signals) |
| `app_state` with screenshot | 320-455ms (SCK capture is the bulk) |
| `dwell_after_click_ms` | 80ms per click, product-mandated visible dwell |
| Driver's own `glide_duration_ms` | 0, and irrelevant: the glide uses this skill's operator overlay, not the driver's agent cursor. Do not tune driver cursor motion to speed up glide |
| Glided click step, real | 180-251ms vs 106ms floor. Only 2 driver calls per 5-click run and snapshots are reused (2 AX snaps, not 5), so the gap is in-process wait granularity: operator-cursor ack polls every 20ms (`runtime_pointer.py`), `_wait_for_expectations` polls every 200ms (`runtime_plan.py`) |
| Driver RPC, CLI vs persistent socket | 169.8ms vs 0.1ms |
| JSON serialize 60-el tree | 0.10ms |

### Row floors — rating denominator

Floor = irreducible cost from the primitives above, launch + product-mandated dwell
included. `budget_*` stays the fail-closed gate and is never the rating denominator.
Speed rating = `10 * clamp01(floor / p50)`; 9.5 needs `p50 <= floor / 0.95`.

| Row | Floor | Composition | Was | Off floor |
| --- | --- | --- | --- | --- |
| calculator-8x8 | 1.582s / 106ms step | launch 1.010 + snap 0.021 + 5x(move+press 5.1ms+dwell 80ms+verify 21ms) + final query 0.021 | 5.828s / 1108ms | 3.7x / 10.5x |
| folder-downloads | 0.358s | snap 120el 0.038 + app_state with screenshot 0.320 | 0.986s | 2.8x |
| textedit-right-click | 1.284s | launch 0.869 + 5x snap 0.105 + glide right-click 0.085 + probe sleep 0.200 + keys 0.025 | 7.528s | 5.9x |
| whatsapp-new-chat | 0.125s | glide perform_action 0.085 + verify 0.030 + Escape 0.010 | 2.583s | 20.7x |

| Symptom | Cause | Fix |
| --- | --- | --- |
| Calculator/Finder/WhatsApp tree is Apple-menu items | `AXMenuBar` was used as a BFS walk root | Do not re-add `menubar` to `choose_walk_roots`. Open app-level menus, then one window. File menu later = wrap `invoke_menu`, never Apple-menu BFS |
| Plan re-walks AX after every accepted click | Stale habit from “indices are snapshot-scoped” | Reuse the postcondition tree; refresh once on label miss. Live proof: Calculator `state_reuses` |
| Glide misses a sheet/popover control | Container was AXWindow-only | `_glide_container_frame` uses popover/sheet/dialog/menu/largest containing frame |
| Background `key` misses after space/AX miss | One background delivery is not enough | Owner retries that key once when `off_space_or_ax_unresolved` or `escalation.recommended=foreground` |
| Chrome driven through macos-cua | Hermes coexistence owns Chrome | Use browser MCP. This skill is non-Chrome native UI |
| Dock full of Calculator/Dictionary/Stickies after a probe | Test apps left running | Quit the test app before closeout. See the rule above |
| Glide clicks the old screen point after the user moves the app | Labeled glide sent stale `cursor_screen_x/y`, which disables operator live parenting | Omit screen coords so the overlay maps normalized `cursor_x/y`. Do not Quartz-read on the Python click path. Do not add a second on-app icon |
| Session preflight fails because only one monitor is awake | Lab gate (secondary display) was copied into session preflight | Single-monitor is valid. `display_count_active` may be 1 while `display_count_configured` is 2. Do not move a window onto an asleep screen |
| AX/Screen Recording granted to Cursor but clicks do nothing | TCC keys to signed `Cua Driver.app`, not the MCP Python child | `cua-driver permissions grant` on the driver app. Do not wrap `compact_mcp.py` as a second signed principal |
| Retina / scale-cached pixel miss | Scale was reused from an older snapshot | Rebase from this snapshot's `screenshot_width/height`. Do not cache scale |
| Click blocked by an invisible Dock | CG Z-order / `kCGWindowSharingState=0` false-positive | Do not add a Dock hit-test. AX first; pixels stay window-local |
| Overlay covers every display / always-on screenshot | Union-frame overlay or default pixels | One-window operator; `state` stays `--no-screenshot` |
| Background click “works” but lands off-window | Global HID as the happy path | Glide then AX. Pixels are window-local + this-snapshot scale |
| Cheap preflight re-prompts Screen Recording | Sequoia monthly SCK-probe | `check_permissions` only. Do not SCK-probe in preflight |
| Background type uses clipboard paste | Community paste-from-background | Refuse. Type stays AX/`type-text`, not clipboard |
| Finder returns only menus / black screenshot | Finder desktop surface was resolved, or capture occurred behind another window | `macos-cua.py reset`; use foreground `state Finder` |
| Finder has `Downloads` as `AXStaticText` but label click misses | Sidebar names are static children; `find_clickable_index` only matches `CLICK_ROLES` | Owner attaches that text to the parent `AXRow`/`AXCell` (`_attach_static_child_text`). Do not add `bring_to_front`+sleep; see [`cua-driver-mcp.md`](cua-driver-mcp.md) |
| Open sheet list has no `--query` hits | Snapshot BFS spent `--max` on window chrome; filenames are nested `AXTextField` | Active surface is the sheet root; field values attach to the row. Do not press Open blind. WhatsApp completion stays `attach-file` |
| `type-text` `ok` but compose empty | Catalyst `AXSelectedText` set does not update `AXValue` | `ax_incomplete_value`; Voice→Send or screenshot is proof |
| `right-click-point` / invented point command rejected | Point right-click is not a separate command | `click-point <app> x y --button right` or `click-desktop x y --button right`; see [`special-surfaces.md`](special-surfaces.md) |
| `state com.apple.notificationcenterui` → `foreground window is not AX-ready` | Desktop widgets / Notification Center are not ordinary AX windows | Do not loop on `state`; use `displays` + fresh screenshot + `click-desktop` |
| Full `apps` output floods context | Unfiltered installed-app inventory | `apps --query TEXT` and/or `--running`; or target bundle/name directly with `state` |
| Action says `effect: unverifiable` | Driver can dispatch input but cannot infer app semantics | Fresh `state` or a `run` expectation owns proof |
| Drag is accepted but the surface does not change | Dispatch acceptance was mistaken for semantic effect, or the app rejected synthetic mouse input | Require before/after rendered proof; do not fall back to moving the user cursor; stop and report raw-HID-only surfaces |
| State or plan output consumes too much context | Full structured elements or successful step diagnostics were requested unnecessarily | Use `state --compact`; keep the default compact plan output; request `output:full` only for a failing run |
| `cmd+a` or native menu shortcut does not land | Key combo used wrong route, or app rejects background delivery | Current wrapper uses `hotkey`; retry only that action with foreground delivery |
| A custom-drawn command field receives canvas shortcuts instead of text | The field did not accept synthetic focus; foreground delivery does not make this safe | Stop CUA input immediately. Use the app's native automation/API entry point or report the gap; never loop on typing into this field |
| Repeated key taps do not trigger a time-based action | `press_key` releases immediately, often between application ticks | Focus the target once, then use `hold-key <app> <key> --duration 1.0`; prove with before/after state |
| Keyboard control works but raw mouse-look/drag capture does not | Agent overlay, background, or targeted mouse motion does not satisfy the app's raw HID capture path | Keep the user's pointer untouched; use the app's native automation/API proof path or report that raw HID input is unavailable |
| `ensure-display` says on Dell but ignores requested size | Older wrapper returned early when monitor already matched, or resized auxiliary `window 1` | Current wrapper verifies frame and selects the largest app window; rerun and require `frame_matches: true` |
| `state --screenshot relative.png` returns no screenshot | cua-driver requires an absolute output path | Current wrapper normalizes/creates relative output paths; raw driver calls must pass an absolute path |
| An asserted `run` passes but its required final screenshot is missing | Background WindowServer capture remained unavailable after the normal bounded retries | `app_state` fronts the resolved window and retries capture once; if `capture_recovery.captured` is not true, stop and report the proof gap |
| Screen Recording is `true` but `screen_recording_capturable` is `false` | The daemon's live capture probe disagrees with Apple's grant bit, usually because the grant belongs to a stale or different responsible-process identity | Preflight must stop. With operator confirmation run `cua-driver permissions grant`, then rerun preflight; do not retry screenshots or add capture fallbacks. |
| Another computer-use route clicks the wrong display | Its screenshot/coordinate mapping did not represent the target secondary-display window | Use `macos-cua` window-local/background actions on the reserved display; report and fix owner friction rather than retrying blind coordinates |
| Operator/PiP restarts after being stopped | Action telemetry starts the operator by default | Set `MACOS_CUA_OPERATOR_UI=0` for the action and verify `operator status` remains stopped |
| Background screenshot contains black/occluded regions | Window is covered | Use default foreground `state`; reserve `--background` for AX-only work |
| First `run` click sits ~30s then `accepted:false` with `native-axpress-fallback` | Native AX had no messaging timeout; a busy app blocked `AXPress`. Compact hid the nested error. | Owner sets a 1.5s timeout on the app and the press target (not every attribute). Nested press errors are lifted. Do not raise MCP timeouts. |
| Any `cua-driver call` hangs / 30s timeout | CLI reads params from **stdin** when argv params missing (empty `{}` dict is falsy!) — blocks forever on agent-shell pipes | Fixed in `call_driver` (always passes `'{}'` + stdin DEVNULL); raw CLI: always append `'{}'` and `</dev/null` |
| `cua-driver status` passes but one RPC times out | The process is alive but its request path is stalled | The shared transport restarts the LaunchAgent and retries that RPC once; a second timeout fails closed |
| Slow pointer clicks | A stale caller still prepares the cua-driver cursor or repeats focus/display work | Use `click-label-pointer` directly; the signed operator needs no driver cursor setup |
| Cursor stays on the previous control while actions continue | Target publication was mistaken for rendered movement, or AppKit implicit animation ignored a panel move | Current wrapper waits for `cursor_rendered_x/y` before AX click. Run the live three-target pointer grader; never bypass its acknowledgement |
| Two visible pointers (cyan/blue + black Hermes) | Legacy cua-driver `auto-*` cursor minted by sessionless pixel/desktop clicks (or left from an interrupted prove) beside the signed operator | Primary path uses only Hermes. `click-label-pointer` / `click_at_desktop` / `click-point` wipe driver cursors before and after; run `workflow.py closeout` if a stray remains |
| Pointer wrong monitor or offset | A stale caller converted the global AX target to driver-local coordinates | Remove driver cursor alignment/local-coordinate logic; [`displays.md`](displays.md) owns the global signed-operator contract |
| Point action says the screenshot is stale or geometry changed | The coordinate no longer belongs to the observed exact window | Run one fresh `state`, use its raw pixels, and retry only the point action; do not extend the freshness budget |
| `ensure-display` reports an off-screen tiny app window | Stage Manager exposed a hidden Quartz thumbnail | Upgrade/run the logical AX fallback; require `verification: accessibility-logical` and inspect `quartz_window_after` |
| Cursor invisible | Signed operator is stopped, hidden, or lacks a valid Hermes raster | `operator start`; run `tests/test_live_pointer_isolation.py`; do not enable a second driver cursor |
| `Sign out` / modal not found | `--max 50` truncates tree | `--max 80` or `MACOS_CUA_MAX_MODAL=80` |
| Delete hits sidebar not modal | Duplicate label | `cua_click_last_label "Delete account"` |
| `Profile could not be saved` in AX but flow continues | Stale UI banner; or no profile row | API `assert_basics_saved_api`; API creates draft on first PATCH |
| Onboarding from Meet tab | Wrong screen | Click **Profile** first |
| `list_windows` / no app window | No usable on-screen window, stale cache, inactive Stage Manager thumbnail, or argv-less raw CLI call blocked on stdin | Foreground commands dispatch NSWorkspace activation for existing apps, resolve their authoritative PID through Quartz before synchronous AX, then use bounded driver foreground + fresh snapshot as proof. Do not require immediate `isActive` during Stage Manager transitions. Run `macos-cua.py reset`; if the app owns a launcher, use that repository entrypoint. |
| `list-buttons` exits nonzero with a JSON error | The resolved-window foreground or snapshot failed, timed out, returned an empty AX tree, or exposed only application/menu scaffolding | Existing apps get bounded NSWorkspace plus PID-specific System Events foreground dispatch before the driver gate. If the driver still returns scaffolding, the wrapper reactivates the exact PID and reads native AX. When native AX confirms the running process has no window, it sends one bounded bundle reopen event and retries native inventory without an intervening driver call. If WindowServer still has a live rendered window but AX remains menu-only, the wrapper asks the already-permitted driver to write the window screenshot using `capture_mode=vision` (the strict 0.7.x schema rejects `mode`), verifies PID-owned Quartz bounds, then runs Apple Vision OCR on that file when the driver supplies no framed labels. Those frames permit pointer-grounded pixel fallback; fresh destination state still proves the action. The error includes observed roles/element count when every route lacks content. `get_window_state` is bounded by `MACOS_CUA_STATE_TIMEOUT` (default 12s). |
| Wrong app / menu bar tree | Stale cache or untitled desktop surface | `macos-cua.py reset` + `state <app>`; titled windows are preferred |
| `not found in cache` | Stale snap | Re-snap before click |
| Pointer-action proof has no cursor | Raw capture was returned or cursor state is stale | Run a fresh pointer action and use the returned `*-cursor.png`; raw captures intentionally remain unmodified |
| Proof PNG has a cursor but the user sees none | Proof compositing passed while the signed desktop overlay is absent | Run `tests/test_live_pointer_isolation.py`; require its layer-1000 window and cyan-pixel gate, then rebuild/restart `operator_ui.py` if it fails |
| Signed cursor publish succeeds but rendered-position acknowledgement times out | The live operator missed a state-file transition while remaining healthy | The pointer action republishes the same target once and still requires rendered-coordinate acknowledgement; a second miss fails closed |
| Pointer is black but lacks the Hermes glow | macOS rendered only the SVG and omitted browser CSS filters | Rebuild the signed operator; it must custom-draw the 12 px cyan and 4 px depth shadows. Asset-path equality is not proof |

## Recovery

```bash
launchctl kickstart -k gui/$UID/com.trycua.driver
python3 $SKILL_DIR/scripts/macos-cua.py reset
python3 $SKILL_DIR/scripts/workflow.py preflight
```

Raw `health_report` is fine only with params + closed stdin: `cua-driver call health_report '{}' </dev/null`.
