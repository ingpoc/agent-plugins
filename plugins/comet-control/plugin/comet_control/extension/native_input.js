// Chrome 155 debugger policy fallback: real macOS input (CUAService via the broker),
// chrome.userScripts for arbitrary js on CSP-strict pages, and a window capture.
// Capabilities are detected once per lease; the path is chosen deterministically
// from them and logged into the broker flight recorder (input-paths.jsonl).
// Policy is never bypassed: no remote-debugging port, no profile edits.

const NATIVE_TIMEOUT_MS = 20_000;
const RESTORE_DELAY_MS = 600;
const pendingNative = new Map();
const leaseCaps = new Map();
let nativeTransport = null;
let nativeSeq = 0;

export const USER_SCRIPTS_HINT =
  "Allow User Scripts is off for Comet Control: open comet://extensions/?id=<Comet Control id> " +
  "(Details) and turn on \"Allow User Scripts\". Until then js() runs only where the page CSP allows eval.";

export function codedError(code, message, details = null) {
  const error = new Error(message);
  error.code = code;
  if (details) error.details = details;
  return error;
}

// ── broker transport ───────────────────────────────────────────────────────
export function setNativeTransport(send) {
  nativeTransport = send;
}

export function handleNativeResponse(message) {
  const entry = pendingNative.get(String(message?.id || ""));
  if (!entry) return false;
  pendingNative.delete(String(message.id));
  clearTimeout(entry.timer);
  if (message.success) entry.resolve(message);
  else entry.reject(codedError(message.error_code || "NATIVE_INPUT_FAILED", message.error || "native input failed", message.details || null));
  return true;
}

export function nativeRequest(op, sessionId, params = {}, timeoutMs = NATIVE_TIMEOUT_MS) {
  if (typeof nativeTransport !== "function") {
    return Promise.reject(codedError("NATIVE_INPUT_UNAVAILABLE", "broker transport is not ready"));
  }
  nativeSeq += 1;
  const id = `native-${Date.now()}-${nativeSeq}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingNative.delete(id);
      reject(codedError("NATIVE_INPUT_TIMEOUT", `native ${op} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    pendingNative.set(id, { resolve, reject, timer });
    const sent = nativeTransport({ type: "native_request", id, op, sessionId, params });
    if (!sent) {
      clearTimeout(timer);
      pendingNative.delete(id);
      reject(codedError("NATIVE_INPUT_UNAVAILABLE", "broker is disconnected"));
    }
  });
}

export function logPath(state, kind, path, extra = {}) {
  const sessionId = sessionOf(state);
  if (!sessionId) return;
  nativeRequest("log_path", sessionId, { kind, path, ...extra }, 5000).catch(() => {});
}

function sessionOf(state) {
  return String(state?.leaseRecord?.sessionId || state?.sessionId || "");
}

// ── capabilities (once per lease) ─────────────────────────────────────────
export async function userScriptsStatus(api = globalThis.chrome) {
  const declared = (api?.runtime?.getManifest?.()?.permissions || []).includes("userScripts");
  if (!declared) return { ok: false, reason: "USER_SCRIPTS_PERMISSION_MISSING" };
  if (!api.userScripts) return { ok: false, reason: "USER_SCRIPTS_DISABLED" };
  try {
    await api.userScripts.getScripts();
  } catch {
    return { ok: false, reason: "USER_SCRIPTS_DISABLED" };
  }
  if (typeof api.userScripts.execute !== "function") return { ok: false, reason: "USER_SCRIPTS_EXECUTE_UNSUPPORTED" };
  return { ok: true, reason: "" };
}

export async function leaseCapabilities(state, hooks) {
  const key = sessionOf(state) || `tab:${state.tabId}`;
  const cached = leaseCaps.get(key);
  if (cached) return cached;
  const probe = typeof hooks.probeDebugger === "function" ? await hooks.probeDebugger(state.tabId) : { ok: true };
  const us = await userScriptsStatus(hooks.chrome || globalThis.chrome);
  const caps = {
    debugger: Boolean(probe.ok),
    debugger_reason: probe.ok ? "" : String(probe.reason || "unavailable").slice(0, 160),
    user_scripts: us.ok,
    user_scripts_reason: us.reason,
    checked_at: Date.now(),
  };
  leaseCaps.set(key, caps);
  logPath(state, "capabilities", caps.debugger ? "debugger" : "no-debugger", {
    reason: caps.debugger ? (caps.user_scripts ? "userScripts" : caps.user_scripts_reason) : caps.debugger_reason,
  });
  return caps;
}

