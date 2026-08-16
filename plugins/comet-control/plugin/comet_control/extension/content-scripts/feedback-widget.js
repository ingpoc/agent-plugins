/**
 * Comet Control Bridge — Feedback Widget content script.
 *
 * Injected on-demand by service_worker.js when the popup toggle is flipped on.
 *
 * Loads overlay.html (markup + styles) from the extension, strips its inline
 * <script>, mounts the markup into the page, then loads the IIFE via a separate
 * <script src=chrome-extension://.../feedback-widget-runtime.js>. The script-via-src
 * path is mandatory: MV3's isolated-world CSP blocks any inline <script> a content
 * script tries to inject into the page (`script-src 'self' 'wasm-unsafe-eval'
 * 'inline-speculation-rules' chrome-extension://<id>/`). Scripts served from the
 * extension URL are allowed because the extension's own URL is in that CSP.
 *
 * Queue origin defaults to the feedback server's default port (http://localhost:4177)
 * and is passed via data-queue-origin on the mount node; the runtime reads it from
 * there and uses it as the fetch base. When the server runs on a non-default port or
 * a different origin, the operator sets it in the popup and the service worker injects
 * the override (window.__COMET_CONTROL_FEEDBACK_QUEUE_ORIGIN).
 */
(() => {
  const FLAG = "__cometControlFeedbackWidgetInjected";
  if (window[FLAG]) return;
  window[FLAG] = true;

  // Queue origin resolution (most specific wins):
  //   1. window.__COMET_CONTROL_FEEDBACK_QUEUE_ORIGIN — injected by the service worker from
  //      the operator's persisted setting (chrome.storage `feedbackQueueOrigin`). Set
  //      this in the popup whenever the feedback server runs on a non-default port or
  //      a remote/different origin than the page being annotated.
  //   2. http://localhost:4177 — the feedback server's DEFAULT port (see
  //      artifact-feedback-server.mjs and wake-bridge.md). This is the correct default
  //      for the extension's primary mode: annotating a SEPARATE running app while the
  //      feedback queue server runs as a fixed sidecar on 4177. location.origin would
  //      be wrong there (it points at the app, not the queue server).
  //
  // Use `localhost`, NOT `127.0.0.1`: WSL2 Node bound to IPv6 wildcard (::) is
  // reachable from Windows-side Chromium via `localhost` (→ [::1]) but NOT via
  // `127.0.0.1`. If the server runs on a non-default port, set the popup override.
  const QUEUE_ORIGIN =
    (window.__COMET_CONTROL_FEEDBACK_QUEUE_ORIGIN) || "http://localhost:4177";

  (async () => {
    let html;
    try {
      const url = chrome.runtime.getURL("content-scripts/overlay.html");
      const res = await fetch(url);
      if (!res.ok) throw new Error(`overlay fetch ${res.status}`);
      html = await res.text();
    } catch (err) {
      console.error("[comet-control-feedback] failed to load overlay.html:", err);
      window[FLAG] = false;
      return;
    }

    const container = document.createElement("div");
    container.id = "comet-control-feedback-mount";
    container.dataset.queueOrigin = QUEUE_ORIGIN;
    container.innerHTML = html;

    // Strip inline <script> blocks — they will not execute under MV3 isolated-world
    // CSP. The runtime is loaded separately via <script src>.
    container.querySelectorAll("script").forEach((s) => s.remove());

    document.body.appendChild(container);

    const runtime = document.createElement("script");
    runtime.src = chrome.runtime.getURL("content-scripts/feedback-widget-runtime.js");
    runtime.async = false;
    runtime.onerror = (e) => console.error("[comet-control-feedback] runtime load error", e);
    document.body.appendChild(runtime);

    console.log("[comet-control-feedback] widget mounted; queue origin:", QUEUE_ORIGIN);
  })();
})();
