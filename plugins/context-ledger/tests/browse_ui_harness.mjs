import { readFileSync } from "node:fs";

const html = readFileSync(process.argv[2], "utf8");
const start = html.indexOf("<script>");
const end = html.lastIndexOf("</script>");
if (start < 0 || end < 0) {
  console.error("browse.html has no script");
  process.exit(1);
}
const code = html.slice(start + "<script>".length, end);

const posts = [];
const parent = {
  postMessage(message) {
    posts.push(message);
  },
};

class El {
  constructor() {
    this.listeners = {};
    this.classes = new Set();
    this.dataset = {};
    this.value = "";
    this.innerHTML = "";
    this.textContent = "";
    this.classList = {
      contains: (name) => this.classes.has(name),
      toggle: (name) => {
        if (this.classes.has(name)) this.classes.delete(name);
        else this.classes.add(name);
      },
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
    };
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  dispatch(type, event) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
}

const ids = ["q", "list", "detail", "count", "chips", "superseded"];
const els = Object.fromEntries(ids.map((id) => [id, new El()]));
const document = {
  listeners: {},
  documentElement: { scrollWidth: 800, scrollHeight: 600 },
  getElementById: (id) => els[id] || null,
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  },
  dispatch(type, event) {
    for (const fn of this.listeners[type] || []) fn(event);
  },
};
const window = {
  listeners: {},
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  },
  dispatch(type, event) {
    for (const fn of this.listeners[type] || []) fn(event);
  },
};

const timers = [];
let now = 0;
let seq = 1;
function setTimeout(fn, ms) {
  const id = seq++;
  timers.push({ id, at: now + (ms || 0), fn });
  return id;
}
function clearTimeout(id) {
  const index = timers.findIndex((timer) => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
}
function flush(ms) {
  now += ms;
  for (let guard = 0; guard < 50; guard += 1) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const next = timers.find((timer) => timer.at <= now);
    if (!next) return;
    timers.splice(timers.indexOf(next), 1);
    next.fn();
  }
  throw new Error("timer flush did not settle");
}

new Function("window", "document", "parent", "setTimeout", "clearTimeout", code)(
  window,
  document,
  parent,
  setTimeout,
  clearTimeout,
);

function calls(name) {
  return posts.filter((message) => message.method === "tools/call" && message.params && message.params.name === name);
}
function reply(id, result) {
  window.dispatch("message", { source: parent, data: { jsonrpc: "2.0", id, result } });
}
function fail(id, message) {
  window.dispatch("message", { source: parent, data: { jsonrpc: "2.0", id, error: { message } } });
}
function click(el, selector, extra) {
  el.dispatch("click", {
    target: {
      closest(query) {
        return query === selector ? extra || {} : null;
      },
    },
  });
}
function row(id, summary) {
  return {
    decision_id: id,
    summary,
    outcome: "pending",
    lifecycle: "active",
    entities: [],
  };
}
function browseResult(matches, hasMore) {
  return { structuredContent: { matches, has_more: hasMore } };
}
function getResult(summary) {
  return {
    structuredContent: {
      record: {
        body: { summary },
        outcome: { status: "pending" },
        lifecycle: "active",
        revision: 1,
        events: [],
      },
    },
  };
}
async function tick() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

const alpha = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const beta = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const gamma = "cccccccc-cccc-cccc-cccc-cccccccccccc";
const report = {};

