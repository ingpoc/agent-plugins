// Chrome 155 debugger policy fallback: IN-PAGE input only (DOM events in the leased tab,
// drawn with comet-control's own cursor overlay), chrome.userScripts for arbitrary js on
// CSP-strict pages, and a window capture. Launch rule: comet-control never moves the Mac's
// real mouse or sends real keys; Agent Computer Use is for Mac operations outside the browser.
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

// Retired: `real_mouse` / `trusted:"real"` no longer reach the OS mouse. Kept so old
// callers get the in-page path instead of an error.
export function wantsRealMouse(_action) {
  return false;
}

// Deterministic routing table. kind: type | key | click | screenshot | js
export function pickPath(caps, kind, _action = {}) {
  const dbg = Boolean(caps?.debugger);
  if (kind === "type" || kind === "key" || kind === "click") return dbg ? "cdp" : "in_page";
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

async function inTab(hooks, tabId, frameId, func, args, label, world) {
  const options = { target: { tabId, frameIds: [frameId || 0] }, func, args };
  // MAIN world: editors (React/Draft.js) read clipboardData and event fields from page objects.
  if (world) options.world = world;
  const frames = await hooks.executeScriptOnTab(tabId, options, 10_000, label);
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

async function release(sessionId) {
  await nativeRequest("release", sessionId, { delay_ms: RESTORE_DELAY_MS }, 5000).catch(() => {});
}

// ── flows ─────────────────────────────────────────────────────────────────
// Debugger-free raise for an occluded (document.hidden) leased window. Uses Chrome's own
// window focus, not CUA bounds matching: Comet can report window bounds that differ from the
// CG window (e.g. 1920x1050 vs 1900x1030), which made native "raise" fail WINDOW_NOT_FOUND.
// prepare_raise/release keep the user's front app restore contract.
export async function nativeBringToFront(state, hooks, { holdMs = 1500, label = "bring_to_front" } = {}) {
  const sessionId = sessionOf(state);
  if (!sessionId) throw codedError("LEASE_REQUIRED", "bring-to-front is lease-scoped");
  const api = hooks.chrome || globalThis.chrome;
  await nativeRequest("prepare_raise", sessionId).catch(() => {});
  try {
    const { tab } = await leaseWindow(state, api);
    await api.windows.update(tab.windowId, { focused: true });
    const start = Date.now();
    let visible = false;
    while (true) {
      visible = await inTab(hooks, state.tabId, 0, () => document.visibilityState, [], "visibilityState") === "visible";
      if (visible || Date.now() - start >= 3000) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    const visibility_wait_ms = Date.now() - start;
    const hold_ms = Number.isFinite(Number(holdMs)) ? Math.max(0, Math.min(10000, Number(holdMs))) : 0;
    if (hold_ms) await new Promise((resolve) => setTimeout(resolve, hold_ms));
    logPath(state, "window", "window_focus", { action: label });
    return { input_path: "window_focus", visible, visibility_wait_ms, hold_ms };
  } finally {
    await release(sessionId);
  }
}

// ── in-page page functions (run in the leased tab, MAIN world) ────────────
// Methods, in auto order, for contenteditable editors. Each is tried only while the
// target is still unchanged, so a method that worked is never layered with another.
//   beforeinput: InputEvent("beforeinput", insertText) — Lexical/ProseMirror/Slate handle it.
//   paste:       ClipboardEvent("paste") with text/plain (+ text/html) — Draft.js handlePastedText.
//   textinput:   TextEvent("textInput") — React onBeforeInput (Draft.js typing path) and Chrome's
//                own insertion when nobody prevents it.
//   execcommand: document.execCommand("insertText" | "insertHTML").
export const IN_PAGE_TEXT_METHODS = ["beforeinput", "paste", "textinput", "execcommand"];

async function pageInsertText(text, clear, method, html) {
  const target = document.querySelector("[data-comet-native-target]");
  if (!target) return { ok: false, code: "TARGET_LOST", error: "focused target disappeared" };
  const deepActive = () => {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  };
  const kind = target instanceof HTMLInputElement ? "input" : target instanceof HTMLTextAreaElement ? "textarea" : target.isContentEditable ? "contenteditable" : "other";
  const read = () => String(kind === "input" || kind === "textarea" ? target.value : target.innerText || "");
  const settle = () => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 60)));
  if (deepActive() !== target && !target.contains(deepActive())) target.focus({ preventScroll: true });
  if (clear) {
    if (kind === "input" || kind === "textarea") target.select();
    else {
      const range = document.createRange();
      range.selectNodeContents(target);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }
  const before = read();
  const tried = [];
  const sink = () => deepActive() || target;
  const attempt = {
    beforeinput: () => sink().dispatchEvent(new InputEvent("beforeinput", { inputType: html ? "insertFromPaste" : "insertText", data: text, bubbles: true, cancelable: true, composed: true })),
    paste: () => {
      const data = new DataTransfer();
      data.setData("text/plain", text);
      if (html) data.setData("text/html", html);
      sink().dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true, composed: true }));
    },
    textinput: () => {
      const event = document.createEvent("TextEvent");
      event.initTextEvent("textInput", true, true, window, text);
      sink().dispatchEvent(event);
    },
    execcommand: () => document.execCommand(html ? "insertHTML" : "insertText", false, html || text),
    setter: () => {
      const proto = kind === "input" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      const setValue = Object.getOwnPropertyDescriptor(proto, "value").set;
      const start = clear ? 0 : target.selectionStart ?? before.length;
      const end = clear ? before.length : target.selectionEnd ?? start;
      setValue.call(target, before.slice(0, start) + text + before.slice(end));
      target.dispatchEvent(new InputEvent("input", { inputType: "insertText", data: text, bubbles: true }));
    },
  };
  const order = method && method !== "auto" ? [method]
    : kind === "input" || kind === "textarea" ? ["execcommand", "setter"] : ["beforeinput", "paste", "textinput", "execcommand"];
  let used = "";
  for (const name of order) {
    if (!attempt[name]) return { ok: false, code: "UNKNOWN_METHOD", error: `unknown in-page method ${name}` };
    tried.push(name);
    try { attempt[name](); } catch (error) { continue; }
    await settle();
    if (read() !== before || (!text && clear)) { used = name; break; }
  }
  return { ok: true, kind, method: used, tried, before, after: read() };
}