export function forgetLeaseCapabilities(sessionId) {
  leaseCaps.delete(String(sessionId || ""));
}

export function wantsRealMouse(action) {
  return action?.real_mouse === true || action?.trusted === "real" || action?.trusted === "os";
}

// Deterministic routing table. kind: type | key | click | screenshot | js
export function pickPath(caps, kind, action = {}) {
  const dbg = Boolean(caps?.debugger);
  if (kind === "type" || kind === "key") return dbg ? "cdp" : "cua_keyboard";
  if (kind === "click") return dbg ? "cdp" : wantsRealMouse(action) ? "cua_mouse" : "dom";
  if (kind === "screenshot") return dbg ? "cdp" : "tabs.captureVisibleTab";
  if (kind === "js") return dbg ? "cdp" : caps?.user_scripts ? "user_script" : "scripting_main";
  throw new Error(`unknown path kind ${kind}`);
}

// ── page helpers (run inside the leased tab via chrome.scripting) ─────────
function pageFocus(selector, point, requireEditable, caretEnd) {
  const deepActive = () => {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  };
  const editableOf = (node) => {
    let el = node;
    while (el && el !== document.body) {
      if (el instanceof HTMLTextAreaElement) return el;
      if (el instanceof HTMLInputElement && !["button", "submit", "reset", "checkbox", "radio", "file", "image", "range", "color", "hidden"].includes(el.type)) return el;
      if (el.isContentEditable) {
        let root = el;
        while (root.parentElement && root.parentElement.isContentEditable) root = root.parentElement;
        return root;
      }
      el = el.parentElement;
    }
    return null;
  };
  let el = null;
  if (selector) {
    el = document.querySelector(selector);
    if (!el) return { ok: false, code: "ELEMENT_NOT_FOUND", error: `no element for ${selector}` };
  } else if (point) {
    el = document.elementFromPoint(point.x, point.y);
  } else {
    el = deepActive();
  }
  const editable = el ? editableOf(el) : null;
  if (requireEditable && !editable) return { ok: false, code: "NOT_EDITABLE", error: "target is not a text input, textarea or contenteditable" };
  const target = editable || el || document.body;
  const r0 = target.getBoundingClientRect();
  if (r0.bottom < 0 || r0.top > innerHeight || r0.right < 0 || r0.left > innerWidth) target.scrollIntoView({ block: "center", inline: "nearest" });
  const wasFocused = deepActive() === target || Boolean(target.contains && target.contains(deepActive()));
  if (!wasFocused) target.focus({ preventScroll: true });
  // Already focused: keep the caret/selection (e.g. after Meta+a) like a real keyboard would.
  if (wasFocused) caretEnd = false;
  const kind = target instanceof HTMLInputElement ? "input" : target instanceof HTMLTextAreaElement ? "textarea" : target.isContentEditable ? "contenteditable" : "other";
  try {
    if (caretEnd && (kind === "input" || kind === "textarea")) {
      const n = target.value.length;
      target.setSelectionRange(n, n);
    } else if (caretEnd && kind === "contenteditable") {
      const range = document.createRange();
      range.selectNodeContents(target);
      range.collapse(false);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  } catch {}
  const active = deepActive();
  const focused = active === target || Boolean(target.contains && target.contains(active));
  const value = kind === "input" || kind === "textarea" ? target.value : kind === "contenteditable" ? target.innerText : "";
  for (const stale of document.querySelectorAll("[data-comet-native-target]")) if (stale !== target) stale.removeAttribute("data-comet-native-target");
  if (!target.hasAttribute("data-comet-native-target")) target.setAttribute("data-comet-native-target", "1");
  const r = target.getBoundingClientRect();
  return {
    ok: true, focused, kind, tag: target.tagName.toLowerCase(), page_focus: document.hasFocus(),
    before: String(value || ""), before_len: String(value || "").length,
    selectionStart: target.selectionStart, selectionEnd: target.selectionEnd,
    rect: { x: r.left, y: r.top, w: r.width, h: r.height }, vw: innerWidth, vh: innerHeight,
  };
}

function pageReadTarget(cleanup) {
  const el = document.querySelector("[data-comet-native-target]");
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  const out = { page_focus: document.hasFocus(), found: Boolean(el) };
  if (el) {
    const kind = el instanceof HTMLInputElement ? "input" : el instanceof HTMLTextAreaElement ? "textarea" : el.isContentEditable ? "contenteditable" : "other";
    out.value = String(kind === "input" || kind === "textarea" ? el.value : kind === "contenteditable" ? el.innerText : "");
    out.focused = a === el || Boolean(el.contains(a));
    if (cleanup) el.removeAttribute("data-comet-native-target");
  }
  out.active = a ? `${a.tagName.toLowerCase()}${a.id ? `#${a.id}` : ""}` : "";
  return out;
}

function pageHitTest(selector, point) {
  const el = selector ? document.querySelector(selector) : null;
  let x = point?.x;
  let y = point?.y;
  if (el) {
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) el.scrollIntoView({ block: "center", inline: "nearest" });
    const r2 = el.getBoundingClientRect();
    if (x == null || y == null) {
      x = r2.left + r2.width / 2;
      y = r2.top + r2.height / 2;
    }
  }
  if (!Number.isFinite(x) || !Number.isFinite(y)) return { ok: false, code: "ELEMENT_NOT_FOUND", error: "no click target" };
  const hit = document.elementFromPoint(x, y);
  const occluded = Boolean(el && hit && hit !== el && !el.contains(hit) && !hit.contains(el));
  return { ok: true, x, y, vw: innerWidth, vh: innerHeight, occluded, hit: hit ? hit.tagName.toLowerCase() : "" };
}

