# Comet Control — Native-computer coexistence



`coexistence-v1`: ownership table and handoff protocol in
[`references/multi-agent.md`](references/multi-agent.md). Concurrent only on
disjoint exact PIDs; the Comet Control PID needs a CUA claim (`native-dialog`) or
zero leases (`comet-admin`). Clipboard / display / shell mutations are
serialized handoffs, never parallel.

The Comet Control broker and macos-cua app commands share the crash-safe
`visual-focus-v1` lock. The broker—not a short-lived client—holds it until
Comet answers, so direct tool calls and client death cannot release the lane
early. For in-page work it serializes input/capture without activating Comet or
stealing the human's macOS key focus; the labeled cursor still glides in-page.
Only a true OS sheet handed to CUA may require foregrounding. Batch coherent
actions so the handoff stays fast.




Comet Control owns **in-page** work, including JS `alert`/`confirm`/`prompt`. Use
`$macos-cua` when the job is **outside** that surface: non-Comet apps, OS
sheets/file choosers/system permission UI, or Comet shell
(`chrome://`, extension admin, launch/quit, geometry). Hand off per
[`references/multi-agent.md`](references/multi-agent.md); resume the same
Comet Control lease afterward. Do not drive page DOM through macos-cua.

For OS UI on the Comet Control PID, use the atomic helper (do not improvise
claim/release loops):

```bash
python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" --ttl-seconds 45 state
python3 skills/comet-control/scripts/cua_slice.py --workdir "$WORK" run @plan.json
```

Google sign-in overlays (X, LinkedIn, etc.): Comet Control clicks the in-page
Continue/Google CTA once → one `cua_slice` state → one labeled click with
`expect` → Comet Control `page_context` on the **same** lease. Do not close the
lease, improvise claim loops, or re-click the page CTA while the overlay is
up. Full recipe: [`references/multi-agent.md`](references/multi-agent.md)
§ Google OAuth slice.

## Other-window Google accountchooser

If GSI opens in a **separate** Comet window, do **not** drive it via lease CDP or blind `cua_slice` coord clicks. Use Accessibility `AXPress` on the account `AXLink` in that window (recipe: [`google-accountchooser-ax.md`](google-accountchooser-ax.md)). Fix CUAService Accessibility if `axTrusted=false`.

