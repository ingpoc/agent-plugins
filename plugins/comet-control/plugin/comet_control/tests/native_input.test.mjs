// Chrome 155 debugger policy: routing to real input (CUAService via broker), userScripts js(),
// and window capture. Usage: node native_input.test.mjs <extension dir>
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";
import vm from "node:vm";

const ext = path.resolve(process.argv[2]);
const tabs = { 7: { id: 7, windowId: 70, active: true, title: "ACU input matrix", url: "http://127.0.0.1/form.html" } };
const chromeFake = {
  runtime: { getManifest: () => ({ permissions: ["scripting", "userScripts"] }) },
  tabs: {
    get: async (id) => ({ ...tabs[id] }),
    update: async (id, props) => Object.assign(tabs[id], props),
    getZoom: async () => 1,
    captureVisibleTab: async () => { throw new Error("capture failed"); },
  },
  windows: {
    get: async () => ({ left: 2560, top: 317, width: 1920, height: 1050 }),
    update: async (_id, props) => ({ left: 2560, top: 317, width: 1920, height: 1050, ...props }),
  },
  userScripts: undefined,
  scripting: { executeScript: async () => [{ result: null }] },
};
globalThis.chrome = chromeFake;
const ni = await import(pathToFileURL(path.join(ext, "native_input.js")).href);
const parity = await import(pathToFileURL(path.join(ext, "parity_capabilities.js")).href);

// ── 1) deterministic routing table
const blocked = { debugger: false, user_scripts: false };
const blockedUS = { debugger: false, user_scripts: true };
const open = { debugger: true, user_scripts: true };
assert.equal(ni.pickPath(blocked, "type"), "cua_keyboard");
assert.equal(ni.pickPath(blocked, "key"), "cua_keyboard");
assert.equal(ni.pickPath(open, "type"), "cdp");
assert.equal(ni.pickPath(blocked, "click"), "dom");
assert.equal(ni.pickPath(blocked, "click", { real_mouse: true }), "cua_mouse");
assert.equal(ni.pickPath(blocked, "click", { trusted: "real" }), "cua_mouse");
assert.equal(ni.pickPath(open, "click", { real_mouse: true }), "cdp");
assert.equal(ni.pickPath(blocked, "screenshot"), "tabs.captureVisibleTab");
assert.equal(ni.pickPath(blocked, "js"), "scripting_main");
assert.equal(ni.pickPath(blockedUS, "js"), "user_script");
assert.equal(ni.pickPath(open, "js"), "cdp");

// ── 2) value verification + screen geometry
assert.ok(ni.typedValueMatches({ before: "", after: "Héllo 😀", text: "He\u0301llo 😀", clear: true }));
assert.ok(ni.typedValueMatches({ before: "ab", after: "abcd", text: "cd" }));
assert.ok(!ni.typedValueMatches({ before: "hello", after: "hello", text: "hello" }));
assert.ok(!ni.typedValueMatches({ before: "hello", after: "hello", text: "hello", kind: "contenteditable" }));
assert.ok(!ni.typedValueMatches({ before: "hello", after: "hellox", text: "hello" }));
assert.ok(ni.typedValueMatches({ before: "hello world", after: "hello there", text: "there", selectionStart: 6, selectionEnd: 11 }));
assert.ok(!ni.typedValueMatches({ before: "old", after: "oldnew", text: "new", clear: true }));
assert.ok(ni.typedValueMatches({ kind: "contenteditable", after: "hi\u00a0there\n", text: "hi there", clear: true }));
assert.deepEqual(ni.screenPointFor({ left: 2560, top: 317, width: 1920, height: 1050 }, { x: 100, y: 50 }, { vw: 1920, vh: 963 }, 1), { x: 2660, y: 454 });
assert.deepEqual(ni.screenPointFor({ left: 0, top: 0, width: 1000, height: 800 }, { x: 10, y: 10 }, { vw: 800, vh: 560 }, 1.25), { x: 12.5, y: 112.5 });

