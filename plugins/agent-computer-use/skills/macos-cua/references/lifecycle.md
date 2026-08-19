# Lifecycle

```text
workflow.py preflight     # once, start: daemon, TCC, signed operator
  Computer Use MCP        # middle: start_session → act-first per app → end_session
workflow.py closeout      # once, end: cache, cursors, operator idle
```

Preflight never launches or focuses an app. On-screen window readiness is
Computer Use `resolve_app` (cold launch uses `open -n`; PID-live is not
proof). `end_session` already runs closeout; a trailing `workflow.py closeout`
is the session bookend, not a per-app ritual.

## Setup (once)

```bash
ls ~/.local/bin/cua-driver
~/.local/bin/cua-driver --version  # must be >= 0.8.3
python3 $SKILL_DIR/scripts/workflow.py preflight
```

Daemon wedge: `launchctl kickstart -k gui/$UID/com.trycua.driver` → re-run preflight.

Preflight only stabilizes infrastructure: it requires the daemon's
Accessibility and Screen Recording grants, but never launches or exercises a
test app. Older drivers may also report a direct capture probe; an explicit
`false` fails closed, while an absent probe is left to the live validation
commands below. With operator confirmation, run `cua-driver permissions grant`,
then rerun preflight.

Preflight packages, identity-signs, verifies, and starts the native operator as
a per-user launchd service. The first build requires the system `swiftc`; later
sessions reuse the installed app bundle. `state` and `run` publish
app/window/harness/cursor/proof state automatically. Set
`MACOS_CUA_INSTALL_SERVICE=0` only for a deliberate transient/manual run.
Healthy preflight prints a compact readiness packet; add `--verbose` only when
diagnosing a failed component.

Full local acceptance gate (safe native app plus isolated temporary TextEdit):

```bash
python3 $SKILL_DIR/tests/test_live_computer_parity.py
python3 $SKILL_DIR/scripts/validate-macos-cua.py --live --progress
```

Production hardening gate (static checks plus both live graders and ledger):

```bash
python3 ~/.agents/skills/elon-algorithm/scripts/run_gate.py \
  --contract $SKILL_DIR/hardening-contract.json \
  --label iteration-1
```

Run it twice without source changes for closeout. The second report must have
`ok`, `source_unchanged_during_gate`, `unchanged_from_previous_green`, and
`empty_iteration` all set to `true`; enforce that with `--require-empty`.
Logs and the append-only local ledger live under
`~/.cache/elon-algorithm/macos-cua/`.

## Act loop (single app session)

```
state <app> --compact → asserted act/plan → conditional independent readback
```

- The signed operator owns the visible cursor. Each pointer action publishes the
  global AX target, waits for the operator's rendered-position acknowledgement,
  then performs the AX click. Do not prepare or move the cua-driver cursor.
- `scripts/mcp_runtime.py` holds one facade and driver socket for MCP lifecycle,
  state, and act calls. `scripts/macos-cua.py` remains the stable diagnostic
  compatibility facade. Cohesive `scripts/runtime_*.py` modules own
  coexistence, transport, app resolution, capture, actions, accessibility,
  plans, and CLI dispatch. Validation rejects production Python modules over
  600 lines so the monolith cannot return.
- Drag mechanics, native text readback, and targeted key delivery live in
  `scripts/native_input.py`; coordinate text selection lives in
  `scripts/native_text_pointer.py`. Drag tries
  native AX slider control first, then an explicitly reported system-cursor
  fallback while mirroring the trajectory with the signed agent pointer.
- Label clicks try the live native AX tree first, then the driver AX snapshot,
  then visual grounding. A plan reuses a successful postcondition snapshot for
  the next action while it remains fresh.
- `macos-cua.py reset` after app rebuild or wrong window.
- Use `run` for 3+ actions so fresh indices, assertions, cursor proof, and one
  final capture stay in a single bounded process.
- A verified plan already contains independent post-mutation readback. Do not
  follow it with another `verify`; reserve that re-read for unverified actions
  or external state changes.
- App-owned wrappers remain responsible for app-specific routes and assertions.

## Hermes browser handoff

Ordinary native apps and a Hermes lease may run at the same time when macos-cua
uses an exact non-Chrome PID. The CLI automatically authorizes every resolved
target against `coexistence-v1`; this adds no broker dependency for a disjoint
native app.

