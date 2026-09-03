// Offline behavior check: node plugin/comet_control/tests/test_locator_scrolling.mjs [source.js]
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(process.argv[2] || new URL("../extension/parity_capabilities.js", import.meta.url), "utf8");

function fixture() {
  const scrolls = [], moves = [], events = [];
  let downloaded = false;
  const element = (id, top, height = 40) => ({
    id, top, height, tagName: "DIV", nodeType: 1, parentElement: null,
    innerText: id, value: id, src: `https://example.com/${id}.png`,
    getAttribute: () => null, closest: () => null, contains(node) { return node === this.child; },
    getBoundingClientRect() { return { left: 20, right: 420, width: 400, top: this.top, bottom: this.top + this.height, height: this.height }; },
    scrollIntoView(options) {
      scrolls.push({ id, ...options });
      if (this.top >= 600) this.top = 100;
    },
  });
  const a = element("a", 1400), b = element("b", 2000);
  const document = {
    activeElement: null,
    querySelectorAll(selector) { return selector === "#a" ? [a] : selector === "#b" ? [b] : [a, b]; },
    querySelector(selector) { return this.querySelectorAll(selector)[0]; },
  };
  const context = vm.createContext({
    document, window: { innerWidth: 800, innerHeight: 600 },
    CSS: { escape: (value) => value }, Node: { ELEMENT_NODE: 1 },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1", position: "static" }),
    setTimeout: (fn, ms) => setTimeout(fn, ms).unref(),
    chrome: {
      scripting: { executeScript: async ({ func, args }) => [{ result: func(...args) }] },
      downloads: {
        download: async () => { downloaded = true; return 1; },
        search: async () => downloaded ? [{ id: 1, state: "complete", filename: "/tmp/fixture.png", url: a.src }] : [],
      },
    },
  });
  vm.runInContext(source.replace(/^export /gm, ""), context);
  const api = vm.runInContext("({locatorAction, downloadAction})", context);
  const hooks = {
    moveCursorToPoint: async (_, point) => moves.push(point),
    send: async (_, method, params) => { events.push({ method, ...params }); if (params.type === "mouseReleased") downloaded = true; },
  };
  const run = (operation, extra = {}) => api.locatorAction({ operation, locator: { selector: "#a" }, key: "Enter", ...extra }, { tabId: 1 }, hooks);
  const download = (type, locator = { selector: "#a" }) => api.downloadAction({ type, locator }, { tabId: 1 }, hooks);
  return { a, b, document, scrolls, moves, events, hooks, run, download };
}

const checks = [
  ["locator reads leave every match and viewport untouched", async () => {
    const f = fixture();
    for (const operation of ["count", "inspect", "all_text", "inner_text", "value", "is_visible", "wait"]) await f.run(operation);
    await f.run("inspect", { locator: { selector: ".item" } });
    assert.equal(f.scrolls.length, 0);
    assert.equal(f.moves.length, 0);
  }],
  ["focused tall-editor press/type preserve the caret and viewport", async () => {
    const f = fixture();
    f.a.top = -1000; f.a.height = 5000; f.a.child = {}; f.document.activeElement = f.a.child;
    await f.run("press"); await f.run("type", { text: "continued paragraph" });
    assert.equal(f.scrolls.length, 0);
    assert.equal(f.moves.length, 0);
    assert.equal(f.events.filter((e) => e.method === "Input.dispatchMouseEvent").length, 0);
    assert.equal(f.events.filter((e) => e.method === "Input.dispatchKeyEvent").length, 2);
    assert.equal(f.events.at(-1).text, "continued paragraph");
  }],
  ["unfocused press intentionally scrolls and clicks only its chosen target", async () => {
    const f = fixture(); await f.run("press");
    assert.equal(f.scrolls.length, 1); assert.equal(f.scrolls[0].id, "a");
    assert.equal(f.scrolls[0].block, "nearest");
    assert(f.moves.every((p) => p.y >= 0 && p.y < 600));
    assert(f.events.some((e) => e.type === "mouseReleased"));
  }],
  ["tall unfocused editor gets visible coordinates", async () => {
    const f = fixture(); f.a.top = -1000; f.a.height = 5000;
    await f.run("type", { text: "hello" });
    assert(f.moves.every((p) => p.y >= 0 && p.y < 600));
  }],
  ["ambiguous interactive locator fails before any movement", async () => {
    const f = fixture();
    await assert.rejects(f.run("click", { locator: { selector: ".item" } }), /one target/);
    assert.equal(f.scrolls.length + f.moves.length + f.events.length, 0);
    await f.run("click", { locator: { selector: ".item", last: true } });
    assert.equal(f.scrolls.length, 1); assert.equal(f.scrolls[0].id, "b");
  }],
  ["download_media is pure; download_click scrolls the selected target", async () => {
    const media = fixture(); await media.download("download_media");
    assert.equal(media.scrolls.length + media.moves.length, 0);
    const click = fixture(); await click.download("download_click");
    assert.equal(click.scrolls.length, 1); assert.equal(click.scrolls[0].id, "a");
    const ambiguous = fixture();
    await assert.rejects(ambiguous.download("download_click", { selector: ".item" }), /one target/);
    assert.equal(ambiguous.scrolls.length + ambiguous.events.length, 0);
  }],
  ["installed End/Home keys and bounded fill remain supported", async () => {
    const f = fixture(); f.document.activeElement = f.a;
    for (const [key, code] of [["End", 35], ["Home", 36]]) {
      await f.run("press", { key });
      assert.equal(f.events.at(-1).windowsVirtualKeyCode, code);
    }
    await assert.rejects(f.run("fill", { value: "replacement" }), /requires bounded executeScriptOnTab/);
    f.hooks.executeScriptOnTab = async (tabId, options, timeout, label) => {
      assert.equal(tabId, 1); assert.equal(timeout, 8000); assert.equal(label, "locatorFill");
      assert.equal(options.args[1], "replacement");
      return [{ result: "replacement" }];
    };
    assert.equal((await f.run("fill", { value: "replacement" })).value, "replacement");
  }],
];
let failures = 0;
for (const [name, check] of checks) {
  try { await check(); console.log(`PASS ${name}`); }
  catch (error) { failures++; console.error(`FAIL ${name}: ${error.message}`); }
}
if (failures) process.exitCode = 1;