// ── 3) userScripts detection
assert.equal((await ni.userScriptsStatus({ runtime: { getManifest: () => ({ permissions: [] }) } })).reason, "USER_SCRIPTS_PERMISSION_MISSING");
assert.equal((await ni.userScriptsStatus(chromeFake)).reason, "USER_SCRIPTS_DISABLED");
const throwing = { runtime: chromeFake.runtime, userScripts: { getScripts: async () => { throw new Error("toggle off"); }, execute: async () => [] } };
assert.equal((await ni.userScriptsStatus(throwing)).reason, "USER_SCRIPTS_DISABLED");
const ok = { runtime: chromeFake.runtime, userScripts: { getScripts: async () => [], execute: async () => [] } };
assert.equal((await ni.userScriptsStatus(ok)).ok, true);
const old = { runtime: chromeFake.runtime, userScripts: { getScripts: async () => [] } };
assert.equal((await ni.userScriptsStatus(old)).reason, "USER_SCRIPTS_EXECUTE_UNSUPPORTED");

// ── fake broker + page
const log = [];
let page;
let failPrepareRaise = false;
function resetPage(kind = "input", value = "") {
  page = { kind, value, focused: false, pageFocus: false, raised: false };
}
ni.setNativeTransport((msg) => {
  log.push(msg.op === "steps" ? `steps:${msg.params.steps.map((s) => s.kind === "text" ? `text(${s.text})` : s.kind === "key" ? s.key : `click@${Math.round(s.x)},${Math.round(s.y)}`).join(",")}` : msg.op);
  assert.equal(msg.sessionId, "lease-1");
  queueMicrotask(() => {
    let reply = { id: msg.id, success: true };
    if (msg.op === "prepare_raise" && failPrepareRaise) {
      reply = { id: msg.id, success: false, error_code: "PREPARE_FAILED", error: "prepare failed" };
    } else if (msg.op === "raise") {
      assert.deepEqual(msg.params.window, { left: 2560, top: 317, width: 1920, height: 1050 });
      assert.equal(msg.params.title, "ACU input matrix");
      page.raised = true;
      page.pageFocus = page.blockPageFocus ? false : true;
      reply.window_id = 18599;
      if (globalThis.__raiseFrame) reply.frame = globalThis.__raiseFrame;
    } else if (msg.op === "steps") {
      assert.equal(msg.params.window_id, 18599);
      for (const s of msg.params.steps) {
        if (s.key === "Meta+a") page.selectAll = true;
        else if (s.key === "Backspace" && page.selectAll) { page.value = ""; page.selectAll = false; }
        else if (s.kind === "text" && page.dropOnce && s.mode !== "paste") { page.value += s.text.slice(0, -2); page.dropOnce = false; }
        else if (s.kind === "text" && !page.dropText) page.value += s.text;
      }
      reply.steps = msg.params.steps.map((s) => (s.kind === "text" ? { kind: "text", mode: s.mode === "paste" ? "paste" : "keystrokes", length: s.text.length } : { kind: s.kind, key: s.key, method: "cgevent-click-pid" }));
    } else if (msg.op === "capture") {
      reply = { ...reply, screenshot_path: "/x/cua.png", format: "png", base64: msg.params.inline ? "QUJD" : undefined };
    }
    ni.handleNativeResponse(reply);
  });
  return true;
});
const hooks = {
  chrome: chromeFake,
  probeDebugger: async () => ({ ok: false, reason: "policy" }),
  executeScriptOnTab: async (_tabId, options, _ms, label) => {
    log.push(`page:${label}`);
    const fn = options.func.name;
    if (fn === "pageFocus") { page.focused = true; return [{ result: { ok: true, focused: true, kind: page.kind, tag: "input", page_focus: page.pageFocus, before: page.value, before_len: page.value.length, rect: { x: 10, y: 10, w: 100, h: 20 }, vw: 1920, vh: 963 } }]; }
    if (fn === "pageReadTarget") return [{ result: { page_focus: page.pageFocus, found: true, value: page.value, focused: page.focused, active: "input#name" } }];
    if (fn === "pageHitTest") return [{ result: { ok: true, x: 100, y: 50, vw: 1920, vh: 963, occluded: false, hit: "button" } }];
    throw new Error(`unexpected page fn ${fn}`);
  },
};
const state = { tabId: 7, leaseRecord: { sessionId: "lease-1", windowId: 70 } };