try {
  const init = posts.find((message) => message.method === "ui/initialize");
  reply(init.id, {});
  await tick();
  flush(60);
  const first = calls("browse").at(-1);
  reply(first.id, browseResult([row(alpha, "alpha"), row(beta, "beta")], true));
  await tick();
  const getAlpha = calls("get").at(-1);
  click(els.list, "[data-i]", { dataset: { i: "1" } });
  const getBeta = calls("get").at(-1);
  reply(getBeta.id, getResult("detail-bee"));
  await tick();
  reply(getAlpha.id, getResult("detail-ay"));
  await tick();
  report.staleDetail = els.detail.innerHTML.includes("detail-bee") && !els.detail.innerHTML.includes("detail-ay");

  click(els.list, "#more");
  const more = calls("browse").at(-1);
  report.laterOffset = more.params.arguments.offset === 2 && more.params.arguments.limit === 12;
  reply(more.id, browseResult([row(gamma, "gamma-page")], false));
  await tick();
  report.laterPage =
    els.list.innerHTML.includes("alpha") &&
    els.list.innerHTML.includes("beta") &&
    els.list.innerHTML.includes("gamma-page") &&
    !els.list.innerHTML.includes('id="more"');

  els.q.value = "beta";
  els.q.dispatch("input", {});
  flush(200);
  const betaBrowse = calls("browse").at(-1);
  els.q.value = "gamma";
  els.q.dispatch("input", {});
  flush(200);
  const gammaBrowse = calls("browse").at(-1);
  reply(gammaBrowse.id, browseResult([row(gamma, "gamma-row")], false));
  await tick();
  reply(betaBrowse.id, browseResult([row(beta, "old-row")], false));
  await tick();
  const staleBeforeError = els.list.innerHTML.includes("gamma-row") && !els.list.innerHTML.includes("old-row");
  els.q.value = "delta";
  els.q.dispatch("input", {});
  flush(200);
  const deltaBrowse = calls("browse").at(-1);
  els.q.value = "gamma-kept";
  els.q.dispatch("input", {});
  flush(200);
  const keptBrowse = calls("browse").at(-1);
  reply(keptBrowse.id, browseResult([row(gamma, "kept-row")], false));
  await tick();
  fail(deltaBrowse.id, "STALE");
  await tick();
  report.staleFilter = staleBeforeError && els.list.innerHTML.includes("kept-row") && !els.list.innerHTML.includes("STALE");

  els.q.value = "boom";
  els.q.dispatch("input", {});
  flush(200);
  const boom = calls("browse").at(-1);
  const pendingGet = calls("get").at(-1);
  fail(boom.id, "UNAVAILABLE");
  await tick();
  reply(pendingGet.id, getResult("stale-detail"));
  await tick();
  report.failedBrowse =
    els.list.innerHTML.includes("UNAVAILABLE") &&
    els.list.innerHTML.includes('id="retry"') &&
    els.count.textContent === "Unavailable" &&
    els.detail.innerHTML.includes("Select a decision") &&
    !els.detail.innerHTML.includes("stale-detail") &&
    !els.list.innerHTML.includes("kept-row");
  document.dispatch("keydown", { key: "ArrowDown", target: els.list, preventDefault() {} });
  await tick();
  report.failedArrow =
    els.list.innerHTML.includes("UNAVAILABLE") && !els.list.innerHTML.includes("kept-row");

  els.q.value = "slow";
  els.q.dispatch("input", {});
  flush(200);
  const slow = calls("browse").at(-1);
  flush(8000);
  await tick();
  report.timedOut = els.list.innerHTML.includes("TIMEOUT") && els.count.textContent === "Unavailable";
  reply(slow.id, browseResult([row(alpha, "late-row")], false));
  await tick();
  report.lateIgnored = els.list.innerHTML.includes("TIMEOUT") && !els.list.innerHTML.includes("late-row");
  click(els.list, "#retry");
  const retry = calls("browse").at(-1);
  report.retried = retry.id !== slow.id && retry.params.arguments.query === "slow" && retry.params.arguments.offset === 0;
  reply(retry.id, browseResult([row(alpha, "retry-row")], false));
  await tick();
  report.retryShows = els.list.innerHTML.includes("retry-row");
  const detailWait = calls("get").at(-1);
  flush(8000);
  await tick();
  report.detailTimeout = els.detail.innerHTML.includes("TIMEOUT") && els.detail.innerHTML.includes('id="retry-detail"');
  click(els.detail, "#retry-detail");
  const detailRetry = calls("get").at(-1);
  report.detailRetried = detailRetry.id !== detailWait.id;
  reply(detailRetry.id, getResult("detail-again"));
  await tick();
  report.detailRetryShows = els.detail.innerHTML.includes("detail-again");

  const failed = Object.entries(report).filter((entry) => entry[1] !== true);
  process.stdout.write(JSON.stringify(report));
  if (failed.length) {
    console.error(failed.map((entry) => entry[0]).join(","));
    process.exit(1);
  }
} catch (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
}