export function normalizeText(value) {
  return String(value ?? "").normalize("NFC").replace(/\u00a0/g, " ").replace(/\r\n?/g, "\n").replace(/\u200b/g, "");
}

export function typedValueMatches({ before = "", after = "", text = "", clear = false, kind = "input", selectionStart = before.length, selectionEnd = selectionStart }) {
  const a = normalizeText(after);
  const t = normalizeText(text);
  if (kind === "contenteditable") {
    const flat = (s) => s.replace(/\s+/g, " ").trim();
    return clear ? flat(a) === flat(t) : (!t || flat(a) !== flat(normalizeText(before))) && flat(a).includes(flat(t));
  }
  if (clear) return a === t;
  const start = selectionStart ?? before.length;
  const end = selectionEnd ?? start;
  return a === normalizeText(before.slice(0, start) + text + before.slice(end));
}

// Screen point (macOS global points) for a client point in the leased tab's top frame.
export function screenPointFor(bounds, client, viewport, zoom = 1) {
  const z = Number(zoom) > 0 ? Number(zoom) : 1;
  const insetX = Math.max(0, (bounds.width - viewport.vw * z) / 2);
  const toolbar = Math.max(0, bounds.height - viewport.vh * z);
  return { x: bounds.left + insetX + client.x * z, y: bounds.top + toolbar + client.y * z };
}

async function inTab(hooks, tabId, frameId, func, args, label) {
  const frames = await hooks.executeScriptOnTab(
    tabId, { target: { tabId, frameIds: [frameId || 0] }, func, args }, 5000, label,
  );
  return frames?.[0]?.result;
}

async function leaseWindow(state, api = globalThis.chrome) {
  const tab = await api.tabs.get(state.tabId);
  const windowId = state.leaseRecord?.windowId || tab.windowId;
  if (Number(tab.windowId) !== Number(windowId)) throw codedError("LEASE_TAB_SCOPED", "leased tab left its leased window");
  if (!tab.active) await api.tabs.update(state.tabId, { active: true });
  const win = await api.windows.get(windowId);
  return { tab, bounds: { left: win.left, top: win.top, width: win.width, height: win.height } };
}