async function pageKey(key, modifiers) {
  const mods = (modifiers || []).map((m) => String(m).toLowerCase());
  let k = String(key || "");
  if (k.includes("+") && k.length > 1) {
    const parts = k.split("+");
    k = parts.pop();
    mods.push(...parts.map((m) => m.toLowerCase()));
  }
  const names = { enter: "Enter", return: "Enter", tab: "Tab", escape: "Escape", esc: "Escape", backspace: "Backspace", delete: "Delete", space: " ",
    arrowleft: "ArrowLeft", arrowright: "ArrowRight", arrowup: "ArrowUp", arrowdown: "ArrowDown", home: "Home", end: "End" };
  k = names[k.toLowerCase()] || k;
  const codes = { Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, " ": 32, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35 };
  const keyCode = codes[k] || (k.length === 1 ? k.toUpperCase().charCodeAt(0) : 0);
  const meta = mods.some((m) => ["meta", "cmd", "command"].includes(m));
  const ctrl = mods.some((m) => ["ctrl", "control"].includes(m));
  const init = { key: k, code: k.length === 1 ? (/[a-z]/i.test(k) ? `Key${k.toUpperCase()}` : "") : k === " " ? "Space" : k,
    bubbles: true, cancelable: true, composed: true, metaKey: meta, ctrlKey: ctrl,
    shiftKey: mods.includes("shift"), altKey: mods.some((m) => ["alt", "option"].includes(m)) };
  let el = document.activeElement || document.body;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  const fire = (type) => {
    const event = new KeyboardEvent(type, init);
    // Draft.js and older handlers key on keyCode/which; KeyboardEvent's constructor cannot set them.
    Object.defineProperty(event, "keyCode", { get: () => keyCode });
    Object.defineProperty(event, "which", { get: () => keyCode });
    return el.dispatchEvent(event);
  };
  const notPrevented = fire("keydown");
  let default_action = "";
  if (notPrevented) {
    const editable = el.isContentEditable || el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement);
    if ((meta || ctrl) && k.toLowerCase() === "a") { document.execCommand("selectAll"); default_action = "selectAll"; }
    else if (k === "Enter" && el.isContentEditable) { document.execCommand("insertParagraph"); default_action = "insertParagraph"; }
    else if (k === "Enter" && el instanceof HTMLTextAreaElement) { document.execCommand("insertText", false, "\n"); default_action = "newline"; }
    else if (k === "Enter" && el instanceof HTMLInputElement && el.form) { el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); default_action = "submit"; }
    else if (k === "Backspace" && editable) { document.execCommand("delete"); default_action = "delete"; }
    else if (k === "Delete" && editable) { document.execCommand("forwardDelete"); default_action = "forwardDelete"; }
    else if (k === "Tab") {
      const focusable = [...document.querySelectorAll("a[href],button,input,select,textarea,[tabindex]:not([tabindex='-1']),[contenteditable='true']")]
        .filter((n) => !n.disabled && n.getClientRects().length);
      const i = focusable.indexOf(el);
      const next = focusable[(i + (init.shiftKey ? -1 : 1) + focusable.length) % focusable.length];
      if (next) { next.focus(); default_action = "focus"; }
    } else if (k.length === 1 && !meta && !ctrl && editable) {
      fire("keypress");
      document.execCommand("insertText", false, k);
      default_action = "insertText";
    }
  }
  fire("keyup");
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  return { ok: true, key: k, modifiers: mods, prevented: !notPrevented, default_action, active: a ? `${a.tagName.toLowerCase()}${a.id ? `#${a.id}` : ""}` : "" };
}

