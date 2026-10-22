// Chrome 155 debugger policy: the leased tab's Browser Use CDP surface falls back to
// chrome.scripting / chrome.tabs (no debugger), fast, and never bypasses the policy.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const updates = [];
globalThis.chrome = { tabs: { update: async (tabId, props) => { updates.push([tabId, props]); }, reload: async () => {} } };
const mod = await import(pathToFileURL(path.resolve(process.argv[2])).href);
const { runParityAction } = mod;

const page = { location: { href: "https://x.com/compose/articles" }, document: { title: "Articles / X", readyState: "complete" } };
function hooks({ cspBlocksEval = false } = {}) {
  const calls = { send: 0, scripts: 0 };
  return {
    calls,
    send: async () => { calls.send += 1; throw new Error("Host access is restricted by policy."); },
    executeScriptOnTab: async (_tabId, options) => {
      calls.scripts += 1;
      assert.equal(options.world, "MAIN");
      const realEval = globalThis.eval;
      const saved = {};
      for (const k of ["location", "document"]) { saved[k] = globalThis[k]; globalThis[k] = page[k]; }
      if (cspBlocksEval) globalThis.eval = () => { throw new EvalError("Refused to evaluate a string as JavaScript because 'unsafe-eval' is not an allowed source of script"); };
      try {
        return [{ result: await options.func(...(options.args || [])) }];
      } finally {
        globalThis.eval = realEval;
        for (const k of Object.keys(saved)) globalThis[k] = saved[k];
      }
    },
  };
}
const cdp = (method, params = {}) => ({ type: "browser_use_cdp", method, params });

// 1) location.href through the policy block (eval allowed by the page).
let h = hooks();
let r = await runParityAction(cdp("Runtime.evaluate", { expression: "location.href", returnByValue: true }), { tabId: 1 }, h);
assert.equal(r.result.result.result.value, "https://x.com/compose/articles");
assert.equal(r.result.fallback, "policy");
assert.equal(h.calls.send, 1);
// The block is remembered: the next call does not retry the debugger.
r = await runParityAction(cdp("Runtime.evaluate", { expression: "document.title" }), { tabId: 1 }, h);
assert.equal(r.result.result.result.value, "Articles / X");
assert.equal(h.calls.send, 1);

// 2) Page CSP forbids eval: the fixed reads still answer, others report the error.
h = hooks({ cspBlocksEval: true });
r = await runParityAction(cdp("Runtime.evaluate", { expression: "location.href" }), { tabId: 2 }, h);
assert.equal(r.result.result.result.value, "https://x.com/compose/articles");
r = await runParityAction(cdp("Runtime.evaluate", { expression: "1+1" }), { tabId: 2 }, h);
assert.match(r.result.result.exceptionDetails.text, /unsafe-eval/);

// 3) Script exceptions come back as exceptionDetails, promises are awaited.
h = hooks();
r = await runParityAction(cdp("Runtime.evaluate", { expression: "Promise.resolve(41+1)", awaitPromise: true }), { tabId: 3 }, h);
assert.equal(r.result.result.result.value, 42);
r = await runParityAction(cdp("Runtime.evaluate", { expression: "null.x" }), { tabId: 3 }, h);
assert.match(r.result.result.exceptionDetails.text, /TypeError/);

// 4) enable is a no-op, navigate uses chrome.tabs, debugger-only methods fail fast.
assert.deepEqual((await runParityAction(cdp("Page.enable"), { tabId: 3 }, h)).result.result, {});
await runParityAction(cdp("Page.navigate", { url: "https://x.com/compose/articles" }), { tabId: 3 }, h);
assert.deepEqual(updates.at(-1), [3, { url: "https://x.com/compose/articles" }]);
await assert.rejects(runParityAction(cdp("Page.navigate", { url: "javascript:alert(1)" }), { tabId: 3 }, h));
await assert.rejects(runParityAction(cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "a" }), { tabId: 3 }, h), /needs chrome\.debugger/);

// 5) Other debugger errors are not swallowed.
const other = { send: async () => { throw new Error("Cannot access a chrome:// URL"); }, executeScriptOnTab: async () => assert.fail("no fallback") };
await assert.rejects(runParityAction(cdp("Runtime.evaluate", { expression: "1" }), { tabId: 9 }, other), /chrome:\/\//);
console.log("policy fallback OK");