async function raiseLease(state, hooks, label) {
  const sessionId = sessionOf(state);
  if (!sessionId) throw codedError("LEASE_REQUIRED", "real input is lease-scoped");
  const api = hooks.chrome || globalThis.chrome;
  const { tab, bounds } = await leaseWindow(state, api);
  const raised = await nativeRequest("raise", sessionId, { window: bounds, title: tab.title || "", action: label });
  return { sessionId, windowId: raised.window_id, bounds, tab };
}

async function release(sessionId) {
  await nativeRequest("release", sessionId, { delay_ms: RESTORE_DELAY_MS }, 5000).catch(() => {});
}

// ── flows ─────────────────────────────────────────────────────────────────
// Focus via scripting -> raise the exact leased window -> confirm page focus ->
// CUAService keys/text -> verify through a scripting read -> optional Enter.
export async function nativeTypeFlow(state, hooks, opts) {
  const t0 = Date.now();
  const text = String(opts.text ?? "");
  const clear = Boolean(opts.clear);
  const frameId = opts.frameId || 0;
  const timings = {};
  const focus = await inTab(hooks, state.tabId, frameId, pageFocus,
    [opts.selector || null, opts.point || null, true, !clear], "nativeFocus");
  if (!focus?.ok) throw codedError(focus?.code || "FOCUS_FAILED", focus?.error || "could not focus the target");
  timings.focus = Date.now() - t0;
  const lease = await raiseLease(state, hooks, opts.label || "type");
  try {
    timings.raise = Date.now() - t0;
    let check = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "nativeFocusCheck");
    if (!check?.focused) {
      await inTab(hooks, state.tabId, frameId, pageFocus, [opts.selector || null, opts.point || null, true, !clear], "nativeRefocus");
      check = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "nativeFocusCheck");
    }
    if (!check?.page_focus || !check?.focused) {
      throw codedError("FOCUS_NOT_IN_PAGE", "after raising the leased window the page target is not focused; nothing typed",
        { page_focus: Boolean(check?.page_focus), active: check?.active || "" });
    }
    const steps = [];
    if (clear) steps.push({ kind: "key", key: "Meta+a" }, { kind: "key", key: "Backspace" });
    if (text) steps.push({ kind: "text", text, mode: opts.mode || "auto" });
    let typed = { steps: [] };
    if (steps.length) typed = await nativeRequest("steps", lease.sessionId, { window_id: lease.windowId, steps });
    timings.steps = Date.now() - t0;
    const textStep = (typed.steps || []).find((s) => s.kind === "text");
    let after = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "nativeVerify");
    let verified = typedValueMatches({ ...focus, after: after?.value, text, clear });
    if (!verified) {
      await new Promise((r) => setTimeout(r, 150));
      after = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "nativeVerify");
      verified = typedValueMatches({ ...focus, after: after?.value, text, clear });
    }
    let recovered = "";
    if (!verified && textStep?.mode === "keystrokes" && (clear || !focus.before_len)) {
      // Fast keystrokes can drop characters while a SPA re-renders (x.com search):
      // retype once atomically through the clipboard (Cmd+V).
      await nativeRequest("steps", lease.sessionId, { window_id: lease.windowId, steps: [
        { kind: "key", key: "Meta+a" }, { kind: "key", key: "Backspace" }, { kind: "text", text, mode: "paste" },
      ] });
      after = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "nativeVerify");
      verified = typedValueMatches({ before: "", after: after?.value, text, clear: true, kind: focus.kind });
      recovered = "paste";
    }
    timings.verify = Date.now() - t0;
    if (!verified && opts.verify !== false) {
      throw codedError("TYPE_NOT_VERIFIED", "real-keyboard text did not land in the target",
        { kind: focus.kind, expected_len: text.length, before_len: focus.before_len, after_len: String(after?.value || "").length, text_mode: textStep?.mode || "" });
    }
    if (opts.submit) {
      await nativeRequest("steps", lease.sessionId, { window_id: lease.windowId, steps: [{ kind: "key", key: "Enter" }] });
    }
    for (const key of opts.keysAfter || []) {
      await nativeRequest("steps", lease.sessionId, { window_id: lease.windowId, steps: [{ kind: "key", key }] });
    }
    timings.total = Date.now() - t0;
    logPath(state, "type", "cua_keyboard", { action: opts.label || "type", reason: textStep?.mode || (clear ? "clear" : "") });
    return {
      input_path: "cua_keyboard", text_mode: textStep?.mode || "none", verified, target_kind: focus.kind,
      ...(recovered ? { recovered_via: recovered } : {}),
      value_length: String(after?.value || "").length, submitted: Boolean(opts.submit), timings_ms: timings,
    };
  } finally {
    await inTab(hooks, state.tabId, frameId, pageReadTarget, [true], "nativeCleanup").catch(() => {});
    await release(lease.sessionId);
  }
}