function pageClick(selector, point, count) {
  let el = selector ? document.querySelector(selector) : null;
  let x = point?.x;
  let y = point?.y;
  if (el && (x == null || y == null)) {
    const r = el.getBoundingClientRect();
    x = r.left + r.width / 2;
    y = r.top + r.height / 2;
  }
  if (!Number.isFinite(x) || !Number.isFinite(y)) return { ok: false, code: "ELEMENT_NOT_FOUND", error: "no click target" };
  const hit = document.elementFromPoint(x, y);
  if (el && hit && hit !== el && !el.contains(hit) && !hit.contains(el)) return { ok: false, code: "ACTIONABILITY_OCCLUDED", error: `click target is covered by <${hit.tagName.toLowerCase()}>` };
  el = el || hit;
  if (!el) return { ok: false, code: "ELEMENT_NOT_FOUND", error: "nothing at that point" };
  const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, button: 0 };
  for (let i = 1; i <= Math.max(1, count || 1); i += 1) {
    el.dispatchEvent(new PointerEvent("pointerdown", { ...base, pointerId: 1, pointerType: "mouse", isPrimary: true, buttons: 1, detail: i }));
    el.dispatchEvent(new MouseEvent("mousedown", { ...base, buttons: 1, detail: i }));
    if (i === 1 && typeof el.focus === "function") el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", { ...base, pointerId: 1, pointerType: "mouse", isPrimary: true, detail: i }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...base, detail: i }));
    el.dispatchEvent(new MouseEvent("click", { ...base, detail: i }));
  }
  if ((count || 1) >= 2) el.dispatchEvent(new MouseEvent("dblclick", { ...base, detail: 2 }));
  return { ok: true, x, y, tag: el.tagName.toLowerCase() };
}

async function showCursor(hooks, tabId, frameId, point, label) {
  // The in-page cursor overlay is drawn for every in-page action that has a point.
  if (!point || typeof hooks.sendToContentScript !== "function") return false;
  await hooks.sendToContentScript(tabId, "moveToAndWait", [point.x, point.y, 600], frameId || 0).catch(() => {});
  await hooks.sendToContentScript(tabId, "pulseClick", [], frameId || 0).catch(() => {});
  if (label) await hooks.sendToContentScript(tabId, "showKey", [label], frameId || 0).catch(() => {});
  return true;
}