resetPage();
let visibilityReads = 0;
const visibilityHooks = { ...hooks, executeScriptOnTab: async (_tabId, options) => {
  assert.deepEqual(options.target.frameIds, [0]);
  return [{ result: ++visibilityReads === 1 ? "hidden" : "visible" }];
} };
log.length = 0;
const raised = await ni.nativeBringToFront(state, visibilityHooks, { holdMs: 0 });
assert.equal(raised.visible, true);
assert.equal(raised.input_path, "window_focus");
assert.equal(raised.hold_ms, 0);
assert.deepEqual(log.filter((op) => ["prepare_raise", "raise", "release"].includes(op)), ["prepare_raise", "release"]);
log.length = 0;
await assert.rejects(ni.nativeBringToFront(state, { ...hooks, executeScriptOnTab: async () => { throw new Error("poll failed"); } }, { holdMs: 0 }), /poll failed/);
assert.deepEqual(log.filter((op) => ["prepare_raise", "raise", "release"].includes(op)), ["prepare_raise", "release"]);
await assert.rejects(ni.nativeBringToFront({ tabId: 7 }, hooks, { holdMs: 0 }), { code: "LEASE_REQUIRED" });

// ── 4) capability detection runs once per lease
let probes = 0;
const countingHooks = { ...hooks, probeDebugger: async () => { probes += 1; return { ok: false, reason: "policy" }; } };
const c1 = await ni.leaseCapabilities(state, countingHooks);
const c2 = await ni.leaseCapabilities(state, countingHooks);
assert.equal(probes, 1);
assert.equal(c1, c2);
assert.equal(c1.debugger, false);
assert.equal(c1.user_scripts_reason, "USER_SCRIPTS_DISABLED");
ni.forgetLeaseCapabilities("lease-1");
await ni.leaseCapabilities(state, countingHooks);
assert.equal(probes, 2);

// ── 5) type flow order: focus -> prepare_raise -> raise -> focus check -> steps -> verify -> cleanup -> release
resetPage("input", "old");
log.length = 0;
let r = await ni.nativeTypeFlow(state, hooks, { selector: "#name", text: "Héllo 😀", clear: true, submit: true });
assert.equal(r.verified, true);
assert.equal(r.input_path, "cua_keyboard");
assert.deepEqual(log.filter((l) => !l.startsWith("log_path")), [
  "page:nativeFocus", "prepare_raise", "raise", "page:nativeFocusCheck", "steps:Meta+a,Backspace,text(Héllo 😀)",
  "page:nativeVerify", "steps:Enter", "page:nativeCleanup", "release",
]);

failPrepareRaise = true;
log.length = 0;
await assert.rejects(ni.nativeKeyFlow(state, hooks, { key: "Tab" }), (e) => e.code === "PREPARE_FAILED");
failPrepareRaise = false;
assert.deepEqual(log.filter((l) => !l.startsWith("log_path")), ["prepare_raise", "release"]);

// ── 6) page lost keyboard focus after raise (omnibox) -> nothing typed, still released
resetPage("input", "");
page.blockPageFocus = true;
log.length = 0;
await assert.rejects(ni.nativeTypeFlow(state, hooks, { selector: "#name", text: "x" }), (e) => e.code === "FOCUS_NOT_IN_PAGE");
assert.ok(!log.some((l) => l.startsWith("steps")));
assert.equal(log.at(-1), "release");

// ── 7) text that never lands -> TYPE_NOT_VERIFIED (no Enter sent)
resetPage("input", "");
page.dropText = true;
log.length = 0;
await assert.rejects(ni.nativeTypeFlow(state, hooks, { selector: "#name", text: "abc", submit: true }), (e) => e.code === "TYPE_NOT_VERIFIED" && e.details.expected_len === 3);
assert.ok(!log.includes("steps:Enter"));

