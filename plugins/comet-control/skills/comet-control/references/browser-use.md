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

Before the first `browser-use` program on a slow site, and before any fresh-nav
reload, run the settle gate on the same lease:

```bash
python3 skills/comet-control/scripts/settle_preflight.py --work "$WORK" \
  --timeout 75 --expect-url linkedin.com --lock <browser-use lock>
```

It reaps this lease's stray Browser Use daemons, clears stale sockets, and proves
`js('1+1') == 2` on the expected tab. `--timeout` is one overall deadline for
discovery, kill, slot wait, probe, and an 8 s cleanup reserve; 75 s leaves the probe
about the harness's 60 s daemon startup window. Slow process discovery fails as
`discovery_slow`. After every miss it kills this lease's daemons (the harness daemon
runs in its own session, so it is found by `BU_NAME`, not by process group) and
re-scans to prove they are gone; a failed or unverified reap is `stage: cleanup`,
`cause: cleanup_failed` (with `probe_cause`) and is never retried. If a caller kills
the script mid-probe, run it again with `--reap-only`: it kills the probe group
recorded in `$WORK/settle-probe.pid` and the lease's daemons, then verifies. Exit 3 (gate `li_settle_preflight`) fails closed and
carries `cause`, `attempts`, and `timings`. Escalate on the same lease; never
remint or rerun it by hand. The gate itself retries only `slot_busy` (1008
"lease already has a Browser Use client") with a 1/2/4 s backoff, and it reaps
daemons after every miss so none keeps the bridge slot. Causes: `slot_busy`,
`cdp_slow` (empty `enable …:` or duplicate responses in the daemon log),
`probe_slow`, `attach_slow`, `spawn_slow`. Each run appends a timing line to
`$WORK/settle-preflight.jsonl`, and daemon logs are copied to
`$WORK/settle-daemon-*.log` before a kill, because the daemon truncates its log
on every spawn. Tails, log copies, and the jsonl are redacted (`BU_CDP_WS`, ws/wss
URLs, `token=`/`key=` values). The bridge hands its slot to a new client once the previous
client's socket has closed, even while that client's last call is still in flight.
Browser Harness `cdp()`/`goto_url()` default to a 5 s IPC
timeout; for heavy navigations use
`cdp('Page.navigate', url=..., _response_timeout=45.0)` and treat
`TimeoutError` as a hard stop, never a `location.href` fallback.

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