export async function nativeKeyFlow(state, hooks, opts) {
  const t0 = Date.now();
  const frameId = opts.frameId || 0;
  if (opts.selector || opts.point) {
    const focus = await inTab(hooks, state.tabId, frameId, pageFocus, [opts.selector || null, opts.point || null, false, false], "nativeFocus");
    if (!focus?.ok) throw codedError(focus?.code || "FOCUS_FAILED", focus?.error || "could not focus the target");
  }
  const lease = await raiseLease(state, hooks, opts.label || "key");
  try {
    const check = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "nativeFocusCheck");
    if (!check?.page_focus) throw codedError("FOCUS_NOT_IN_PAGE", "leased page does not have keyboard focus; key not sent", { active: check?.active || "" });
    const keys = Array.isArray(opts.keys) ? opts.keys : [{ key: opts.key, modifiers: opts.modifiers || [] }];
    const out = await nativeRequest("steps", lease.sessionId, {
      window_id: lease.windowId,
      steps: keys.map((k) => ({ kind: "key", key: String(k.key), modifiers: k.modifiers || [] })),
    });
    const after = await inTab(hooks, state.tabId, frameId, pageReadTarget, [true], "nativeKeyAfter").catch(() => null);
    logPath(state, "key", "cua_keyboard", { action: opts.label || "key" });
    return { input_path: "cua_keyboard", keys: (out.steps || []).map((s) => s.key), active_after: after?.active || "", timings_ms: { total: Date.now() - t0 } };
  } finally {
    await release(lease.sessionId);
  }
}

export async function nativeClickFlow(state, hooks, opts) {
  const t0 = Date.now();
  if (opts.frameId && Number(opts.frameId) !== 0) throw codedError("REAL_MOUSE_FRAME", "real-mouse click supports the top frame only");
  const api = hooks.chrome || globalThis.chrome;
  const before = await inTab(hooks, state.tabId, 0, pageHitTest, [opts.selector || null, opts.point || null], "nativeHitTest");
  if (!before?.ok) throw codedError(before?.code || "ELEMENT_NOT_FOUND", before?.error || "no click target");
  if (before.occluded) throw codedError("ACTIONABILITY_OCCLUDED", `click target is covered by <${before.hit}>`);
  const lease = await raiseLease(state, hooks, opts.label || "click");
  try {
    const now = await inTab(hooks, state.tabId, 0, pageHitTest, [opts.selector || null, opts.point || null], "nativeHitTest");
    if (!now?.ok || now.occluded) throw codedError("ACTIONABILITY_UNSTABLE", "click target moved or became covered after raise");
    const zoom = await api.tabs.getZoom(state.tabId).catch(() => 1);
    const screen = screenPointFor(lease.bounds, { x: now.x, y: now.y }, now, zoom);
    const out = await nativeRequest("steps", lease.sessionId, {
      window_id: lease.windowId, steps: [{ kind: "click", x: screen.x, y: screen.y, count: opts.count || 1 }],
    });
    logPath(state, "click", "cua_mouse", { action: opts.label || "click" });
    return {
      input_path: "cua_mouse", trusted: true, clicked: true, client: { x: now.x, y: now.y },
      screen: { x: Math.round(screen.x), y: Math.round(screen.y) }, zoom, method: out.steps?.[0]?.method || "",
      timings_ms: { total: Date.now() - t0 },
    };
  } finally {
    await release(lease.sessionId);
  }
}

export async function nativeCapture(state, hooks, opts = {}) {
  const sessionId = sessionOf(state);
  if (!sessionId) throw codedError("LEASE_REQUIRED", "window capture is lease-scoped");
  const api = hooks.chrome || globalThis.chrome;
  const { tab, bounds } = await leaseWindow(state, api);
  const out = await nativeRequest("capture", sessionId, { window: bounds, title: tab.title || "", inline: Boolean(opts.inline) }, 25_000);
  return { screenshot_path: out.screenshot_path, format: out.format, base64: out.base64, window_width: out.window_width, window_height: out.window_height };
}