App commands and Hermes native-host requests share `visual-focus-v1`, a short
kernel lock with in-process thread serialization at
`~/.cache/macos-cua/visual-focus.lock`. Hermes holds it through the Chrome
response even if its client dies; it releases on normal exit, error, interrupt,
or owner-process death. This prevents concurrent foreground/capture operations
while leaving task planning and non-UI work concurrent.
Full-window proof is checked against the exact window geometry; one bounded
foreground recapture replaces a Stage Manager thumbnail, then fails closed.

**Ownership:** Hermes owns page DOM and JS `alert`/`confirm`/`prompt`. This
skill owns OS-native UI (sheets, file choosers, system permission dialogs) and
Chrome shell admin. Hand off when the job is that surface — not because Hermes
“failed” a page action.

Before touching the Hermes Chrome PID for OS-native UI, run the app-agnostic
`workflow.py preflight`. Prefer Hermes'
atomic slice (claim → this skill → release) so agents cannot leave orphan
claims or desync the durable controller:

```bash
python3 ~/.agents/skills/hermes-chrome/scripts/cua_slice.py \
  --workdir "$WORK" --ttl-seconds 45 state
python3 ~/.agents/skills/hermes-chrome/scripts/cua_slice.py \
  --workdir "$WORK" run @plan.json
```

Manual equivalent: Hermes `native_handoff` → pass claim + session id +
`browser_pid` here → always release the claim. Keep the Hermes driver alive;
after the slice, Hermes re-observes on the **same** lease. For launch/quit,
geometry, `chrome://`, or extension repair: zero Hermes leases, then
`--browser-intent chrome-admin`, then re-probe. Never pass a Hermes lease token
or persist the short-lived claim.

If Hermes `page_context` includes `handoff_hint` (blocked OAuth popup / native
overlay), do not retry in-page clicks — run `cua_slice` instead. Same when
Hermes still lists the page Google CTA but `state` shows a GSI card,
`accounts.google.com` chooser, or `Continue as …` button Hermes cannot see.

### Google OAuth (CUA side only)

Own only the overlay / chooser. Hermes owns the in-page CTA and post-login
assert. Prefer Hermes `cua_slice` (claim → here → release); never hold a claim
across chat turns.

1. `state` (compact) — pick the **primary** control: `AXButton` / `AXLink`, not
   static name/email text under it.
2. One `click` with exact AX label + per-step `expect` (e.g.
   `not_text: "Continue as …"` or `not_text: "Choose an account"`).
3. Exit; Hermes re-observes the same lease.

Ambiguous labels fail closed. If both an in-page Google control and a GSI
overlay match a substring, use the longer exact overlay label from `state`.
Mutating plans without `expect` are rejected (`assertion_required`).

## Closeout

`workflow.py closeout` clears cached app/window resolution JSON, hides and ends
agent cursor sessions, marks the operator idle (which hides PiP), and verifies
the daemon. The signed launchd-owned menu item stays available for the next
harness. Raw and cursor-composited proof screenshots under
`~/.cache/macos-cua/screenshots/` remain available for review. Closeout protects
the active proof and prunes unreferenced files beyond the configured 30-day or
256-MB retention boundary.
Healthy closeout prints only the compact cleanup proof; use `--verbose` for the
full component report.

Resolution reset is schema-scoped: it does not delete the operator state or
process record even though those JSON files share the same cache directory.

## Storage

Cache and proof screenshots live under `~/.cache/macos-cua/`. Closeout
protects the active proof and prunes unreferenced screenshots beyond 30
days or 256 MB; override with `MACOS_CUA_PROOF_MAX_AGE_SECONDS` and
`MACOS_CUA_PROOF_MAX_BYTES`. The signed app lives under
`~/Library/Application Support/macos-cua/`; its reversible service is
`~/Library/LaunchAgents/com.macos-cua.operator.plist`. The skill stores
no app data.

## Test-app hygiene

Live probes may launch Apple test apps or other fixtures. Quit each one
immediately after its final assertion, before the next app can inherit its
foreground window. Do not quit Cursor, the user's messenger, or a browser
that still has the user's tabs.
