# Browser Use in Comet

> **Never dual-drive.** If using this bridge, do not also `send` JSON actions as a concurrent input owner on the same lease. Primary agent path is `durable_lease_controller.py send` — see [SKILL.md](../SKILL.md).


Comet Control leases one visible tab in the user's existing logged-in Comet
profile. `durable_lease_controller.py start --browser-use` creates a private
loopback CDP WebSocket for that tab and writes `BU_CDP_WS`, `BU_NAME`, and
`BH_OPEN_LIVE_URL=0` to `<workdir>/browser-use.env` with mode `0600`.

Require the current Browser Use CLI with its Browser Harness skill surface:

```bash
browser-use skill >/dev/null
```

If that command is unavailable, install or upgrade the upstream `browser-use`
package before acquiring a Comet lease.

```bash
. "$WORK/browser-use.env"
browser-use <<'PY'
print(page_info())
click_at_xy(320, 240)
print(page_info())
PY
```

## Stale daemon recovery

Bridge startup reaps only the Browser Harness daemon named by this lease before
publishing its fresh private CDP endpoint. A bridge-side `Connection lost` also
closes that client and clears its exact `bu-$BU_NAME.{pid,sock}` artifacts.
Comet, the lease controller, and the driver stay alive.

If `Runtime.evaluate` or `Page.navigate` still returns the daemon's 5s IPC
timeout, recover once and retry only that failed read or navigation:

```bash
. "$WORK/browser-use.env"
python3 skills/comet-control/scripts/browser_use_cdp_bridge.py \
  recover --name "$BU_NAME"
# retry the failed page_info/evaluate/navigate once
```

The recovery command validates the PID is a `browser_harness.daemon` owned by
this exact Comet bridge name, terminates only that process, and removes only its
PID/socket pair. It fails closed if the PID belongs to another process or
Comet name. Never glob runtime files, kill all harness daemons, remint the
lease, or drive through `send` while Browser Use owns the campaign.

After a media/upload mutation timeout, recover once, re-read `page_info`, and
confirm state before deciding whether the mutation needs a retry; do not blindly
resend it. Escalate to ACU only if the same evaluate/navigation probe fails
after this one local recovery. ACU inherits the same lease and captain rule.

The adapter presents exactly one synthetic target. It maps Browser Use's tab
attachment to the lease, rejects foreign targets and browser-wide CDP domains,
and keeps the real lease token inside the driver. `Input.dispatchMouseEvent`
moves the extension cursor first, waits for its visible glide on mouse press,
then dispatches the CDP input and click pulse. Browser Use and the pointer
therefore share one coordinate stream and one control owner.

Use the upstream Browser Use skill's accessibility-first page workflow and raw
`cdp(...)` escape hatch. Load Browser Harness interaction guidance only when a
mechanic needs it: connection, cookies (page interaction only; cookie export is
blocked), cross-origin iframes, dialogs, downloads, drag/drop, dropdowns,
iframes, network requests, print-to-PDF, screenshots, scrolling, shadow DOM,
tabs, uploads, and viewport. The single-target bridge intentionally reuses the
leased tab when Browser Use asks to create a tab and refuses to close it outside
Comet Control closeout.

Do not merge these Browser Use skills into this local logged-in path:

- `cloud`, `remote-browser`, and `qa`: hosted or isolated browsers, not the
  user's Comet session, and may require an API key.
- `x402`: paid cloud execution and wallet funding.
- `open-source`: Python Agent/Tools application development; use only when the
  task is to build a Browser Use application, not to control Comet.
- `profile-sync`: copies authentication material into another browser. Comet
  already uses the real profile in place, so copying is unnecessary.

Domain skills remain disabled (`BH_DOMAIN_SKILLS=0`) and recordings remain off
unless the user explicitly asks. Stop for passwords, MFA, consent, CAPTCHA, or
ambiguous account selection. Close the campaign through the durable controller
and require `verified_absent: true`; do not close the synthetic CDP target.