// ── flows (in-page only) ────────────────────────────────────────────────────
// Names kept for callers; nothing here touches CUAService input.
export async function nativeTypeFlow(state, hooks, opts) {
  const t0 = Date.now();
  const text = String(opts.text ?? "");
  const clear = Boolean(opts.clear);
  const frameId = opts.frameId || 0;
  const focus = await inTab(hooks, state.tabId, frameId, pageFocus,
    [opts.selector || null, opts.point || null, true, !clear], "inPageFocus");
  if (!focus?.ok) throw codedError(focus?.code || "FOCUS_FAILED", focus?.error || "could not focus the target");
  try {
    const r = focus.rect;
    if (r && frameId === 0) await showCursor(hooks, state.tabId, frameId, { x: r.x + Math.min(r.w / 2, 24), y: r.y + r.h / 2 }, "type");
    const typed = await inTab(hooks, state.tabId, frameId, pageInsertText,
      [text, clear, opts.method || opts.mode || "auto", opts.html || null], "inPageType", "MAIN");
    if (!typed?.ok) throw codedError(typed?.code || "TYPE_FAILED", typed?.error || "in-page typing failed");
    const after = await inTab(hooks, state.tabId, frameId, pageReadTarget, [false], "inPageVerify");
    const verified = opts.html ? normalizeText(after?.value) !== normalizeText(focus.before)
      : typedValueMatches({ ...focus, after: after?.value, text, clear });
    if (!verified && opts.verify !== false) {
      throw codedError("TYPE_NOT_VERIFIED", "in-page text did not land in the target",
        { kind: focus.kind, expected_len: text.length, before_len: focus.before_len, after_len: String(after?.value || "").length, tried: typed.tried || [] });
    }
    if (opts.submit) await nativeKeyFlow(state, hooks, { key: "Enter", frameId, label: "submit" });
    for (const key of opts.keysAfter || []) await nativeKeyFlow(state, hooks, { key, frameId, label: "key" });
    logPath(state, "type", "in_page", { action: opts.label || "type", reason: typed.method || "" });
    return {
      input_path: "in_page", text_mode: typed.method || "none", tried: typed.tried || [], verified, target_kind: focus.kind,
      value_length: String(after?.value || "").length, submitted: Boolean(opts.submit), timings_ms: { total: Date.now() - t0 },
    };
  } finally {
    await inTab(hooks, state.tabId, frameId, pageReadTarget, [true], "inPageCleanup").catch(() => {});
  }
}

export async function nativeKeyFlow(state, hooks, opts) {
  const t0 = Date.now();
  const frameId = opts.frameId || 0;
  if (opts.selector || opts.point) {
    const focus = await inTab(hooks, state.tabId, frameId, pageFocus, [opts.selector || null, opts.point || null, false, false], "inPageFocus");
    if (!focus?.ok) throw codedError(focus?.code || "FOCUS_FAILED", focus?.error || "could not focus the target");
    await inTab(hooks, state.tabId, frameId, pageReadTarget, [true], "inPageCleanup").catch(() => {});
  }
  const keys = Array.isArray(opts.keys) ? opts.keys : [{ key: opts.key, modifiers: opts.modifiers || [] }];
  const results = [];
  for (const k of keys) {
    if (typeof hooks.sendToContentScript === "function") await hooks.sendToContentScript(state.tabId, "showKey", [String(k.key)], frameId).catch(() => {});
    results.push(await inTab(hooks, state.tabId, frameId, pageKey, [String(k.key), k.modifiers || []], "inPageKey", "MAIN"));
  }
  logPath(state, "key", "in_page", { action: opts.label || "key" });
  return { input_path: "in_page", keys: results.map((r) => r?.key || ""), default_actions: results.map((r) => r?.default_action || ""),
    active_after: results.at(-1)?.active || "", timings_ms: { total: Date.now() - t0 } };
}

export async function nativeClickFlow(state, hooks, opts) {
  const t0 = Date.now();
  const frameId = opts.frameId || 0;
  const before = await inTab(hooks, state.tabId, frameId, pageHitTest, [opts.selector || null, opts.point || null], "inPageHitTest");
  if (!before?.ok) throw codedError(before?.code || "ELEMENT_NOT_FOUND", before?.error || "no click target");
  if (before.occluded) throw codedError("ACTIONABILITY_OCCLUDED", `click target is covered by <${before.hit}>`);
  const cursor = await showCursor(hooks, state.tabId, frameId, { x: before.x, y: before.y });
  const out = await inTab(hooks, state.tabId, frameId, pageClick, [opts.selector || null, { x: before.x, y: before.y }, opts.count || 1], "inPageClick", "MAIN");
  if (!out?.ok) throw codedError(out?.code || "CLICK_FAILED", out?.error || "in-page click failed");
  logPath(state, "click", "in_page", { action: opts.label || "click" });
  return { input_path: "in_page", trusted: false, clicked: true, client: { x: out.x, y: out.y }, tag: out.tag, cursor_drawn: cursor,
    timings_ms: { total: Date.now() - t0 } };
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
