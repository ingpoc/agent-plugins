# Comet Control capability boundary

Comet is Chromium-based and supports extensions. Comet Control uses the
operator's existing logged-in Comet profile so authenticated browsing state is
available without copying credentials.

Official references:

- <https://www.perplexity.ai/help-center/comet/en/articles/11734716-extensions>
- <https://www.perplexity.ai/help-center/comet/en/articles/11642916-profiles-on-comet>

## Owned surface

Comet Control owns page navigation, semantic and coordinate interaction,
screenshots, dialogs, downloads/uploads, console/network diagnostics, and
token-protected isolated window leases inside the managed Comet runtime.

The local broker binds only loopback, accepts the exact configured extension
origin, and verifies the configured Comet executable and default profile.

## Unowned surface

Bundled `@chrome` remains the sole Google Chrome owner. Comet Control does not
launch Chrome, access Chrome profiles, register anything there, claim an
existing browser tab, or fall back to another browser.

Comet shell and macOS-native surfaces remain `$macos-cua` work under the
short-lived ownership handoff described in `multi-agent.md`.

## Proof

Run the deterministic unit/contract suite and strict validator after every
source change. Runtime acceptance additionally requires a successful broker probe,
a leased-page readback, rendered screenshot inspection, closeout,
and an empty session inventory. Repeat the live capability and isolation runs on
source-frozen deploy before claiming resilience.

## Reliability capabilities

- Protocol-1 extension/broker pairing with one fenced connection generation.
- Playwright-style pre-click/fill actionability with structured failure codes.
- Exact-node opaque refs invalidated on relevant DOM revision or navigation.
- In-memory semantic plan reuse keyed by extension version, origin, path, page
  fingerprint, and revision; no coordinate, secret, form, or submission cache.
- Operator pause cancels active runs and rejects new mutations while preserving
  status, lease renewal/closeout, and CUA release.
- Failure-only local flight records and a repeatable fixture corpus for
  remounts, overlays, motion, dialogs, navigation, disconnects, large frames,
  and duplicate extension generations.
