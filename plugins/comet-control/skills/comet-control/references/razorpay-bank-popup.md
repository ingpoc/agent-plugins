# Razorpay Bank popup in OTHER Comet window

Canonical recipe when Razorpay **test-mode** netbanking opens a separate
`Razorpay Bank` popup (`window.open` →
`api.razorpay.com/v1/gateway/mocksharp/payment?key_id=rzp_test_…`) while the
Buyer lease stays on checkout and the Razorpay iframe sits on `Sending OTP`.
Keep [SKILL.md](../SKILL.md) thin; load this reference only then.

## Why the lease cannot do it

| Surface | Sees the popup? | Proof (2026-10-05 fixture) |
| --- | --- | --- |
| Lease `page_context` / locators / `focus_tab` | No, tab-scoped (`LEASE_TAB_SCOPED`) | `final_url` stays on checkout |
| Lease `BU_CDP_WS` (`lease_driver` bridge) | No: one synthetic target | `Target.getTargets` = 1 before **and** after popup |
| Browser-wide CDP | Usually absent: real Comet runs without `--remote-debugging-port` | n/a |
| Second lease | No: a lease opens its **own** window and cannot adopt the popup; reopening the mocksharp URL loses `window.opener` | n/a |

So the product recipe **never** assumes `Target.getTargets` /
`attachToTarget` on the Buyer lease's `BU_CDP_WS`.

## Proven path: `razorpay_bank_popup.py`

Binds the popup by Comet window identity, never by Comet PID or coordinates:
exact window name `Razorpay Bank`, titles containing `Baroda` / `Mock Bank`
rejected, active-tab URL prefix `https://api.razorpay.com/v1/gateway/`, and
`key_id=rzp_test_` required on razorpay.com (live mode never binds). Full URLs and
key ids are never printed.

Method ladder (`--method auto` picks via preflight):

1. `ax`: AXPress the `AXButton` in that AX window. Needs an unlocked screen and a
   healthy Comet AX tree (same shape as [google-accountchooser-ax.md](google-accountchooser-ax.md)).
2. `applescript-js`: Comet AppleScript `execute javascript` on **that tab only**:
   exactly one visible button whose text is `Success` → `click()`. Works with the
   screen locked, displays asleep, and AX degenerate. Needs Comet › View ›
   Developer › *Allow JavaScript from Apple Events* (probe proves it on the popup
   tab). This is the one scoped exception to "no script-eval clicks". It applies
   only to a non-leased, test-mode popup. Leased tabs keep the visible cursor.

`cua_slice` / CUAService pixel clicks do not appear on this ladder. They bind the
Comet PID and not the window, and they are blocked whenever the screen is locked.

## Exact invocation (Aadhar / Buyer lease owner)

Run from `~/.agents/plugins/comet-control` on the Mini. Do not send lease
actions while it runs (single actor):

```bash
H=skills/comet-control/scripts/razorpay_bank_popup.py
python3 $H preflight                    # lock/display/AX state + bound windows (no JS)
python3 $H probe                        # read-only: buttons + readyState per bound popup
python3 $H press --action success       # fails closed on 0 or >1 actionable popups
#   >1 "Razorpay Bank" (stale first attempt): --window-id <id from probe>  (or --pick newest)
```

Then, on the **same** Buyer lease (not the popup), verify checkout advanced.
**UNSAFE — do not use:** `locator` + `frameSelector` into `iframe.razorpay-checkout-frame`
(Razorpay OOPIF). ONDC / AadhaarChain: that path hangs → `timeout_waiting_response` →
`LEASE_CLEANUP_INCOMPLETE`. Never send that action shape.

Safe post-check (pick one; stay on the Buyer lease):

```bash
# Preferred: compact page_context on the leased checkout tab
python3 skills/comet-control/scripts/durable_lease_controller.py send --workdir "$WORK" --timeout 45 \
  '{"actions":[{"type":"page_context"}]}'

# Or: Browser Use AX / body text on the same lease (after sourcing browser-use.env)
# browser-use --browser-use …  # AX tree / body text; look for absence of "Sending OTP"
```

Pass = popup `popup_state` is `closed`/`advanced`, leased page / AX / body text no longer
shows `Sending OTP` (success / order URL / success copy instead), and checkout advances.
Optional API-first proof: `GET /v1/payments/{id}` with Keychain `razorpay.test.key_id` /
`razorpay.test.key_secret` (reuse; never print, never write on the Air).

## Failure modes

| Symptom | Real cause | Do |
| --- | --- | --- |
| CGWindow exists, `onscreen=null`; or `onscreen=true` after the lease closed it | WindowServer ghost under a locked screen: Chrome-model window gone (`closeout verified_absent`) but CG id lingers | Trust Comet AppleScript window list / `closeout`, not CG ids; ghosts reap on wake |
| Captures pure black (`screencapture`, CUA) | `CGSSessionScreenIsLocked=true`, `CGGetActiveDisplayList`=0, so the cause is not only Screen Recording | `preflight` shows it; use `applescript-js`; lease `screenshot` (CDP) still renders the leased tab |
| AX empty / every node `AXApplication "Comet"` | Comet AX degenerate (`AXWindows` returns the app element) | `ax` fails closed (`ax_window_not_found`); do not restart Comet under live leases |
| `macos-cua.py status` → `~/.local/bin/cua-driver` FileNotFound | Legacy status path, a red herring | Engine is CUAService (`~/.cache/macos-cua/cua-service.sock`, `service/cua_client.py`) |
| `cua_slice` → `Cannot claim managed Comet while N browser mutation(s) are active` | `native_handoff` is per Comet PID, so another lease's in-flight run blocks it | Not a popup path anyway; use this helper |
| Lease `click_text` → `ACTIONABILITY_UNSTABLE`, `frame_deltas_ms`≈1000 | Hidden/locked tab throttles rAF+timers, so `_stableRect` never gets 3 samples | Use `click_at_xy` (trusted CDP) for in-lease clicks while locked |
| `Executing JavaScript through AppleScript is turned off` | Comet developer toggle off | Human enables it once, or unlock and use `ax` |
| Two `Razorpay Bank` windows | Earlier attempt left a popup | `probe` → `--window-id`; never press both |
| Checkout / body / AX still shows `Sending OTP` after `popup_state=closed` | Gateway callback is slow or the popup was stale | One lease `page_context` (or `--browser-use` AX / body text) after ≤30 s; do not re-press; never `frameSelector` into `razorpay-checkout-frame` |

## Proof (synthetic, Mini, screen locked)

Fixture: a throwaway lease on a local checkout page with a cross-origin iframe
(`Sending OTP`) that `window.open`s `Razorpay Bank` (Success/Failure) and a
`Bank of Baroda — Razorpay Mock Bank` decoy. Results: `ax` 0/1
(`ax_window_not_found`), `cua_slice` 0/1 (handoff refused), and `applescript-js`
6/6. The 6 runs were 4 dev runs plus 2 E2E runs on the installed copy. 5 were
Success, which moved the iframe to `Payment Successful`, and 1 was Failure, which
moved it to `Payment Failed`. The bridge reported `Target.getTargets` = 1 with the
popup open (3/3 checks). The decoy was never pressed, the real Aadhar popup was
skipped (`url_prefix_mismatch`), and closeout returned `verified_absent`.