// ── js(): userScripts (USER_SCRIPT world) -> scripting MAIN eval -> error ──
function userScriptCode(body) {
  return `(async () => { ${body}\n})().then((v) => { if (v === undefined) return { ok: true, type: "undefined" };` +
    ` try { return { ok: true, type: typeof v, value: JSON.parse(JSON.stringify(v)) }; } catch (e) { return { ok: true, type: typeof v, value: String(v) }; } },` +
    ` (e) => ({ ok: false, name: String((e && e.name) || "Error"), message: String((e && e.message) || e) }))`;
}

export async function runUserScript(tabId, expression, { world = "USER_SCRIPT", api = globalThis.chrome } = {}) {
  const src = String(expression || "").trim().replace(/;+\s*$/, "");
  const attempt = async (body) => {
    const frames = await api.userScripts.execute({
      target: { tabId, frameIds: [0] }, world: world === "MAIN" ? "MAIN" : "USER_SCRIPT",
      injectImmediately: true, js: [{ code: userScriptCode(body) }],
    });
    const frame = frames?.[0] || {};
    if (frame.error) return { ok: false, name: "Error", message: String(frame.error) };
    return frame.result || { ok: false, name: "Error", message: "userScripts returned no result" };
  };
  let out = await attempt(`return (\n${src}\n);`);
  if (out.ok === false && /SyntaxError|Unexpected token|Unexpected identifier|missing \)/i.test(`${out.name} ${out.message}`)) {
    out = await attempt(src);
  }
  return out;
}

// Runtime.evaluate-shaped result: { result: {type, value}, exceptionDetails? }
export async function evaluateWithoutDebugger(state, hooks, expression, caps, { awaitPromise = true, world } = {}) {
  const api = hooks.chrome || globalThis.chrome;
  const pack = (out, path) => {
    logPath(state, "js", path);
    if (!out || out.ok === false) {
      const text = out ? `${out.name}: ${out.message}` : "evaluate returned no result";
      return { path, result: { type: "undefined" }, exceptionDetails: { text, exception: { type: "object", description: text } } };
    }
    return { path, result: out.type === "undefined" ? { type: "undefined" } : { type: out.type, value: out.value } };
  };
  if (caps?.user_scripts) {
    try {
      return pack(await runUserScript(state.tabId, expression, { world, api }), "user_script");
    } catch (error) {
      if (!/user ?scripts?|Allow User Scripts/i.test(String(error?.message || error))) throw error;
      caps.user_scripts = false;
      caps.user_scripts_reason = "USER_SCRIPTS_DISABLED";
    }
  }
  const frames = await hooks.executeScriptOnTab(state.tabId, {
    target: { tabId: state.tabId }, world: "MAIN",
    func: (src, wait) => {
      const packv = (value) => {
        if (value === undefined) return { ok: true, type: "undefined" };
        try { return { ok: true, type: typeof value, value: JSON.parse(JSON.stringify(value)) }; } catch { return { ok: true, type: typeof value, value: String(value) }; }
      };
      const fail = (e) => ({ ok: false, name: String((e && e.name) || "Error"), message: String((e && e.message) || e) });
      try {
        const value = (0, eval)(src);
        if (wait && value && typeof value.then === "function") return value.then(packv, fail);
        return packv(value);
      } catch (e) { return fail(e); }
    },
    args: [String(expression || ""), awaitPromise !== false],
  }, 5000, "evaluate (no debugger)");
  const out = frames?.[0]?.result;
  if (out?.ok === false && /EvalError|unsafe-eval|Content Security Policy/i.test(`${out.name} ${out.message}`)) {
    const reason = caps?.user_scripts_reason || "USER_SCRIPTS_DISABLED";
    return {
      ...pack({ ok: false, name: reason, message: `page CSP forbids eval and userScripts is unavailable (${reason}). ${USER_SCRIPTS_HINT}` }, "scripting_main"),
      error_code: reason,
    };
  }
  return pack(out, "scripting_main");
}