resetPage("input", "hello");
page.dropText = true;
log.length = 0;
await assert.rejects(ni.nativeTypeFlow(state, hooks, { selector: "#name", text: "hello", submit: true }), (e) => e.code === "TYPE_NOT_VERIFIED");
assert.ok(!log.includes("steps:Enter"));

// Exercise the actual page helpers: their old 4k/8k truncation escaped the mocked flow.
class LongInput {
  constructor() { this.tagName = "INPUT"; this.type = "text"; this.marked = false; }
  get value() { return page.value; }
  contains(el) { return el === this; }
  focus() {}
  getBoundingClientRect() { return { top: 10, bottom: 30, left: 10, right: 110, x: 10, y: 10, width: 100, height: 20 }; }
  hasAttribute() { return this.marked; }
  setAttribute() { this.marked = true; }
  removeAttribute() { this.marked = false; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
}
const longInput = new LongInput();
const pageSandbox = {
  HTMLInputElement: LongInput, HTMLTextAreaElement: class {}, innerWidth: 1920, innerHeight: 963,
  document: {
    activeElement: longInput, body: {}, hasFocus: () => page.pageFocus,
    querySelector: (selector) => selector === "#name" || longInput.marked ? longInput : null,
    querySelectorAll: () => longInput.marked ? [longInput] : [],
  },
};
const realPageHooks = { ...hooks, executeScriptOnTab: async (_tabId, options) => [{ result:
  vm.runInNewContext(`(${options.func.toString()})(...args)`, { ...pageSandbox, args: options.args }),
}] };
resetPage("input", "");
r = await ni.nativeTypeFlow(state, realPageHooks, { selector: "#name", text: "x".repeat(9001), clear: true, mode: "paste" });
assert.equal(r.verified, true);
assert.equal(r.value_length, 9001);
resetPage("input", "x".repeat(5000));
longInput.setSelectionRange(5000, 5000);
r = await ni.nativeTypeFlow(state, realPageHooks, { selector: "#name", text: "END", mode: "paste" });
assert.equal(r.verified, true);
assert.equal(r.value_length, 5003);

// ── 7b) keystrokes that drop characters are retyped once via paste (clear / empty field only)
resetPage("input", "");
page.dropOnce = true;
log.length = 0;
r = await ni.nativeTypeFlow(state, hooks, { selector: "#q", text: "comet browser", clear: true });
assert.equal(r.verified, true);
assert.equal(r.recovered_via, "paste");
assert.ok(log.includes("steps:Meta+a,Backspace,text(comet browser)"));

// ── 8) key + real-mouse click + capture
resetPage();
log.length = 0;
r = await ni.nativeKeyFlow(state, hooks, { key: "Tab" });
assert.deepEqual(log.filter((l) => !l.startsWith("log_path")), ["prepare_raise", "raise", "page:nativeFocusCheck", "steps:Tab", "page:nativeKeyAfter", "release"]);
log.length = 0;
r = await ni.nativeClickFlow(state, hooks, { selector: "#trusted" });
assert.ok(log.includes("steps:click@2660,454"));
// macOS/Stage Manager clamps the real window 20px shorter than Chrome reports: map with the real frame.
globalThis.__raiseFrame = { left: 2560, top: 317, width: 1920, height: 1030 };
log.length = 0;
await ni.nativeClickFlow(state, hooks, { selector: "#trusted" });
assert.ok(log.includes("steps:click@2660,434"), log.join(" "));
globalThis.__raiseFrame = undefined;
assert.equal(r.trusted, true);
await assert.rejects(ni.nativeClickFlow(state, hooks, { point: { x: 1, y: 1 }, frameId: 3 }), (e) => e.code === "REAL_MOUSE_FRAME");
r = await ni.nativeCapture(state, hooks, { inline: true });
assert.equal(r.base64, "QUJD");

// ── 9) js(): userScripts USER_SCRIPT world, expression then statement form; CSP error code without it
let executed = [];
const usChrome = { ...chromeFake, userScripts: { getScripts: async () => [], execute: async (spec) => {
  executed.push(spec);
  assert.equal(spec.world, "USER_SCRIPT");
  if (spec.js[0].code.includes("return (\nlet a")) return [{ error: "SyntaxError: Unexpected identifier 'a'" }];
  return [{ result: { ok: true, type: "string", value: "X" } }];
} } };
let out = await ni.evaluateWithoutDebugger(state, { ...hooks, chrome: usChrome }, "document.title", { user_scripts: true });
assert.equal(out.path, "user_script");
assert.equal(out.result.value, "X");
executed = [];
out = await ni.runUserScript(7, "let a = 1; return document.title;", { api: usChrome });
assert.equal(executed.length, 2);
const cspHooks = { ...hooks, executeScriptOnTab: async () => [{ result: { ok: false, name: "EvalError", message: "Refused to evaluate a string as JavaScript because 'unsafe-eval' is not an allowed source" } }] };
out = await ni.evaluateWithoutDebugger(state, cspHooks, "document.title", { user_scripts: false, user_scripts_reason: "USER_SCRIPTS_DISABLED" });
assert.equal(out.error_code, "USER_SCRIPTS_DISABLED");
assert.match(out.exceptionDetails.text, /Allow User Scripts/);

// ── 10) parity: locator type / press route to the real keyboard when the lease has no debugger
const parityHooks = {
  ...hooks,
  send: async () => { throw new Error("Host access is restricted by policy."); },
  moveCursorToPoint: async () => {},
  leaseCapabilities: async () => ({ debugger: false, user_scripts: false, user_scripts_reason: "USER_SCRIPTS_DISABLED" }),
  nativeHooks: hooks,
};
chromeFake.scripting.executeScript = async () => [{ result: [{ selector: "#name", frame_id: 0, point: { x: 50, y: 20 }, visible: true, enabled: true, name: "name", role: "textbox", text: "", value: "" }] }];
resetPage("input", "");
log.length = 0;
let pr = await parity.runParityAction({ type: "locator", operation: "type", locator: { by: "css", selector: "#name" }, value: "hey" }, state, parityHooks);
assert.equal(pr.result.input_path, "cua_keyboard");
assert.ok(log.includes("steps:text(hey)"));
log.length = 0;
pr = await parity.runParityAction({ type: "locator", operation: "press", locator: { by: "css", selector: "#name" }, key: "Enter" }, state, parityHooks);
assert.ok(log.includes("steps:Enter"));

// ── 11) Browser Use CDP: Input.insertText / keyDown / click / screenshot / js without the debugger
resetPage("input", "");
log.length = 0;
const cdp = (method, params = {}) => ({ type: "browser_use_cdp", method, params });
pr = await parity.runParityAction(cdp("Page.bringToFront", { holdMs: 0 }), state, { ...parityHooks, nativeHooks: visibilityHooks });
assert.equal(pr.result.fallback, "policy");
assert.equal(pr.result.result.input_path, "window_focus");
assert.equal(pr.result.result.visible, true);
pr = await parity.runParityAction(cdp("Input.insertText", { text: "abc" }), state, parityHooks);
assert.equal(pr.result.fallback, "policy");
assert.equal(pr.result.result.input_path, "cua_keyboard");
pr = await parity.runParityAction(cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "a", modifiers: 4 }), state, parityHooks);
assert.ok(log.includes("steps:Meta+a") || log.some((l) => l.startsWith("steps:")));
assert.deepEqual((await parity.runParityAction(cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "a" }), state, parityHooks)).result.result, {});
log.length = 0;
await parity.runParityAction(cdp("Input.dispatchMouseEvent", { type: "mousePressed", x: 100, y: 50, clickCount: 1 }), state, parityHooks);
await parity.runParityAction(cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x: 100, y: 50, clickCount: 1 }), state, parityHooks);
assert.ok(log.some((l) => l.startsWith("steps:click@")));
pr = await parity.runParityAction(cdp("Page.captureScreenshot", { format: "png" }), state, parityHooks);
assert.equal(pr.result.result.data, "QUJD");
await assert.rejects(parity.runParityAction(cdp("DOM.getDocument"), state, parityHooks), /DOM\.\* has none/);
console.log("native input OK");
