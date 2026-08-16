# Operator UI and harness integration

`macos-cua` owns one native companion process for every harness. It reads a
small JSON state contract written by the CLI and never captures the screen
itself; PiP reuses the proof image already captured through `cua-driver`.

## Visible contract

- Menu bar: cursor icon plus controlled app name.
- Menu: controlled app, window, harness, status, PiP toggle, refresh, end session.
- PiP: floating, non-activating, resizable panel on all Spaces with the current
  image, a clamped full Hermes pointer plus exact cyan target ring,
  `status • app • harness` label, and direct
  Hide/Refresh/End controls.
- Desktop cursor: a separate click-through `.screenSaver`-level panel owned by
  the signed operator. It renders Hermes Chrome's 28 px arrow, 12 px cyan glow,
  4 px dark depth shadow, 1.7 s float, 0.32 s glide, and harness label without
  activating the app or moving the hardware pointer. The glide uses an explicit
  main-run-loop timer because implicit `NSPanel.animator()` movement was ignored
  after the first target on this window level.
- Proof PNG: a fresh `state` after a pointer action composites the same cursor
  asset into a deterministic `*-cursor.png`; the raw capture remains available.
- Active state shows PiP by default. `workflow.py closeout` marks the session
  idle and hides PiP while leaving the inexpensive menu service available.

```bash
python3 $SKILL_DIR/scripts/macos-cua.py operator start
python3 $SKILL_DIR/scripts/macos-cua.py operator status
python3 $SKILL_DIR/scripts/macos-cua.py operator stop
python3 $SKILL_DIR/scripts/macos-cua.py operator install-service
python3 $SKILL_DIR/scripts/macos-cua.py operator signing-status
python3 $SKILL_DIR/scripts/macos-cua.py operator uninstall-service
```

Preflight packages the AppKit executable as
`~/Library/Application Support/macos-cua/macos-cua Operator.app`, signs it with
`MACOS_CUA_SIGNING_IDENTITY` or the first valid Developer ID/Apple Development
identity, verifies the signature, and installs the reversible user LaunchAgent
`com.macos-cua.operator`. launchd keeps the menu service alive and restarts it
after a crash. `uninstall-service` removes both installed artifacts.

An ad-hoc signature is a runnable degraded path when no identity exists, but it
does not pass the parity gate. Notarization is a distribution concern and is
not required for this local per-user service.

The native panel is the functional control owner across harnesses. A local
skill cannot inject UI into proprietary Codex sidebars; keeping the controls in
one signed companion avoids separate Codex/Cursor implementations and drift.

Set `MACOS_CUA_OPERATOR_UI=0` only for headless/static runs. Set
`MACOS_CUA_HARNESS=<name>` when a harness does not expose a recognizable
environment variable.

## Harness discovery

The source of truth remains this plugin's `$SKILL_DIR`. Install the Cursor
plugin copy and idempotent skill links:

```bash
python3 $SKILL_DIR/scripts/install_harness.py all --replace-copy
```

`--replace-copy` deletes a copied `macos-cua` skill tree and replaces it with
a symlink. The installer refuses a link to another owner. Do not copy the
skill into `~/.cursor/skills` or `~/.agents/skills`; copied forks drift.

## State contract

`~/.cache/macos-cua/operator-state.json` contains version, active/status,
app/PID/window, raw screenshot path, normalized cursor position/visibility,
the independently acknowledged `cursor_rendered_x/y`, cursor asset path, PiP
visibility, harness, session ID, message, and timestamp. Writes are atomic. A
new target clears the old rendered acknowledgement; pointer clicks wait for the
new exact acknowledgement before dispatch. A changed app clears stale
screenshot and cursor state until fresh proof arrives, so the PiP never labels
one app with another app's image.

The cursor visual owner is this skill's portable
`assets/pointer-shape-animated.svg`. Hermes may consume the same bytes, while
`MACOS_CUA_CURSOR_ICON` remains the explicit integration override. The wrapper
materializes `~/.cache/macos-cua/hermes-pointer.png` for native APIs. The
operator renders the external glow and motion; do not create another shape.

Asset-path equality is not visual proof: macOS CoreSVG can omit browser CSS
filters. `tests/test_live_pointer_isolation.py` moves across three Calculator
targets, requires a rendered acknowledgement for each, compares the actual
layer-1000 Quartz origin within one pixel, captures the operator overlay, and
requires cyan glow pixels. A proof PNG or cua-driver `overlay_enabled` state
cannot replace that capture.
