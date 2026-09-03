import {
  cleanupParityTab,
  dismissParityDialog,
  evaluateReadOnly,
  getParityDialog,
  listUserTabs,
  pressKey,
  raceClickWithDialog,
  readBrowserHistory,
  recordParityCdpEvent,
  runParityAction,
} from "./parity_capabilities.js";

const BROKER_URL = "ws://127.0.0.1:38927";
const PROTOCOL_VERSION = 1;
const PAIRING_SECRET_KEY = "comet-control-broker-pairing-secret-v1";
const CONTROL_PAUSED_KEY = "comet-control-control-paused-v1";
const EXTENSION_CAPABILITIES = Object.freeze([
  "capability-negotiation",
  "console-network-diagnostics",
  "cua-handoff",
  "dialogs-files-clipboard",
  "isolated-window-leases",
  "operator-pause",
  "raw-cdp",
  "screenshots",
  "visible-agent-cursor",
]);
const PAUSE_ALLOWED_HOST_TYPES = new Set([
  "cua_runtime_release",
  "session_closeout",
  "session_renew",
  "sessions",
  "status",
]);
let port = null;
let connecting = false;
let brokerReady = false;
let brokerInfo = {};
let reconnectTimer = null;
let controlPaused = false;
const activeRunStates = new Set();
const attachedTabs = new Set();
const injectedTabs = new Set();
// Tabs where executeScript timed out client-side but Comet may still hold the op.
// Further injects stack zombies; recover via sendMessage probe or navigation only.
const tabScriptingPoisoned = new Set();
// Tabs whose content script answered getStatus but then failed a real action.
const contentScriptForceReinjection = new Set();
const sessionLeases = new Map(); // opaque sessionId → persisted owned window/tab lease
const sessionQueues = new Map(); // sessionId → same-session FIFO lifecycle queue
let agentWindowLayoutQueue = Promise.resolve(); // one display-layout owner across concurrent leases
let deferredAgentWindowLayoutReason = null; // autonomous events mark only; host-locked requests apply
let viewportCaptureQueue = Promise.resolve(); // captureVisibleTab is active-window based; serialize cross-session proof
let lastViewportCaptureStartedAt = 0;
const tabConsoleCdp = new Map(); // tabId → ring buffer from CDP Runtime events
const tabNetworkCdp = new Map(); // tabId → opt-in compact Network diagnostics
const SESSION_STORAGE_KEY = "comet-control-agent-session-leases-v1";
const LEASE_REMOVAL_STORAGE_KEY = "comet-control-agent-lease-removals-v1";
const CUA_CLAIM_STORAGE_KEY = "comet-control-cua-runtime-claim-v1";
const controlPauseReady = chrome.storage.local.get(CONTROL_PAUSED_KEY)
  .then((stored) => { controlPaused = stored[CONTROL_PAUSED_KEY] === true; })
  .catch(() => { controlPaused = false; });
const extensionBuildSha256 = Promise.all([
  "service_worker.js",
  "parity_capabilities.js",
  "content-scripts/cursor-agent.js",
].map(async (path) => {
  const response = await fetch(chrome.runtime.getURL(path));
  if (!response.ok) throw new Error(`Could not read extension build: ${path} ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}))
  .then((parts) => {
    const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    for (const part of parts) {
      bytes.set(part, offset);
      offset += part.length;
    }
    return crypto.subtle.digest("SHA-256", bytes);
  })
  .then((digest) => Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""));

async function pairingSecret() {
  const stored = await chrome.storage.local.get(PAIRING_SECRET_KEY);
  const existing = String(stored[PAIRING_SECRET_KEY] || "");
  if (/^[0-9a-f]{64}$/.test(existing)) return existing;
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const generated = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  await chrome.storage.local.set({ [PAIRING_SECRET_KEY]: generated });
  return generated;
}

async function sendBrokerHello(socket) {
  socket.send(JSON.stringify({
    type: "broker_hello",
    protocol_version: PROTOCOL_VERSION,
    extension_version: chrome.runtime.getManifest().version,
    extension_build_sha256: await extensionBuildSha256,
    pairing_secret: await pairingSecret(),
    capabilities: EXTENSION_CAPABILITIES,
  }));
}
const LEASE_REMOVAL_LIMIT = 40;
const DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000;
const DEFAULT_CUA_CLAIM_TTL_MS = 2 * 60 * 1000;
const MIN_LEASE_TTL_SECONDS = 1; // normal tools clamp to 30; one-second leases enable bounded lifecycle proof
let leasePersistenceQueue = Promise.resolve();
let leaseRemovalQueue = Promise.resolve();
let cuaRuntimeClaim = null;
let activeHostMutations = 0;
const CURSOR_COLORS = ["#64d8ff", "#7cf29a", "#ffd166", "#ff8fab", "#c4a7ff", "#ff9f68"];
const CONSOLE_RING_MAX = 100;
const NETWORK_ERROR_RING_MAX = 200;

function pushTabConsole(tabId, entry) {
  if (!tabId || !entry) return;
  let buf = tabConsoleCdp.get(tabId);
  if (!buf) {
    buf = [];
    tabConsoleCdp.set(tabId, buf);
  }
  buf.push(entry);
  if (buf.length > CONSOLE_RING_MAX) buf.splice(0, buf.length - CONSOLE_RING_MAX);
}

function installConsoleProbeFn() {
  if (window.__COMET_CONTROL_CONSOLE__) return true;
  const MAX = 100;
  const entries = [];
  function push(level, args, extra) {
    const text = args.map((a) => {
      try {
        if (typeof a === "string") return a;
        if (a instanceof Error) return a.stack || a.message;
        return JSON.stringify(a);
      } catch {
        return String(a);
      }
    }).join(" ").slice(0, 800);
    entries.push(Object.assign({ t: Date.now(), level, text }, extra || {}));
    if (entries.length > MAX) entries.splice(0, entries.length - MAX);
  }
  function wrap(level, orig) {
    return function (...args) {
      try { push(level, args); } catch { /* ignore */ }
      return orig.apply(console, args);
    };
  }
  console.error = wrap("error", console.error.bind(console));
  console.warn = wrap("warn", console.warn.bind(console));
  window.addEventListener("error", (e) => {
    push("page_error", [e.message || "error"], { source: e.filename || "", line: e.lineno || 0 });
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    push("unhandledrejection", [r instanceof Error ? (r.stack || r.message) : String(r)]);
  });
  window.__COMET_CONTROL_CONSOLE__ = {
    entries,
    tail(n) {
      const lim = Math.max(1, Math.min(Number(n) || 50, MAX));
      return entries.slice(-lim);
    },
    clear() { entries.length = 0; return true; },
    counts() {
      const c = { error: 0, warn: 0, page_error: 0, unhandledrejection: 0 };
      for (const e of entries) c[e.level] = (c[e.level] || 0) + 1;
      return c;
    }
  };
  return true;
}

async function ensureConsoleProbe(tabId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.id || !isControllableUrl(tab.url)) return { ok: false, reason: "tab not controllable" };
  if (tabScriptingPoisoned.has(tabId)) return { ok: false, reason: "tab scripting wedged" };
  try {
    await executeScriptOnTab(
      tabId,
      { target: { tabId }, world: "MAIN", func: installConsoleProbeFn },
      5000,
      "ensureConsoleProbe"
    );
    return { ok: true };
  } catch (error) {
    return { ok: false, reason: String(error?.message || error) };
  }
}

async function readPageConsoleTail(tabId, maxEntries, clear) {
  if (tabScriptingPoisoned.has(tabId)) return { entries: [], counts: {} };
  try {
    const results = await executeScriptOnTab(
      tabId,
      {
        target: { tabId },
        world: "MAIN",
        func: (n, doClear) => {
          const buf = window.__COMET_CONTROL_CONSOLE__;
          if (!buf) return { entries: [], counts: {} };
          const entries = buf.tail(n);
          const counts = buf.counts();
          if (doClear) buf.clear();
          return { entries, counts };
        },
        args: [maxEntries, Boolean(clear)]
      },
      5000,
      "readPageConsoleTail"
    );
    return results?.[0]?.result || { entries: [], counts: {} };
  } catch {
    return { entries: [], counts: {} };
  }
}

function mergeConsoleEntries(cdpEntries, pageEntries, maxEntries) {
  const merged = [...(cdpEntries || []), ...(pageEntries || [])]
    .filter((e) => e && e.text)
    .sort((a, b) => (a.t || 0) - (b.t || 0));
  const out = [];
  for (const e of merged) {
    const prev = out[out.length - 1];
    if (
      prev &&
      prev.level === e.level &&
      prev.text === e.text &&
      Math.abs((prev.t || 0) - (e.t || 0)) < 2000
    ) {
      continue; // page probe + CDP often double-report the same event
    }
    out.push(e);
  }
  return out.slice(-Math.max(1, Math.min(maxEntries, CONSOLE_RING_MAX)));
}

function consoleErrorCount(entries) {
  return (entries || []).filter((e) =>
    e.level === "error" || e.level === "page_error" || e.level === "unhandledrejection" || e.level === "assert"
  ).length;
}

function freshNetworkState() {
  return {
    enabled: true,
    startedAt: Date.now(),
    requests: new Map(),
    errors: [],
    requestCount: 0,
    responseCount: 0,
    failedCount: 0,
    httpErrorCount: 0,
    statusCounts: {},
    typeCounts: {},
  };
}

function clearNetworkState(state) {
  const replacement = freshNetworkState();
  Object.assign(state, replacement);
  return state;
}

function pushNetworkError(state, entry) {
  const normalized = {
    timestamp: new Date().toISOString(),
    kind: String(entry.kind || "network_error"),
    url: String(entry.url || "").slice(0, 1200),
    method: String(entry.method || "").slice(0, 20),
    resource_type: String(entry.resourceType || "").slice(0, 80),
    status: Number.isFinite(Number(entry.status)) ? Number(entry.status) : undefined,
    status_text: String(entry.statusText || "").slice(0, 200),
    error: String(entry.error || "").slice(0, 500),
    blocked_reason: String(entry.blockedReason || "").slice(0, 200),
    canceled: Boolean(entry.canceled),
  };
  state.errors.push(normalized);
  if (state.errors.length > NETWORK_ERROR_RING_MAX) {
    state.errors.splice(0, state.errors.length - NETWORK_ERROR_RING_MAX);
  }
}

async function ensureNetworkCapture(tabId, { clear = false } = {}) {
  await ensureAttached(tabId);
  let state = tabNetworkCdp.get(tabId);
  if (!state) {
    state = freshNetworkState();
    tabNetworkCdp.set(tabId, state);
    await withTimeout(
      chrome.debugger.sendCommand({ tabId }, "Network.enable"), 3000, "Network.enable"
    );
  } else if (clear) {
    clearNetworkState(state);
  }
  return state;
}

function networkSummary(state) {
  if (!state) {
    return {
      enabled: false,
      capture_started: false,
      instruction: "Run network_watch before navigation or interaction to capture subsequent failures",
    };
  }
  return {
    enabled: true,
    capture_started: true,
    started_at: new Date(state.startedAt).toISOString(),
    request_count: state.requestCount,
    response_count: state.responseCount,
    failed_count: state.failedCount,
    http_error_count: state.httpErrorCount,
    error_count: state.errors.length,
    status_counts: { ...state.statusCounts },
    resource_type_counts: { ...state.typeCounts },
    last_error: state.errors[state.errors.length - 1] || null,
  };
}

function filteredConsoleEntries(entries, action, maximum) {
  const requested = Array.isArray(action.levels)
    ? new Set(action.levels.map((level) => String(level).toLowerCase()))
    : new Set(["error", "warn", "warning", "assert", "page_error", "unhandledrejection"]);
  const filter = String(action.filter || "").toLowerCase();
  return entries.filter((entry) => {
    const level = String(entry.level || "").toLowerCase();
    const requestedLevel = requested.has(level)
      || (level === "warn" && requested.has("warning"))
      || (requested.has("error") && ["page_error", "unhandledrejection", "assert"].includes(level));
    if (!requestedLevel) return false;
    if (!filter) return true;
    return `${entry.text || ""} ${entry.source || ""}`.toLowerCase().includes(filter);
  }).slice(-maximum);
}

function filteredNetworkErrors(state, action, maximum) {
  const filter = String(action.filter || "").toLowerCase();
  const kinds = Array.isArray(action.kinds)
    ? new Set(action.kinds.map((kind) => String(kind).toLowerCase()))
    : null;
  return (state?.errors || []).filter((entry) => {
    if (kinds && !kinds.has(String(entry.kind || "").toLowerCase())) return false;
    if (!filter) return true;
    return `${entry.url || ""} ${entry.method || ""} ${entry.status || ""} ${entry.error || ""} ${entry.blocked_reason || ""}`
      .toLowerCase().includes(filter);
  }).slice(-maximum);
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const tabId = source?.tabId;
  if (!tabId) return;
  recordParityCdpEvent(source, method, params);
  if (method === "Runtime.consoleAPICalled") {
    const rawLevel = String(params?.type || "log").toLowerCase();
    const level = rawLevel === "warning" ? "warn" : rawLevel;
    if (!["debug", "info", "log", "warn", "error", "assert"].includes(level)) return;
    const text = (params?.args || []).map((a) => {
      if (a?.value != null) return String(a.value);
      if (a?.description) return String(a.description);
      return String(a?.type || "");
    }).join(" ").slice(0, 800);
    if (!text) return;
    pushTabConsole(tabId, {
      t: Date.now(),
      level,
      text,
      via: "cdp"
    });
    return;
  }
  if (method === "Runtime.exceptionThrown") {
    const d = params?.exceptionDetails;
    const text = String(d?.exception?.description || d?.text || "exception").slice(0, 800);
    pushTabConsole(tabId, {
      t: Date.now(),
      level: "page_error",
      text,
      via: "cdp",
      source: d?.url || "",
      line: d?.lineNumber || 0
    });
    return;
  }

  const network = tabNetworkCdp.get(tabId);
  if (!network) return;
  if (method === "Network.requestWillBeSent") {
    const request = params?.request || {};
    network.requests.set(params?.requestId, {
      url: request.url || "",
      method: request.method || "",
      resourceType: params?.type || "",
    });
    network.requestCount += 1;
    return;
  }
  if (method === "Network.responseReceived") {
    const response = params?.response || {};
    const status = Number(response.status);
    const resourceType = String(params?.type || "other");
    network.responseCount += 1;
    network.statusCounts[String(Number.isFinite(status) ? status : "unknown")] =
      (network.statusCounts[String(Number.isFinite(status) ? status : "unknown")] || 0) + 1;
    network.typeCounts[resourceType] = (network.typeCounts[resourceType] || 0) + 1;
    if (Number.isFinite(status) && status >= 400) {
      network.httpErrorCount += 1;
      const request = network.requests.get(params?.requestId) || {};
      pushNetworkError(network, {
        kind: "http",
        url: response.url || request.url,
        method: request.method,
        resourceType,
        status,
        statusText: response.statusText,
        error: `HTTP ${status}${response.statusText ? ` ${response.statusText}` : ""}`,
      });
    }
    return;
  }
  if (method === "Network.loadingFailed") {
    network.failedCount += 1;
    const request = network.requests.get(params?.requestId) || {};
    pushNetworkError(network, {
      kind: params?.blockedReason ? "blocked" : "loading_failed",
      url: request.url,
      method: request.method,
      resourceType: params?.type || request.resourceType,
      error: params?.errorText,
      blockedReason: params?.blockedReason || params?.corsErrorStatus?.corsError,
      canceled: params?.canceled,
    });
    network.requests.delete(params?.requestId);
    return;
  }
  if (method === "Network.loadingFinished") {
    network.requests.delete(params?.requestId);
    return;
  }
  if (method === "Network.webSocketFrameError") {
    const request = network.requests.get(params?.requestId) || {};
    pushNetworkError(network, {
      kind: "websocket",
      url: request.url,
      method: request.method,
      resourceType: "WebSocket",
      error: params?.errorMessage,
    });
  }
});

chrome.debugger.onDetach.addListener((source, reason) => {
  const tabId = source?.tabId;
  if (!tabId) return;
  attachedTabs.delete(tabId);
  tabConsoleCdp.delete(tabId);
  tabNetworkCdp.delete(tabId);
  console.warn(`Comet debugger detached from tab ${tabId}: ${reason || "unknown reason"}`);
});

function scheduleHostReconnect(delayMs) {
  if (port || reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectHost();
  }, delayMs);
}

async function invalidateLeasedConnectionState() {
  const records = Array.from(sessionLeases.values());
  for (const record of records) {
    const tabId = Number(record.tabId);
    if (!Number.isInteger(tabId)) continue;
    await sendContentScriptMessage(tabId, "invalidateConnectionState", []).catch(() => {});
    await chrome.debugger.detach({ tabId }).catch(() => {});
    attachedTabs.delete(tabId);
    injectedTabs.delete(tabId);
    tabScriptingPoisoned.delete(tabId);
    contentScriptForceReinjection.add(tabId);
    tabConsoleCdp.delete(tabId);
    tabNetworkCdp.delete(tabId);
    cleanupParityTab(tabId);
  }
}

async function restoreLeasedConnectionState() {
  await requireSessionStateReady();
  for (const record of Array.from(sessionLeases.values())) {
    const targets = await readOwnedLeaseTargets(record);
    if (!ownedLeaseTargetsComplete(record, targets)) continue;
    const status = await ensureContentScript(record.tabId).catch(() => null);
    if (status?.injected) await setAgentIdentity(record.tabId, record).catch(() => {});
  }
}

function connectHost() {
  if (port || connecting) return;
  connecting = true;
  try {
    const nextPort = new WebSocket(BROKER_URL);
    port = nextPort;
    nextPort.onopen = () => {
      restoreLeasedConnectionState()
        .then(() => sendBrokerHello(nextPort))
        .catch((error) => {
          connecting = false;
          console.error("Comet Control broker handshake failed:", error);
          nextPort.close(1008, "broker handshake failed");
        });
    };
    nextPort.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        console.error("Comet Control broker sent invalid JSON:", error);
        nextPort.close(1007, "invalid broker message");
        return;
      }
      if (message?.type === "broker_hello_ack") {
        if (message.protocol_version !== PROTOCOL_VERSION) {
          nextPort.close(1008, "broker protocol mismatch");
          return;
        }
        brokerReady = true;
        brokerInfo = {
          broker_build_sha256: String(message.broker_build_sha256 || ""),
          connection_generation: message.connection_generation,
        };
        connecting = false;
        return;
      }
      if (message?.type === "broker_ping") {
        post({ id: message.id, type: "broker_pong", success: true });
        return;
      }
      const queueKey = message?.sessionId || message?.sessionName || `request:${message?.id || crypto.randomUUID()}`;
      enqueueSession(queueKey, () => handleHostMessage(message)).catch((error) => {
        post({
          id: message?.id,
          success: false,
          ...(error?.code ? { error_code: error.code } : {}),
          ...(error?.retryable ? { retryable: true } : {}),
          ...(error?.leaseToken ? { lease_token: error.leaseToken } : {}),
          ...(error?.details ? { details: error.details } : {}),
          ...(error?.failureRecord ? { failure_record: error.failureRecord } : {}),
          error: String(error?.message || error),
        });
      });
    };
    nextPort.onerror = () => console.warn("Comet Control broker connection failed");
    nextPort.onclose = () => {
      connecting = false;
      brokerReady = false;
      brokerInfo = {};
      if (port === nextPort) port = null;
      invalidateLeasedConnectionState()
        .catch((error) => {
          console.warn("Comet Control disconnect invalidation failed:", error);
        })
        .finally(() => scheduleHostReconnect(2000));
    };
  } catch (error) {
    connecting = false;
    port = null;
    console.warn("Comet Control broker connection failed:", error);
    scheduleHostReconnect(2000);
  }
}

function post(message) {
  if (port?.readyState === WebSocket.OPEN) {
    port.send(JSON.stringify(message));
  } else {
    console.error("Comet Control response failed: broker is disconnected");
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sanitizeIdentity(value, fallback = "agent") {
  const clean = String(value || "").trim().replace(/[^a-zA-Z0-9._:@/-]+/g, "-").replace(/^[-._]+|[-._]+$/g, "");
  return (clean || fallback).slice(0, 96);
}

function requireSessionId(value) {
  const sessionId = String(value ?? "").trim();
  if (!sessionId) throw new Error("sessionId is required for an isolated browser lease");
  if (sessionId.length > 128) throw new Error("sessionId must be 128 characters or fewer");
  return sessionId;
}

function boundedNumber(value, fallback, minimum, maximum, name) {
  if (typeof value === "boolean" || (typeof value === "string" && !value.trim())) {
    throw new Error(`${name} must be a finite number`);
  }
  const number = value == null ? fallback : Number(value);
  if (!Number.isFinite(number)) throw new Error(`${name} must be a finite number`);
  return Math.max(minimum, Math.min(maximum, number));
}

function cursorColor(sessionId) {
  let hash = 0;
  for (const ch of String(sessionId || "")) hash = ((hash * 31) + ch.charCodeAt(0)) >>> 0;
  return CURSOR_COLORS[hash % CURSOR_COLORS.length];
}

function publicLease(record, extra = {}) {
  const layout = record.windowLayout || {};
  return {
    session_id: record.sessionId,
    lease_token: record.leaseToken,
    agent_id: record.agentId,
    agent_label: record.agentLabel,
    session_name: record.sessionName,
    isolation: record.isolation,
    window_id: record.windowId,
    tab_id: record.tabId,
    cursor_color: record.cursorColor,
    busy: Boolean(record.busy),
    expires_at: record.lastSeen + record.ttlMs,
    display_id: layout.displayId,
    display_name: layout.displayName,
    display_role: layout.displayRole,
    display_count: layout.displayCount,
    display_work_area: layout.displayWorkArea,
    layout_count: layout.count,
    layout_columns: layout.columns,
    layout_rows: layout.rows,
    layout_slot: layout.slot,
    requested_window_bounds: layout.requestedBounds,
    window_bounds: layout.actualBounds,
    layout_reason: layout.reason,
    ...extra,
  };
}

function recordLeaseRemoval(record, reason, extra = {}) {
  if (!record?.sessionId) return Promise.resolve();
  const event = {
    session_id: record.sessionId,
    agent_id: record.agentId,
    agent_label: record.agentLabel,
    window_id: record.windowId,
    tab_id: record.tabId,
    reason,
    removed_at: Date.now(),
    ...extra,
  };
  const next = leaseRemovalQueue.catch(() => {}).then(async () => {
    const stored = await chrome.storage.session.get(LEASE_REMOVAL_STORAGE_KEY).catch(() => ({}));
    const previous = Array.isArray(stored?.[LEASE_REMOVAL_STORAGE_KEY])
      ? stored[LEASE_REMOVAL_STORAGE_KEY]
      : [];
    await chrome.storage.session.set({
      [LEASE_REMOVAL_STORAGE_KEY]: [...previous, event].slice(-LEASE_REMOVAL_LIMIT),
    });
  });
  leaseRemovalQueue = next;
  return next;
}

async function readLeaseRemovals(sessionId = "") {
  await leaseRemovalQueue.catch(() => {});
  const stored = await chrome.storage.session.get(LEASE_REMOVAL_STORAGE_KEY).catch(() => ({}));
  const events = Array.isArray(stored?.[LEASE_REMOVAL_STORAGE_KEY])
    ? stored[LEASE_REMOVAL_STORAGE_KEY]
    : [];
  return events.filter((event) => !sessionId || event?.session_id === sessionId);
}

function displayWorkArea(display) {
  const area = display?.workArea || display?.bounds;
  if (!area) return null;
  const left = Number(area.left);
  const top = Number(area.top);
  const width = Number(area.width);
  const height = Number(area.height);
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null;
  return {
    left: Math.round(left),
    top: Math.round(top),
    width: Math.round(width),
    height: Math.round(height),
  };
}

function agentLayoutGrid(count) {
  if (count <= 1) return { columns: 1, rows: 1 };
  if (count === 2) return { columns: 2, rows: 1 };
  const columns = Math.ceil(Math.sqrt(count));
  return { columns, rows: Math.ceil(count / columns) };
}

function gridCellBounds(area, columns, rows, slot) {
  const column = slot % columns;
  const row = Math.floor(slot / columns);
  const leftOffset = Math.floor((area.width * column) / columns);
  const rightOffset = Math.floor((area.width * (column + 1)) / columns);
  const topOffset = Math.floor((area.height * row) / rows);
  const bottomOffset = Math.floor((area.height * (row + 1)) / rows);
  return {
    left: area.left + leftOffset,
    top: area.top + topOffset,
    width: rightOffset - leftOffset,
    height: bottomOffset - topOffset,
  };
}

function actualWindowBounds(window, fallback) {
  const result = {};
  for (const key of ["left", "top", "width", "height"]) {
    const value = Number(window?.[key]);
    result[key] = Number.isFinite(value) ? Math.round(value) : fallback[key];
  }
  return result;
}

async function tileOwnedAgentWindows(reason = "lease-change") {
  const candidates = Array.from(sessionLeases.values())
    .filter((record) => record.ownsWindow && !record.closing)
    .sort((a, b) => (a.createdAt - b.createdAt) || a.sessionId.localeCompare(b.sessionId));
  const targetStates = await Promise.all(
    candidates.map(async (record) => ({ record, targets: await readOwnedLeaseTargets(record) }))
  );
  // A retained partial lease remains the cleanup owner's responsibility, but
  // it must not break geometry for complete peer sessions or move a half-owned
  // surface during unrelated work.
  const records = targetStates
    .filter(({ record, targets }) => ownedLeaseTargetsComplete(record, targets))
    .map(({ record }) => record);
  if (!records.length) return { reason, count: 0 };
  if (!chrome.system?.display?.getInfo) {
    throw new Error("Chromium display layout API is unavailable; cannot keep agent windows visible");
  }

  const displays = (await chrome.system.display.getInfo())
    .map((display) => ({ display, area: displayWorkArea(display) }))
    .filter((item) => item.area && item.display?.isEnabled !== false);
  if (!displays.length) throw new Error("No usable macOS display work area is available");

  const byAreaThenId = (a, b) => (
    (b.area.width * b.area.height) - (a.area.width * a.area.height)
    || String(a.display.id).localeCompare(String(b.display.id))
  );
  const secondary = displays.filter((item) => !item.display.isPrimary).sort(byAreaThenId);
  const target = secondary[0] || displays.slice().sort((a, b) => {
    if (Boolean(a.display.isPrimary) !== Boolean(b.display.isPrimary)) return a.display.isPrimary ? -1 : 1;
    return byAreaThenId(a, b);
  })[0];
  const { columns, rows } = agentLayoutGrid(records.length);

  for (let slot = 0; slot < records.length; slot += 1) {
    const record = records[slot];
    const requestedBounds = gridCellBounds(target.area, columns, rows, slot);
    await chrome.windows.update(record.windowId, { state: "normal" });
    await chrome.windows.update(record.windowId, requestedBounds);
    const updated = await chrome.windows.get(record.windowId);
    record.windowLayout = {
      reason,
      displayId: String(target.display.id),
      displayName: String(target.display.name || target.display.id || "display"),
      displayRole: target.display.isPrimary
        ? (displays.length === 1 ? "primary-only" : "primary-fallback")
        : "secondary",
      displayCount: displays.length,
      displayWorkArea: { ...target.area },
      count: records.length,
      columns,
      rows,
      slot,
      requestedBounds,
      actualBounds: actualWindowBounds(updated, requestedBounds),
    };
  }
  return {
    reason,
    count: records.length,
    display_id: String(target.display.id),
    display_role: records[0].windowLayout.displayRole,
    columns,
    rows,
  };
}

function enqueueAgentWindowLayout(reason) {
  const next = agentWindowLayoutQueue.catch(() => {}).then(async () => {
    try {
      return await tileOwnedAgentWindows(reason);
    } finally {
      // Lease deletion must persist even if a peer window cannot be moved.
      await persistSessionLeases();
    }
  });
  agentWindowLayoutQueue = next;
  return next;
}

function deferAgentWindowLayout(reason) {
  deferredAgentWindowLayoutReason = String(reason || "lease-change");
  return persistSessionLeases();
}

async function applyAgentWindowLayout(reason) {
  const deferredReason = deferredAgentWindowLayoutReason;
  deferredAgentWindowLayoutReason = null;
  try {
    return await enqueueAgentWindowLayout(reason || deferredReason || "lease-change");
  } catch (error) {
    if (!deferredAgentWindowLayoutReason) {
      deferredAgentWindowLayoutReason = deferredReason || String(reason || "lease-change");
    }
    throw error;
  }
}

async function flushDeferredAgentWindowLayout() {
  if (!deferredAgentWindowLayoutReason) return { deferred: false };
  return applyAgentWindowLayout(`deferred:${deferredAgentWindowLayoutReason}`);
}

function persistSessionLeases() {
  // Cross-session requests have independent FIFO queues. Serialize registry
  // snapshots here so an older heartbeat cannot finish after a newer one and
  // overwrite another agent's lastSeen/TTL in storage.
  const next = leasePersistenceQueue.catch(() => {}).then(async () => {
    const value = Object.fromEntries(Array.from(sessionLeases.entries()).map(([key, record]) => [key, { ...record, busy: false }]));
    await chrome.storage.session.set({ [SESSION_STORAGE_KEY]: value });
  });
  leasePersistenceQueue = next;
  return next;
}

function publicCuaClaim(claim) {
  if (!claim) return null;
  return {
    claim_id: claim.claimId,
    intent: claim.intent,
    session_id: claim.sessionId || null,
    created_at: claim.createdAt,
    expires_at: claim.expiresAt,
  };
}

async function persistCuaClaim() {
  if (cuaRuntimeClaim) {
    await chrome.storage.session.set({ [CUA_CLAIM_STORAGE_KEY]: cuaRuntimeClaim });
  } else {
    await chrome.storage.session.remove(CUA_CLAIM_STORAGE_KEY);
  }
}

async function restoreCuaClaim() {
  const stored = await chrome.storage.session.get(CUA_CLAIM_STORAGE_KEY);
  const claim = stored?.[CUA_CLAIM_STORAGE_KEY];
  if (claim?.claimId && claim?.claimToken && Number(claim.expiresAt) > Date.now()) {
    cuaRuntimeClaim = claim;
    return;
  }
  cuaRuntimeClaim = null;
  await chrome.storage.session.remove(CUA_CLAIM_STORAGE_KEY);
}

let cuaClaimStateError = null;
const cuaClaimStateReady = restoreCuaClaim().catch((error) => {
  cuaClaimStateError = error;
  console.error("Could not restore the CUA runtime claim; managed Comet is locked:", error);
});

async function requireCuaClaimStateReady() {
  await cuaClaimStateReady;
  if (!cuaClaimStateError) return;
  const error = new Error(
    "CUA runtime claim state is unavailable; refusing managed Comet work until the extension is reloaded"
  );
  error.code = "CUA_CLAIM_STATE_UNAVAILABLE";
  throw error;
}

function activeCuaClaim() {
  if (cuaRuntimeClaim && Number(cuaRuntimeClaim.expiresAt) <= Date.now()) {
    cuaRuntimeClaim = null;
    void persistCuaClaim().catch((error) => console.warn("Could not reap expired CUA claim:", error));
  }
  return cuaRuntimeClaim;
}

async function restoreSessionLeases() {
  // A storage read failure is not equivalent to an empty lease registry.  If
  // restoration is uncertain, requireSessionStateReady() must stop every
  // browser-mutating route rather than allowing duplicate ownership.
  const stored = await chrome.storage.session.get(SESSION_STORAGE_KEY);
  const leases = stored?.[SESSION_STORAGE_KEY] || {};
  for (const [sessionId, raw] of Object.entries(leases)) {
    if (!raw) continue;
    const ownsWindow = raw.ownsWindow !== false;
    const ownsTab = raw.ownsTab !== false;
    const validOwnedWindow = !ownsWindow || Number.isInteger(raw.windowId);
    const validOwnedTab = !ownsTab || Number.isInteger(raw.tabId);
    // A persisted acquisition may intentionally be window-only while Comet
    // is still discovering its first tab. Keep that exact provisional window
    // across MV3 suspension; discarding it would abandon cleanup ownership.
    if ((!ownsWindow && !ownsTab) || !validOwnedWindow || !validOwnedTab) continue;
    const record = {
      ...raw,
      sessionId,
      ownsWindow,
      ownsTab,
      busy: false,
      closing: false,
    };
    const targets = await readOwnedLeaseTargets(record);
    if (ownedLeaseTargetsAbsent(targets)) {
      await recordLeaseRemoval(record, "restore-target-absent", {
        tab_present: false,
        window_present: false,
      });
      continue;
    }
    // A half-missing or moved target is still authenticated browser ownership.
    // Retain it so the original capability can close every surviving surface.
    sessionLeases.set(sessionId, record);
  }
  // Service-worker startup may race a disjoint native-app CUA capture. Restore
  // ownership state now and defer all window geometry to a host-locked request.
  await deferAgentWindowLayout("restore");
}

let sessionStateError = null;
const sessionStateReady = restoreSessionLeases().catch((error) => {
  sessionStateError = error;
  console.error("Could not restore Comet Control agent leases; browser mutations are disabled:", error);
});

async function requireSessionStateReady() {
  await sessionStateReady;
  if (!sessionStateError) return;
  const error = new Error(
    "Comet Control lease state is unavailable; refusing browser session work until the extension is reloaded"
  );
  error.code = "LEASE_STATE_UNAVAILABLE";
  throw error;
}

function enqueueSession(sessionId, operation) {
  const key = sessionId || `unscoped:${crypto.randomUUID()}`;
  const previous = sessionQueues.get(key) || Promise.resolve();
  const next = previous.catch(() => {}).then(operation);
  sessionQueues.set(key, next);
  next.finally(() => {
    if (sessionQueues.get(key) === next) sessionQueues.delete(key);
  }).catch(() => {});
  return next;
}

function enqueueViewportCapture(operation) {
  const next = viewportCaptureQueue.catch(() => {}).then(operation);
  viewportCaptureQueue = next;
  return next;
}

async function waitForViewportCaptureSlot() {
  const minGapMs = 550; // Comet allows at most two captureVisibleTab calls per second.
  const waitMs = Math.max(0, lastViewportCaptureStartedAt + minGapMs - Date.now());
  if (waitMs > 0) await sleep(waitMs);
  lastViewportCaptureStartedAt = Date.now();
}

async function waitForTabReady(tabId, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("Agent-owned tab disappeared during preflight");
    if (tab.status === "complete" && isControllableUrl(tab.url)) return tab;
    await sleep(100);
  }
  throw new Error("Agent-owned tab did not become controllable during preflight");
}

async function setAgentIdentity(tabId, record) {
  if (!tabId || !record) return;
  const ready = await ensureContentScript(tabId);
  if (ready?.blocked) throw new Error(ready.reason || "Could not inject agent cursor");
  const response = await sendToContentScript(tabId, "setIdentity", [{
    agentId: record.agentId,
    label: record.agentLabel,
    sessionId: record.sessionId,
    color: record.cursorColor,
  }]);
  if (!response?.success) throw new Error(response?.error || "Could not label agent cursor");
}

async function sessionPreflight(message) {
  const timeoutMs = Math.max(
    1000,
    boundedNumber(message.timeoutSeconds, 45, 1, 300, "timeoutSeconds") * 1000 - 500
  );
  const sessionId = requireSessionId(message.sessionId);
  const deadlineAt = Date.now() + timeoutMs;
  const remainingMs = () => {
    const remaining = deadlineAt - Date.now();
    if (remaining <= 0) throw new Error("Agent browser preflight reached its extension deadline");
    return remaining;
  };
  await withTimeout(requireSessionStateReady(), remainingMs(), "restoreSessionLeases");
  await withTimeout(
    reapExpiredSessions({ ownedSessionId: sessionId }), remainingMs(), "reapExpiredSessions"
  );
  const existing = sessionLeases.get(sessionId);
  if (existing) {
    const tokenOk = Boolean(message.leaseToken) && message.leaseToken === existing.leaseToken;
    const targets = await readOwnedLeaseTargets(existing);
    if (tokenOk) {
      if (ownedLeaseTargetsComplete(existing, targets)) {
        existing.agentLabel = String(message.agentLabel || existing.agentLabel).slice(0, 80);
        existing.lastSeen = Date.now();
        existing.ttlMs = boundedNumber(
          message.ttlSeconds, existing.ttlMs / 1000, MIN_LEASE_TTL_SECONDS, 3600, "ttlSeconds"
        ) * 1000;
        await withTimeout(
          setAgentIdentity(existing.tabId, existing), remainingMs(), "setAgentIdentity"
        );
        await withTimeout(
          applyAgentWindowLayout("preflight-reuse"), remainingMs(), "tileAgentWindows"
        );
        return publicLease(existing, { reused: true });
      }
      // Preflight is visual and may repair a partial prior target, but it must
      // prove that every owned surface is gone before creating a replacement.
      await closeSession(sessionId, { reason: "preflight-target-partial" });
    } else {
      // Same session-id retry without the private token. Reclaim only when the
      // prior owner is gone: Comet targets absent, or renewals stale for two
      // driver renew intervals (ttl/3, capped at 60s). Never return leaseToken
      // here — that would steal an idle live owner's lease (busy is false
      // between run commands).
      // Also reclaim stuck-busy orphans: EXTENSION_TIMEOUT / dead drivers can
      // leave busy=true with no renewals; require a longer silence than a live
      // run timeout so an in-flight owner is not stolen.
      const renewIntervalMs = Math.min(60_000, Math.max(existing.ttlMs / 3, 50));
      const ownerGoneMs = 2 * renewIntervalMs;
      const idleStale = !existing.busy
        && !existing.closing
        && (Date.now() - existing.lastSeen > ownerGoneMs);
      const stuckBusyStale = existing.busy
        && !existing.closing
        && (Date.now() - existing.lastSeen > Math.max(ownerGoneMs, 180_000));
      const renewStale = idleStale || stuckBusyStale;
      if (ownedLeaseTargetsAbsent(targets) || renewStale) {
        await closeSession(sessionId, { reason: "preflight-orphan-reclaim" });
      } else {
        throw codedError(
          "LEASE_HELD",
          `Session ${sessionId} is already leased by another caller`,
          { retryable: true }
        );
      }
    }
  }

  const startUrl = String(message.url || "").trim();
  if (!isControllableUrl(startUrl)) {
    throw new Error("Window preflight requires an explicit controllable http(s) URL");
  }
  const claimTabId = Number(message.claimTabId || 0);
  if (claimTabId || (message.isolation && message.isolation !== "window")) {
    throw new Error("WIP Comet Control requires isolation=window; claimed and tab-only targets are disabled");
  }
  const isolation = "window";
  const ttlMs = boundedNumber(
    message.ttlSeconds, DEFAULT_LEASE_TTL_MS / 1000, MIN_LEASE_TTL_SECONDS, 3600, "ttlSeconds"
  ) * 1000;
  let tab;
  let windowId;
  let record;
  const agentId = sanitizeIdentity(message.agentId || sessionId, "agent");
  try {
    // Keep the human's key focus. The labeled cursor still glides in-page.
    const created = await chrome.windows.create({ url: startUrl, focused: false, type: "normal" });
    windowId = created.id;
    if (windowId === undefined) throw new Error("Comet did not return an isolated agent window ID");
    tab = created.tabs?.[0] || null;
    // Register the owned window before any further Comet API call. If tab
    // discovery, readiness, labeling, persistence, or layout fails, the same
    // proof-gated closeout path retains ownership until exact IDs are absent.
    record = {
      sessionId,
      leaseToken: crypto.randomUUID(),
      agentId,
      agentLabel: String(message.agentLabel || agentId).trim().slice(0, 80) || agentId,
      sessionName: String(message.sessionName || `Comet Control · ${agentId}`).slice(0, 80),
      isolation,
      windowId,
      tabId: tab?.id || 0,
      ownsWindow: true,
      ownsTab: Boolean(tab?.id),
      cursorColor: cursorColor(sessionId),
      createdAt: Date.now(),
      lastSeen: Date.now(),
      ttlMs,
      busy: false,
    };
    sessionLeases.set(sessionId, record);
    await persistSessionLeases();
    if (!tab?.id) {
      tab = (await chrome.tabs.query({ windowId }))[0];
      if (!tab?.id) throw new Error("Comet did not create an isolated agent tab");
      record.tabId = tab.id;
      record.ownsTab = true;
      await persistSessionLeases();
    }
    await waitForTabReady(record.tabId, Math.min(10000, remainingMs()));
    // A dedicated window is already operator-visible isolation. Grouping its only
    // tab can collapse the window on some macOS Comet configurations.
    await withTimeout(
      setAgentIdentity(record.tabId, record), remainingMs(), "setAgentIdentity"
    );
    await withTimeout(
      applyAgentWindowLayout("preflight"), remainingMs(), "tileAgentWindows"
    );
    return publicLease(record, { reused: false });
  } catch (error) {
    if (record && sessionLeases.get(sessionId) === record) {
      try {
        await closeSession(sessionId, { reason: "preflight-failed" });
      } catch (cleanupError) {
        cleanupError.cause = error;
        // The new lease token was not returned by a successful preflight yet.
        // Return it only to this native-bridge caller so it can authenticate a
        // cleanup retry; token-private clients must redact it from output.
        cleanupError.leaseToken = record.leaseToken;
        throw cleanupError;
      }
    }
    throw error;
  }
}

function requireSessionLease(message) {
  const sessionId = requireSessionId(message.sessionId);
  const record = sessionLeases.get(sessionId);
  if (!record) throw new Error(`No active browser lease for ${sessionId}; run preflight first`);
  if (!message.leaseToken || message.leaseToken !== record.leaseToken) {
    throw new Error(`Invalid browser lease token for ${sessionId}`);
  }
  return record;
}

function chromeTargetLookupProvesAbsent(error, kind) {
  const message = String(error?.message || error || "");
  return kind === "tab"
    ? /(?:no tab with id|invalid tab id|tab not found)/i.test(message)
    : /(?:no window with id|invalid window id|window not found)/i.test(message);
}

async function readExactCometTarget(kind, id) {
  try {
    return kind === "tab" ? await chrome.tabs.get(id) : await chrome.windows.get(id);
  } catch (error) {
    if (chromeTargetLookupProvesAbsent(error, kind)) return null;
    throw codedError(
      "LEASE_TARGET_READ_FAILED",
      `Could not verify owned ${kind} ${id}: ${String(error?.message || error)}`,
      { retryable: true }
    );
  }
}

async function readOwnedLeaseTargets(record) {
  const [tab, ownedWindow] = await Promise.all([
    record.ownsTab === false ? null : readExactCometTarget("tab", record.tabId),
    record.ownsWindow === false ? null : readExactCometTarget("window", record.windowId),
  ]);
  return {
    tab,
    ownedWindow,
    tabPresent: Boolean(tab),
    windowPresent: Boolean(ownedWindow),
    tabWindowId: tab?.windowId,
    moved: Boolean(tab && ownedWindow && tab.windowId !== record.windowId),
  };
}

function ownedLeaseTargetsAbsent(targets) {
  return !targets.tabPresent && !targets.windowPresent;
}

function ownedLeaseTargetsComplete(record, targets) {
  const tabComplete = record.ownsTab === false || targets.tabPresent;
  const windowComplete = record.ownsWindow === false || targets.windowPresent;
  return tabComplete && windowComplete && !targets.moved;
}

function publicOwnedTargetState(targets) {
  return {
    tab_present: targets.tabPresent,
    window_present: targets.windowPresent,
    tab_window_id: targets.tabWindowId,
    tab_moved: targets.moved,
  };
}

async function retainLeaseForCleanupRetry(record) {
  if (sessionLeases.get(record.sessionId) !== record) return;
  record.closing = false;
  record.busy = false;
  await persistSessionLeases();
}

async function readOwnedLeaseTargetsForCleanup(record) {
  try {
    return await readOwnedLeaseTargets(record);
  } catch (error) {
    await retainLeaseForCleanupRetry(record);
    throw error;
  }
}

async function finalizeAbsentLease(record, reason, extra = {}, { visual = false } = {}) {
  // Finalization is deliberately proof-gated. The exact IDs are re-read here
  // even when a caller just checked them, closing the remove/verify race.
  const verified = await readOwnedLeaseTargetsForCleanup(record);
  if (!ownedLeaseTargetsAbsent(verified)) {
    await retainLeaseForCleanupRetry(record);
    throw codedError(
      "LEASE_CLEANUP_INCOMPLETE",
      `Owned browser surfaces for ${record.sessionId} still exist; retry closeout`,
      { retryable: true }
    );
  }
  if (sessionLeases.get(record.sessionId) === record) {
    sessionLeases.delete(record.sessionId);
  }
  await recordLeaseRemoval(record, reason, {
    ...extra,
    ...publicOwnedTargetState(verified),
    verified_absent: true,
  });
  if (visual) {
    try {
      await applyAgentWindowLayout("closeout");
    } catch (error) {
      // Owned surfaces are already verified absent and the deletion was
      // persisted by the layout queue's finally block. Peer tiling is deferred
      // maintenance, not a reason to misreport terminal cleanup as failed.
      console.warn("Comet Control peer-window layout deferred after verified closeout:", error);
    }
  } else {
    await deferAgentWindowLayout(reason);
  }
}

async function renewSessionLease(message) {
  await requireSessionStateReady();
  const record = requireSessionLease(message);
  const targets = await readOwnedLeaseTargets(record);
  if (ownedLeaseTargetsAbsent(targets)) {
    await finalizeAbsentLease(record, "renew-target-absent", {}, { visual: false });
    throw codedError(
      "LEASE_TARGET_MISSING",
      `Agent browser target for ${record.sessionId} no longer exists; renewal refused`
    );
  }
  if (!ownedLeaseTargetsComplete(record, targets)) {
    // Renewal must stay nonvisual: preserve the capability for authenticated
    // closeout, but never focus, move, or remove a surviving partial target.
    await retainLeaseForCleanupRetry(record);
    throw codedError(
      "LEASE_TARGET_PARTIAL",
      `Agent browser target for ${record.sessionId} is partial or moved; retry closeout`,
      { retryable: true }
    );
  }
  const ttlMs = boundedNumber(
    message.ttlSeconds,
    record.ttlMs / 1000,
    MIN_LEASE_TTL_SECONDS,
    3600,
    "ttlSeconds"
  ) * 1000;
  const renewedAt = Date.now();
  record.lastSeen = renewedAt;
  record.ttlMs = ttlMs;
  await persistSessionLeases();
  return {
    session_id: record.sessionId,
    window_id: record.windowId,
    tab_id: record.tabId,
    renewed_at: renewedAt,
    expires_at: renewedAt + ttlMs,
    ttl_seconds: ttlMs / 1000,
  };
}

async function closeSession(sessionId, { reason = "closeout" } = {}) {
  await requireSessionStateReady();
  const record = sessionLeases.get(sessionId);
  if (!record) {
    return { session_id: sessionId, already_closed: true, reason, cursors_hidden: 0, tabs_closed: 0, windows_closed: 0 };
  }
  record.closing = true;
  let cursorsHidden = 0;
  await dismissParityDialog(record.tabId, send).catch(() => {});
  try {
    await sendToContentScript(record.tabId, "hide", []);
    await sendToContentScript(record.tabId, "clearIdentity", []);
    cursorsHidden = 1;
  } catch { /* closing the owned target remains authoritative cleanup */ }
  await chrome.debugger.detach({ tabId: record.tabId }).catch(() => {});
  attachedTabs.delete(record.tabId);
  injectedTabs.delete(record.tabId);
  tabConsoleCdp.delete(record.tabId);
  tabNetworkCdp.delete(record.tabId);
  cleanupParityTab(record.tabId);

  let tabsClosed = 0;
  let windowsClosed = 0;
  const removalErrors = [];
  const initialTargets = await readOwnedLeaseTargetsForCleanup(record);
  let verified = initialTargets;
  // Two exact-ID attempts cover the normal window-removal/tab-onRemoved race
  // without turning closeout into an unbounded retry loop.
  for (let attempt = 1; attempt <= 2 && !ownedLeaseTargetsAbsent(verified); attempt += 1) {
    if (verified.windowPresent) {
      try {
        await chrome.windows.remove(record.windowId);
      } catch (error) {
        removalErrors.push(`window:${String(error?.message || error)}`);
      }
    }

    // Re-read the exact tab after the window attempt. A user or Comet may
    // have moved it, in which case removing the old window does not close it.
    const afterWindowAttempt = await readOwnedLeaseTargetsForCleanup(record);
    if (afterWindowAttempt.tabPresent) {
      try {
        await chrome.tabs.remove(record.tabId);
      } catch (error) {
        removalErrors.push(`tab:${String(error?.message || error)}`);
      }
    }
    verified = await readOwnedLeaseTargetsForCleanup(record);
    if (!ownedLeaseTargetsAbsent(verified) && attempt < 2) await sleep(25);
  }

  if (!ownedLeaseTargetsAbsent(verified)) {
    await retainLeaseForCleanupRetry(record);
    throw codedError(
      "LEASE_CLEANUP_INCOMPLETE",
      `Could not prove all owned browser surfaces for ${sessionId} absent after 2 attempts`,
      { retryable: true }
    );
  }

  // Count verified ownership transitions, not only the API call that happened
  // to cause them. Removing a window normally removes its tab as a side effect.
  tabsClosed = initialTargets.tabPresent && !verified.tabPresent ? 1 : 0;
  windowsClosed = initialTargets.windowPresent && !verified.windowPresent ? 1 : 0;

  await finalizeAbsentLease(record, reason, {
    tabs_closed: tabsClosed,
    windows_closed: windowsClosed,
    removal_errors: removalErrors,
  }, { visual: true });
  return { session_id: sessionId, already_closed: false, reason, cursors_hidden: cursorsHidden, tabs_closed: tabsClosed, windows_closed: windowsClosed };
}

async function reapExpiredSessions({ ownedSessionId = null } = {}) {
  await requireSessionStateReady();
  const now = Date.now();
  // Past ttl/lastSeen, reap even if busy stuck after EXTENSION_TIMEOUT / dead
  // driver — otherwise orphans block repair forever.
  const candidateSessionIds = Array.from(sessionLeases.values())
    .filter((record) => now - record.lastSeen > record.ttlMs)
    .map((record) => record.sessionId);
  const reapIfStillExpired = async (sessionId) => {
    const record = sessionLeases.get(sessionId);
    if (!record || Date.now() - record.lastSeen <= record.ttlMs) return null;
    return closeSession(sessionId, { reason: "lease-expired" });
  };
  const results = [];
  for (const sessionId of candidateSessionIds) {
    // sessionPreflight already owns its session FIFO. Queueing that same key
    // here would wait behind the currently executing operation forever.
    // A foreign FIFO may likewise be owned by a concurrent preflight that is
    // reaping this one. Do not create a cross-session wait cycle: its queued
    // lifecycle operation will either refresh or close that lease itself.
    if (sessionId !== ownedSessionId && sessionQueues.has(sessionId)) continue;
    const result = sessionId === ownedSessionId
      ? await reapIfStillExpired(sessionId)
      : await enqueueSession(sessionId, () => reapIfStillExpired(sessionId));
    if (result) results.push(result);
  }
  return results;
}

function isControllableUrl(url) {
  return typeof url === "string" && /^(https?|file):\/\//i.test(url);
}

function isAdOrTrackerFrameUrl(url) {
  if (typeof url !== "string" || !url) return false;
  return /doubleclick|googlesyndication|googleadservices|adservice\.google|(^|\/\/)([^/]*\.)?(twitter|x)\.com(\/|$)/i.test(url);
}

function isForeignExtensionRestriction(error) {
  const msg = String(error?.message || error || "");
  return msg.includes("foreign-extension URL/frame restriction")
    || (msg.includes("chrome-extension://") && !msg.includes("already attached"));
}

function unsupportedUrlReason(url) {
  if (!url) return "No tab URL is available";
  if (url.startsWith("file://")) {
    return "File URLs require Comet extension file access to be enabled";
  }
  return `Comet does not allow content-script injection on this URL: ${url}`;
}

async function ensureAttached(tabId) {
  if (attachedTabs.has(tabId)) return;
  let attachedHere = false;
  try {
    await withTimeout(chrome.debugger.attach({ tabId }, "1.3"), 3000, "debugger.attach");
    attachedHere = true;
  } catch (e) {
    const msg = String(e?.message || e);
    // "Another debugger is already attached" means we lost the Set (service worker restarted)
    // but our debugger session may still be live. Verify ownership below instead of
    // assuming the attachment belongs to Comet Control; another automation extension can own it.
    if (!msg.includes("already attached")) {
      if (msg.includes("chrome-extension://")) {
        throw new Error(`Comet debugger for tab ${tabId} hit a foreign-extension URL/frame restriction: ${msg}`);
      }
      throw e;
    }
  }
  try {
    await withTimeout(chrome.debugger.sendCommand({ tabId }, "Runtime.enable"), 3000, "Runtime.enable");
    await withTimeout(chrome.debugger.sendCommand({ tabId }, "Page.enable"), 3000, "Page.enable");
    attachedTabs.add(tabId);
  } catch (error) {
    attachedTabs.delete(tabId);
    if (attachedHere) await chrome.debugger.detach({ tabId }).catch(() => {});
    const msg = String(error?.message || error);
    if (msg.includes("chrome-extension://")) {
      throw new Error(`Comet debugger for tab ${tabId} hit a foreign-extension URL/frame restriction: ${msg}`);
    }
    if (msg.includes("not attached")) {
      throw new Error(`Comet debugger for tab ${tabId} is controlled by another extension: ${msg}`);
    }
    throw error;
  }
}

async function attachForClick(tabId) {
  try {
    await ensureAttached(tabId);
    return true;
  } catch (error) {
    if (!isForeignExtensionRestriction(error)) throw error;
    return false;
  }
}

async function send(tabId, method, params = {}) {
  await ensureAttached(tabId);
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

async function evaluate(tabId, expression) {
  // Try CDP evaluate first (most reliable when debugger is attached).
  // Fall back to content script DOM injection for pages with extension frames.
  try {
    const result = await send(tabId, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true
    });
    if (result.exceptionDetails) {
      const detail = result.exceptionDetails.exception?.description || result.exceptionDetails.text;
      throw new Error(detail || "Runtime.evaluate failed");
    }
    return result.result?.value;
  } catch (cdpError) {
    // CDP failed — try content script path (works when debugger can't attach
    // due to extension frames, but content script is still injected)
    try {
      const resp = await sendToContentScript(tabId, "evaluate", [expression]);
      if (resp?.success) return resp.result;
      throw new Error(resp?.error || "content script evaluate failed");
    } catch {
      // Both failed — throw the original CDP error
      throw cdpError;
    }
  }
}

function requireContentScriptResult(response, fallback) {
  if (response?.success === false) {
    const error = codedError(response.error_code || "CONTENT_SCRIPT_ERROR", response.error || fallback);
    if (response.details) error.details = response.details;
    throw error;
  }
  if (response?.result == null) {
    const miss = /No actionable/.test(String(fallback || ""));
    throw codedError(miss ? "ELEMENT_NOT_FOUND" : "CONTENT_SCRIPT_EMPTY_RESULT", fallback);
  }
  return response.result;
}

function isLocatorMissError(error) {
  const code = String(error?.code || "");
  if (code === "ELEMENT_NOT_FOUND" || code === "CONTENT_SCRIPT_EMPTY_RESULT") return true;
  if (code.startsWith("ACTIONABILITY_")) return true;
  const msg = String(error?.message || error || "");
  return /No actionable (clickable )?element matched/i.test(msg)
    || /Expected exactly one actionable target, found 0/i.test(msg);
}

function isContentScriptTimeoutError(error) {
  const code = String(error?.code || "");
  if (code === "CONTENT_SCRIPT_TIMEOUT") return true;
  const msg = String(error?.message || error || "");
  return /timed out/i.test(msg) || /Content script action timed out/i.test(msg);
}

function isHalfDeadReloadError(error) {
  const msg = String(error?.message || error || "");
  return /reload required|Content script missing after SPA remount/i.test(msg);
}

async function findPointBySelector(tabId, selectorText, mode = "click") {
  return findPointOnControllableFrames(
    tabId,
    "findPointBySelector",
    [selectorText, mode],
    `No actionable element matched selector: ${selectorText}`
  );
}

async function findPointByText(tabId, text) {
  return findPointOnControllableFrames(
    tabId,
    "findPointByText",
    [text],
    `No actionable clickable element matched text: ${text}`
  );
}

async function controllableFrameIds(tabId) {
  const frames = await chrome.webNavigation.getAllFrames({ tabId }).catch(() => []);
  const controllable = (frames || []).filter((frame) => isControllableUrl(frame.url));
  // Prefer the top frame (frameId === 0) first so the first https iframe
  // (DoubleClick/Twitter ads) cannot win locators or page_context.
  controllable.sort((a, b) => {
    if ((a.frameId === 0) !== (b.frameId === 0)) return a.frameId === 0 ? -1 : 1;
    return Number(isAdOrTrackerFrameUrl(a.url)) - Number(isAdOrTrackerFrameUrl(b.url));
  });
  return controllable.map(({ frameId, url }) => ({ frame_id: frameId, url }));
}

async function findPointOnControllableFrames(tabId, action, args, fallback) {
  const frames = await controllableFrameIds(tabId);
  const ids = frames.length ? frames : [{ frame_id: 0, url: "" }];
  // Skip known ad/tracker hosts unless the locator is only there.
  const nonAd = ids.filter((frame) => !isAdOrTrackerFrameUrl(frame.url));
  const ads = ids.filter((frame) => isAdOrTrackerFrameUrl(frame.url));
  const order = nonAd.length ? nonAd.concat(ads) : ids;
  let lastError;
  let lastMiss;
  for (const frame of order) {
    const frameId = frame.frame_id;
    try {
      const response = await sendToContentScript(tabId, action, args, frameId);
      const point = requireContentScriptResult(response, fallback);
      return { ...point, frame_id: frameId };
    } catch (error) {
      lastError = error;
      if (isLocatorMissError(error)) lastMiss = error;
    }
  }
  if (lastMiss) {
    if (lastMiss.code) throw lastMiss;
    throw codedError("ELEMENT_NOT_FOUND", lastMiss.message || fallback);
  }
  throw lastError || codedError("ELEMENT_NOT_FOUND", fallback);
}

async function moveCursorToPoint(tabId, point) {
  const response = await sendToContentScript(
    tabId, "moveToAndWait", [point.x, point.y, 900], point.frame_id
  );
  return requireContentScriptResult(response, "Cursor movement failed");
}

async function clickAtPoint(tabId, point, expectation = {}) {
  await moveCursorToPoint(tabId, point);
  const response = await sendToContentScript(tabId, "click", [expectation], point.frame_id);
  return requireContentScriptResult(response, "Cursor click failed");
}

async function clickResolvedTarget(tabId, resolvePoint, expectation) {
  let point = await resolvePoint();
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const click = await clickAtPoint(tabId, point, {
        ...expectation,
        targetToken: point.target_token || "",
        pageRevision: point.page_revision,
        targetHref: point.href || "",
      });
      return { point, click, retried: attempt > 0 };
    } catch (error) {
      const targetMoved = error?.code === "CLICK_TARGET_MISMATCH"
        || String(error?.message || error).includes("CLICK_TARGET_MISMATCH");
      if (!targetMoved || attempt > 0) throw error;
      point = await resolvePoint();
    }
  }
  throw codedError("ACTIONABILITY_UNSTABLE", "Cursor target did not stabilize");
}

function leaseForTab(tabId) {
  for (const record of sessionLeases.values()) {
    if (Number(record.tabId) === Number(tabId)) return record;
  }
  return null;
}

function invalidateTabInjection(tabId) {
  // Navigation destroys the content-script world. Keeping tabId in injectedTabs
  // made onUpdated skip reinjection, so cursor identity vanished after goto.
  // Do NOT clear tabScriptingPoisoned here: SPA remounts fire loading while
  // Comet's scripting FIFO is still wedged; clearing poison let Dispatch
  // retry executeScript and hang (injectCanary timeout).
  injectedTabs.delete(tabId);
  attachedTabs.delete(tabId);
  contentScriptForceReinjection.delete(tabId);
}

// ---- Navigation invalidation (lazy inject on interaction) ----
// Do NOT call ensureContentScript / executeScript from onUpdated. Seller
// Accept→Dispatch SPA remounts fire complete while a click is already injecting;
// concurrent scripting.executeScript on the same tab hangs until timeout.
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status === "loading" || changeInfo.url) {
    invalidateTabInjection(tabId);
  }
  if (changeInfo.status !== "complete") return;
  if (!tab?.url || !isControllableUrl(tab.url)) return;
  const lease = leaseForTab(tabId);
  if (!lease || lease.busy) return;
  try {
    // Identity refresh only when the script is already alive — never reinject here.
    if (await probeContentScript(tabId)) {
      await setAgentIdentity(tabId, lease);
    }
  } catch {
    // Next leased action will ensure/inject.
  }
});

// ---- Content Script Injection Tracking ----
// (injectedTabs declared at top of file, line 4)

async function operatorStatus() {
  await controlPauseReady;
  const sessions = Array.from(sessionLeases.values()).map((record) => ({
    session_id: record.sessionId,
    agent_label: record.agentLabel,
    busy: Boolean(record.busy),
  }));
  return {
    ok: true,
    connected: port?.readyState === WebSocket.OPEN && brokerReady,
    paused: controlPaused,
    protocol_version: PROTOCOL_VERSION,
    extension_version: chrome.runtime.getManifest().version,
    extension_build_sha256: await extensionBuildSha256,
    ...brokerInfo,
    active_agent_sessions: sessions.length,
    sessions,
  };
}

async function setControlPaused(paused) {
  await controlPauseReady;
  controlPaused = Boolean(paused);
  await chrome.storage.local.set({ [CONTROL_PAUSED_KEY]: controlPaused });
  if (controlPaused) {
    for (const state of activeRunStates) state.cancelled = true;
  }
  return operatorStatus();
}

// Listen for content script ready pings and status queries
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg === 'comet-control-cursor-ready' && sender.tab?.id) {
    injectedTabs.add(sender.tab.id);
  }
  if (msg && msg.type === 'comet-control-cursor-status') {
    ensureContentScript(msg.tabId)
      .then((status) => sendResponse(status))
      .catch((error) => sendResponse({ injected: false, blocked: true, reason: String(error?.message || error) }));
    return true;
  }
  if (msg && msg.type === 'comet-control-feedback-toggle') {
    toggleFeedbackWidget(msg.tabId, msg.enabled)
      .then((status) => sendResponse(status))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (msg && msg.type === 'comet-control-operator-status') {
    operatorStatus()
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (msg && msg.type === 'comet-control-control-pause') {
    setControlPaused(msg.paused)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
});

// Inject the feedback widget into a tab. If the operator configured a cross-origin
// queue server (chrome.storage `feedbackQueueOrigin`), set it in the tab's isolated
// world FIRST so feedback-widget.js picks it up; otherwise the widget defaults to
// the page's own origin (correct for artifacts served by the feedback server).
async function injectFeedbackWidget(tabId) {
  const { feedbackQueueOrigin } = await chrome.storage.local.get('feedbackQueueOrigin');
  if (feedbackQueueOrigin) {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (origin) => { window.__COMET_CONTROL_FEEDBACK_QUEUE_ORIGIN = origin; },
      args: [feedbackQueueOrigin]
    });
  }
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ['content-scripts/feedback-widget.js']
  });
}

async function toggleFeedbackWidget(tabId, enabled) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.id) return { ok: false, error: 'tab_not_found' };
  if (!isControllableUrl(tab.url)) return { ok: false, error: unsupportedUrlReason(tab.url) };

  const storageKey = `feedback-mode:${tabId}`;
  if (enabled) {
    await chrome.storage.local.set({ [storageKey]: true });
    await injectFeedbackWidget(tabId);
    return { ok: true, enabled: true };
  }

  await chrome.storage.local.remove(storageKey);
  // Removal: clear the mount node + reset the injection flag. Page reload also works
  // and is the recommended clean reset; this is a best-effort soft removal.
  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      document.getElementById('comet-control-feedback-mount')?.remove();
      window.__cometControlFeedbackWidgetInjected = false;
    }
  }).catch(() => {});
  return { ok: true, enabled: false };
}

// Auto-re-inject the feedback widget after page reloads/navigations in tabs where
// it was toggled on. devpulse (and many dashboards) hard-reload periodically;
// without this the widget would silently disappear and the operator would see the
// toggle "stuck on" with no UI.
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete') return;
  if (!isControllableUrl(tab.url)) return;
  const storageKey = `feedback-mode:${tabId}`;
  const { [storageKey]: on } = await chrome.storage.local.get(storageKey);
  if (!on) return;
  try {
    await injectFeedbackWidget(tabId);
  } catch (err) {
    // Tab may have closed mid-injection or navigated to a blocked URL; non-fatal.
  }
});

async function reconcileExternallyRemovedLeaseTarget(sessionId, expectedRecord, reason, details) {
  await requireSessionStateReady();
  const record = sessionLeases.get(sessionId);
  if (!record || record !== expectedRecord || record.closing) return;
  const targets = await readOwnedLeaseTargets(record);
  if (ownedLeaseTargetsAbsent(targets)) {
    await finalizeAbsentLease(record, reason, details, { visual: false });
    return;
  }
  // One exact owned surface still exists (or the tab moved). Do not discard
  // the only capability that can later close it.
  await retainLeaseForCleanupRetry(record);
}

// Clean up per-tab storage when the tab closes so we don't accumulate stale keys.
chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  chrome.storage.local.remove(`feedback-mode:${tabId}`).catch(() => {});
  attachedTabs.delete(tabId);
  injectedTabs.delete(tabId);
  tabScriptingPoisoned.delete(tabId);
  tabConsoleCdp.delete(tabId);
  tabNetworkCdp.delete(tabId);
  cleanupParityTab(tabId);
  for (const [sessionId, record] of sessionLeases.entries()) {
    if (record.tabId !== tabId || record.closing) continue;
    enqueueSession(sessionId, () => reconcileExternallyRemovedLeaseTarget(
      sessionId,
      record,
      "tab-removed",
      {
        is_window_closing: Boolean(removeInfo?.isWindowClosing),
        removed_window_id: removeInfo?.windowId,
      }
    )).catch((error) => console.error("Could not reconcile removed Comet Control tab:", error));
  }
});

chrome.windows.onRemoved.addListener((windowId) => {
  for (const [sessionId, record] of sessionLeases.entries()) {
    if (record.windowId !== windowId || record.closing) continue;
    enqueueSession(sessionId, () => reconcileExternallyRemovedLeaseTarget(
      sessionId,
      record,
      "window-removed",
      { removed_window_id: windowId }
    )).catch((error) => console.error("Could not reconcile removed Comet Control window:", error));
  }
});

// ---- Content Script Messaging ----

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  // Drop the optimistic injected cache on navigation. Do NOT clear scripting
  // poison here — see invalidateTabInjection.
  if (changeInfo.status === "loading" || changeInfo.url) {
    injectedTabs.delete(tabId);
  }
});

async function probeContentScript(tabId, timeoutMs = 1500) {
  try {
    // A half-dead content script can leave sendMessage pending forever; that
    // previously exhausted the ensureContentScript budget with no re-inject.
    const response = await withTimeout(
      sendContentScriptMessage(tabId, "getStatus", [], 0),
      timeoutMs,
      "probeContentScript"
    );
    if (response?.success) {
      injectedTabs.add(tabId);
      return true;
    }
  } catch {
    return false;
  }
  return false;
}

async function waitForTabComplete(tabId, attempts = 40) {
  for (let i = 0; i < attempts; i += 1) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab?.id) return null;
    if (tab.status === "complete") return tab;
    await sleep(100);
  }
  return chrome.tabs.get(tabId).catch(() => null);
}

// Serialize ensure/inject per tab so onUpdated identity refresh, retries, and
// click paths never overlap executeScript (Comet hangs the second call).
const ensureContentScriptChain = new Map();

async function ensureContentScript(tabId, options = {}) {
  const prev = ensureContentScriptChain.get(tabId) || Promise.resolve();
  const next = prev.catch(() => {}).then(() => ensureContentScriptUnlocked(tabId, options));
  ensureContentScriptChain.set(tabId, next);
  try {
    return await next;
  } finally {
    if (ensureContentScriptChain.get(tabId) === next) {
      ensureContentScriptChain.delete(tabId);
    }
  }
}

async function ensureContentScriptUnlocked(tabId, { force = false } = {}) {
  let tab = await waitForTabComplete(tabId);
  if (!tab?.id) {
    return { injected: false, blocked: true, reason: "Tab is no longer available" };
  }
  if (!isControllableUrl(tab.url)) {
    return { injected: false, blocked: true, reason: unsupportedUrlReason(tab.url), url: tab.url };
  }
  // Prefer sendMessage recovery over inject when Comet scripting is wedged.
  if (tabScriptingPoisoned.has(tabId)) {
    if (await probeContentScript(tabId)) {
      return { injected: true, url: tab.url, poisoned: true };
    }
    return {
      injected: false,
      blocked: true,
      reason: "Tab scripting wedged after executeScript timeout; wait for navigation",
      url: tab.url,
    };
  }
  const mustForce = force || contentScriptForceReinjection.has(tabId);
  // Prefer probe even when force is set — force often means "message failed
  // once", not "script is gone". Injecting while a healthy script exists is how
  // Accept→Dispatch wedges Comet's scripting FIFO.
  if (await probeContentScript(tabId)) {
    contentScriptForceReinjection.delete(tabId);
    return { injected: true, url: tab.url };
  }
  // SPA remount can finish AFTER a successful page_context. Few full probes —
  // short probes false-negative when the cursor script is busy and then we
  // unnecessarily clear+inject (Accept→Dispatch hang).
  tab = await waitForTabComplete(tabId);
  if (!tab?.id) {
    return { injected: false, blocked: true, reason: "Tab is no longer available" };
  }
  for (let i = 0; i < 4; i += 1) {
    if (await probeContentScript(tabId, 1500)) {
      contentScriptForceReinjection.delete(tabId);
      return { injected: true, url: tab.url };
    }
    await sleep(250);
  }
  // Manifest content_scripts reinject after full document loads. Give that
  // path one more chance. Do NOT programmatic-inject: on this Comet+Seller
  // path even a no-op executeScript hangs after Accept.
  // Click handlers recover with one tab reload instead.
  await sleep(500);
  if (await probeContentScript(tabId, 1500)) {
    contentScriptForceReinjection.delete(tabId);
    return { injected: true, url: tab.url, via: "manifest-settle" };
  }
  return {
    injected: false,
    blocked: true,
    reason: "Content script missing after SPA remount; reload required",
    url: tab.url,
    retryable: true,
  };
}


const VIEWPORT_CAPTURE_MS = 8000;

async function captureVisibleTabBounded(windowId, options, ms = VIEWPORT_CAPTURE_MS) {
  const capturePromise = chrome.tabs.captureVisibleTab(windowId, options);
  try {
    return await withTimeout(capturePromise, ms, "tabs.captureVisibleTab");
  } catch (error) {
    // chrome.tabs.captureVisibleTab cannot be aborted. Keep the abandoned
    // capture off the next viewport slot until it settles or this bound ends,
    // but return to the session FIFO immediately via withTimeout above so a
    // hung screenshot cannot wedge local-exec for minutes.
    await Promise.race([
      capturePromise.then(() => {}, () => {}),
      sleep(Math.max(500, Math.min(Number(ms) || VIEWPORT_CAPTURE_MS, VIEWPORT_CAPTURE_MS))),
    ]);
    const wrapped = new Error(`tabs.captureVisibleTab timed out after ${ms}ms`);
    wrapped.code = "SCREENSHOT_TIMEOUT";
    wrapped.cause = error;
    throw wrapped;
  }
}

function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`${label} timed out after ${ms}ms`)), ms
    );
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

// Serialize chrome.scripting.executeScript per tab on the RAW promise.
// withTimeout must not release the queue early — abandoned ops stay in Comet's
// per-tab FIFO and wedge every later inject (Accept→Dispatch failure mode).
const tabScriptingChain = new Map();

function enqueueTabScripting(tabId, start) {
  const prev = tabScriptingChain.get(tabId) || Promise.resolve();
  const next = prev.catch(() => {}).then(start);
  tabScriptingChain.set(
    tabId,
    next.then(
      () => {},
      () => {}
    )
  );
  return next;
}

async function executeScriptOnTab(tabId, options, ms, label) {
  if (tabScriptingPoisoned.has(tabId)) {
    throw new Error(`${label || "executeScript"} blocked: tab scripting wedged`);
  }
  // chrome.debugger.attach and chrome.scripting.executeScript contend on the
  // same tab; a stuck debugger session makes even `() => true` hang (Dispatch
  // injectCanary). Detach before any scripting op.
  if (attachedTabs.has(tabId)) {
    await chrome.debugger.detach({ tabId }).catch(() => {});
    attachedTabs.delete(tabId);
  }
  return enqueueTabScripting(tabId, async () => {
    const scriptPromise = chrome.scripting.executeScript(options);
    try {
      const value = await withTimeout(scriptPromise, ms, label);
      tabScriptingPoisoned.delete(tabId);
      return value;
    } catch (error) {
      tabScriptingPoisoned.add(tabId);
      // Keep queue on raw settle (bounded) so we do not start a second inject
      // while Comet still holds the first.
      await Promise.race([
        scriptPromise.then(
          () => {},
          () => {}
        ),
        sleep(Math.max(500, Math.min(Number(ms) || 5000, 8000))),
      ]);
      throw error;
    }
  });
}

function sendContentScriptMessage(tabId, action, args = [], frameId) {
  return chrome.tabs.sendMessage(
    tabId,
    { action, args },
    { frameId: Number.isInteger(frameId) ? frameId : 0 }
  );
}

async function sendToContentScript(tabId, action, args = [], frameId) {
  // Pin page_context / getStatus to the main document (frame 0). Ad frames are
  // controllable https and must not win getPageContext/getStatus.
  if (action === "getPageContext" || action === "getStatus") {
    frameId = 0;
  }
  // Post-navigation SPA updates (Seller Dispatch, checkout, etc.) routinely
  // exceed a short inject budget. Outer ensure budget must cover wait+inject.
  // Critical: never force-reinject immediately after an inject timeout — that
  // stacks zombie executeScript ops and wedges page_context. Prefer probe.
  //
  // Fast path: try sendMessage before ensure/inject. page_context often proves
  // the script is alive; Dispatch then failed because ensure's probe+inject
  // path hit a wedged scripting FIFO even though sendMessage would work.
  const ensureMs = 32000;
  const messageMs = 12000;
  // Keep findPointByText / click fast-path short. A hung content-script action
  // used to burn 10s then mis-report "Content script missing" from ensure.
  const clickPath = [
    "findPointByText",
    "findPointBySelector",
    "click",
    "moveToAndWait",
    "hasSelector",
  ].includes(action);
  const fastMs = ["getStatus", "getPageContext"].includes(action) ? 4000 : 5000;
  let connectionLost = false;
  try {
    const direct = await withTimeout(
      sendContentScriptMessage(tabId, action, args, frameId),
      fastMs,
      `sendMessageFast(${action})`
    );
    if (direct != null) {
      injectedTabs.add(tabId);
      contentScriptForceReinjection.delete(tabId);
      return direct;
    }
  } catch (fastError) {
    const fastMsg = String(fastError?.message || fastError);
    connectionLost = /Receiving end does not exist|Could not establish connection/i.test(fastMsg);
    if (clickPath) {
      // Probe-once then fail. A missing locator / hung click must not spend
      // ensureMs+ensureMs, and must not executeScript (Seller Accept→Dispatch hang).
      const alive = await probeContentScript(tabId, 800);
      if (alive && !connectionLost) {
        throw codedError("CONTENT_SCRIPT_TIMEOUT", `Content script action timed out: ${action}`);
      }
      if (alive) {
        throw codedError(
          "CONTENT_SCRIPT_FRAME_MISSING",
          `Content script is not available in frame for ${action}`
        );
      }
      throw codedError(
        "CONTENT_SCRIPT_MISSING",
        "Content script missing after SPA remount; reload required",
        { retryable: true }
      );
    }
    if (!connectionLost && /timed out/i.test(fastMsg)) {
      // Sync DOM work (e.g. findPointByText) can occupy the CS thread past fastMs.
      // Probes then false-negative and ensure mislabels "missing after SPA remount".
      // Retry once with a longer budget before the ensure/missing path.
      try {
        const retry = await withTimeout(
          sendContentScriptMessage(tabId, action, args, frameId),
          Math.max(fastMs, 8000),
          `sendMessageRetry(${action})`
        );
        if (retry != null) {
          injectedTabs.add(tabId);
          contentScriptForceReinjection.delete(tabId);
          return retry;
        }
      } catch (retryError) {
        const retryMsg = String(retryError?.message || retryError);
        connectionLost = /Receiving end does not exist|Could not establish connection/i.test(retryMsg);
        if (!connectionLost && await probeContentScript(tabId, 800)) {
          throw new Error(`Content script action timed out: ${action}`);
        }
      }
    }
    /* else fall through to ensure / missing */
  }
  let status;
  try {
    status = await withTimeout(
      ensureContentScript(tabId), ensureMs, `ensureContentScript(${action})`
    );
  } catch {
    await sleep(400);
    if (await probeContentScript(tabId)) {
      status = { injected: true };
    } else if (!tabScriptingPoisoned.has(tabId)) {
      status = await withTimeout(
        ensureContentScript(tabId), ensureMs, `ensureContentScript(${action})`
      );
    } else {
      throw new Error("Content script is not available (tab scripting wedged)");
    }
  }
  if (!status.injected) {
    throw new Error(status.reason || "Content script is not available");
  }
  try {
    const result = await withTimeout(
      sendContentScriptMessage(tabId, action, args, frameId), messageMs, `sendMessage(${action})`
    );
    // Soft failures still mean the old script may be stale after SPA remount.
    // Mark for reinject on the *next* ensure — do not inject inline here.
    if (
      result
      && result.success === false
      && action !== "getStatus"
      && action !== "getPageContext"
    ) {
      contentScriptForceReinjection.add(tabId);
    }
    return result;
  } catch {
    contentScriptForceReinjection.add(tabId);
    await sleep(400);
    if (await probeContentScript(tabId)) {
      return await withTimeout(
        sendContentScriptMessage(tabId, action, args, frameId), messageMs, `sendMessage(${action})`
      );
    }
    if (tabScriptingPoisoned.has(tabId)) {
      throw new Error("Content script is not available (tab scripting wedged)");
    }
    const retryStatus = await withTimeout(
      ensureContentScript(tabId, { force: true }), ensureMs, `ensureContentScript(${action})`
    );
    if (!retryStatus.injected) {
      throw new Error(retryStatus.reason || "Content script is not available");
    }
    return await withTimeout(
      sendContentScriptMessage(tabId, action, args, frameId), messageMs, `sendMessage(${action})`
    );
  }
}

// ---- Browser Actions ----
function assertRequestLive(state) {
  if (state.cancelled) throw new Error("Agent browser request was cancelled during closeout");
  if (state.deadlineAt && Date.now() >= state.deadlineAt) {
    throw new Error("Agent browser request reached its extension deadline");
  }
}

async function boundedWait(ms, state) {
  assertRequestLive(state);
  const remaining = state.deadlineAt ? state.deadlineAt - Date.now() : ms;
  const wait = Math.max(0, Math.min(Number(ms || 0), remaining));
  await sleep(wait);
  assertRequestLive(state);
}

async function runBrowserAction(action, state) {
  assertRequestLive(state);
  const type = action.type;
  if (action.tabId) {
    if (state.strictLease && Number(action.tabId) !== Number(state.tabId)) {
      throw new Error("A leased agent cannot target another session's tab");
    }
    state.tabId = action.tabId;
  }

  if (type === "goto") {
    const network = tabNetworkCdp.get(state.tabId);
    if (network) clearNetworkState(network);
    tabConsoleCdp.set(state.tabId, []);
    await ensureAttached(state.tabId).catch(() => {});
    const current = await chrome.tabs.get(state.tabId);
    let tab;
    if (current.url === action.url) {
      // tabs.update is a no-op for the current URL. A real goto must refresh
      // stale service-worker/cache fallbacks such as TickerTape maintenance.
      await chrome.tabs.reload(state.tabId, { bypassCache: action.bypassCache !== false });
      tab = await chrome.tabs.get(state.tabId);
    } else {
      tab = await chrome.tabs.update(state.tabId, { url: action.url, active: false });
    }
    state.tabId = tab.id;
    state.lastUrl = tab.url || action.url;
    await boundedWait(action.waitMs || 2000, state);
    // Navigation always invalidates the previous content-script world.
    invalidateTabInjection(state.tabId);
    // Do not pre-attach the debugger here. debugger + scripting.executeScript
    // on the same tab wedges Seller Accept→Dispatch (injectCanary timeout).
    // Screenshots/network attach lazily when those actions run.
    // Manifest content_scripts + ensure via sendMessage.
    await ensureContentScript(state.tabId).catch(() => {});
    if (state.leaseRecord) {
      state.leaseRecord.lastSeen = Date.now();
      await setAgentIdentity(state.tabId, state.leaseRecord);
      await persistSessionLeases().catch(() => {});
    }
    return { type, tabId: state.tabId, url: tab.url || action.url };
  }

  if (type === "back" || type === "forward" || type === "reload_page") {
    const network = tabNetworkCdp.get(state.tabId);
    if (network) clearNetworkState(network);
    tabConsoleCdp.set(state.tabId, []);
    await ensureAttached(state.tabId).catch(() => {});
    if (type === "back" || type === "forward") {
      const history = await send(state.tabId, "Page.getNavigationHistory");
      const offset = type === "back" ? -1 : 1;
      const target = history?.entries?.[Number(history.currentIndex) + offset];
      if (!target?.id) throw new Error(`Cannot find a ${type === "back" ? "previous" : "next"} page in history`);
      await send(state.tabId, "Page.navigateToHistoryEntry", { entryId: target.id });
    }
    if (type === "reload_page") {
      await chrome.tabs.reload(state.tabId, { bypassCache: Boolean(action.bypassCache) });
    }
    await boundedWait(action.waitMs || 1000, state);
    invalidateTabInjection(state.tabId);
    // Full reload can clear a wedged scripting FIFO — allow a fresh canary later.
    tabScriptingPoisoned.delete(state.tabId);
    await ensureContentScript(state.tabId).catch(() => {});
    if (state.leaseRecord) await setAgentIdentity(state.tabId, state.leaseRecord).catch(() => {});
    const tab = await chrome.tabs.get(state.tabId).catch(() => null);
    return { type, tabId: state.tabId, url: tab?.url || "" };
  }

  if (type === "wait") {
    await boundedWait(action.ms || 1000, state);
    return { type, ms: action.ms || 1000 };
  }

  if (type === "wait_for_selector") {
    const selector = String(action.selector || "");
    const timeout = action.timeout || 5000;
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      assertRequestLive(state);
      try {
        const response = await sendToContentScript(state.tabId, "hasSelector", [selector]);
        if (response?.result) return { type, selector };
      } catch { /* page still loading — retry */ }
      await boundedWait(250, state);
    }
    throw new Error(`wait_for_selector: "${selector}" not found after ${timeout}ms`);
  }

  if (type === "wait_for_url_change") {
    const timeout = action.timeout || 5000;
    const deadline = Date.now() + timeout;
    const anchor = await chrome.tabs.get(state.tabId).catch(() => null);
    if (!anchor) throw new Error("wait_for_url_change: tab not found");
    const fromUrl = action.from_url || anchor.url;
    while (Date.now() < deadline) {
      assertRequestLive(state);
      const current = await chrome.tabs.get(state.tabId).catch(() => null);
      if (!current) throw new Error("wait_for_url_change: tab was closed");
      if (current.url !== fromUrl && current.url !== "about:blank") {
        invalidateTabInjection(state.tabId);
        if (state.leaseRecord) {
          state.leaseRecord.lastSeen = Date.now();
          await setAgentIdentity(state.tabId, state.leaseRecord).catch(() => {});
        }
        return { type, from: fromUrl, url: current.url };
      }
      await boundedWait(250, state);
    }
    throw new Error(`wait_for_url_change: URL still "${fromUrl}" after ${timeout}ms`);
  }

  if (type === "text") {
    const limit = action.maxChars || state.maxTextChars || 20000;
    const resp = await sendToContentScript(state.tabId, "getVisibleText", [limit]);
    const r = resp?.result;
    if (r && typeof r === 'object') return { type, url: r.url, title: r.title, text: r.text || '' };
    return { type, text: String(r || '') };
  }

  if (type === "snapshot") {
    const tab = await chrome.tabs.get(state.tabId).catch(() => null);
    const resp = await sendToContentScript(state.tabId, "getDOMSnapshot");
    const snapshot = requireContentScriptResult(resp, "DOM snapshot returned no result");
    const elements = Array.isArray(snapshot) ? snapshot : (snapshot.elements || []);
    return {
      type,
      url: tab?.url || '',
      title: tab?.title || '',
      page_revision: snapshot.page_revision ?? null,
      element_count: elements.length,
      snapshot: elements,
    };
  }

  if (type === "page_context") {
    // Seller Dispatch / Buyer checkout remounts often leave status loading
    // briefly; wait for complete before inject so we do not trip inject timeout.
    for (let i = 0; i < 25; i += 1) {
      const tab = await chrome.tabs.get(state.tabId).catch(() => null);
      if (!tab) break;
      if (tab.status === "complete") break;
      await boundedWait(100, state);
    }
    // Do not call readPageConsoleTail / ensureConsoleProbe here. MAIN-world
    // executeScript after getPageContext raced the next click's inject and
    // wedged Seller Dispatch. Console rows come from CDP ring only.
    const resp = await sendToContentScript(state.tabId, "getPageContext", [], 0);
    const max = 20;
    const page = { entries: [] };
    const entries = mergeConsoleEntries(tabConsoleCdp.get(state.tabId) || [], page.entries || [], max);
    const error_count = consoleErrorCount(entries);
    const last_error = [...entries].reverse().find((e) =>
      e.level === "error" || e.level === "page_error" || e.level === "unhandledrejection" || e.level === "assert"
    ) || null;
    const network = tabNetworkCdp.get(state.tabId);
    const pageResult = resp?.result || {};
    // Surface OAuth/popup overlays Comet Control cannot see in captureVisibleTab so
    // agents hand off to macos-cua instead of replaying in-page clicks.
    const handoffHint = detectNativeOverlayHandoffHint({
      lastError: last_error,
      entries,
      page: pageResult,
    });
    return {
      type,
      ...pageResult,
      console_error_count: error_count,
      console_warn_count: entries.filter((e) => e.level === "warn").length,
      last_console_error: last_error ? { level: last_error.level, text: last_error.text, t: last_error.t } : null,
      ...(handoffHint ? { handoff_hint: handoffHint } : {}),
      network_capture_enabled: Boolean(network),
      ...(network ? {
        network_error_count: network.errors.length,
        last_network_error: network.errors[network.errors.length - 1] || null,
      } : {}),
    };
  }

  if (type === "console_tail") {
    await ensureContentScript(state.tabId);
    await ensureConsoleProbe(state.tabId).catch(() => {});
    await ensureAttached(state.tabId).catch(() => {});
    const max = Math.max(1, Math.min(Number(action.max ?? action.limit ?? 50) || 50, CONSOLE_RING_MAX));
    const clear = Boolean(action.clear);
    const page = await readPageConsoleTail(state.tabId, CONSOLE_RING_MAX, clear);
    const cdp = tabConsoleCdp.get(state.tabId) || [];
    const merged = mergeConsoleEntries(cdp, page.entries || [], CONSOLE_RING_MAX);
    const entries = filteredConsoleEntries(merged, action, max);
    if (clear) tabConsoleCdp.set(state.tabId, []);
    const levelCounts = {};
    for (const entry of entries) levelCounts[entry.level] = (levelCounts[entry.level] || 0) + 1;
    return {
      type,
      count: entries.length,
      error_count: consoleErrorCount(entries),
      level_counts: levelCounts,
      filter: String(action.filter || "") || null,
      entries
    };
  }

  if (type === "network_watch") {
    const capture = await ensureNetworkCapture(state.tabId, { clear: action.clear !== false });
    return { type, ...networkSummary(capture) };
  }

  if (type === "network_summary") {
    const captureStartedNow = !tabNetworkCdp.has(state.tabId);
    const capture = await ensureNetworkCapture(state.tabId, { clear: Boolean(action.clear) });
    return {
      type,
      ...networkSummary(capture),
      capture_started_now: captureStartedNow,
      ...(captureStartedNow ? {
        instruction: "Capture starts now; run network_watch before the navigation or action being diagnosed",
      } : {}),
    };
  }

  if (type === "network_tail" || type === "network_errors") {
    const captureStartedNow = !tabNetworkCdp.has(state.tabId);
    const capture = await ensureNetworkCapture(state.tabId);
    const max = Math.max(1, Math.min(Number(action.max ?? action.limit ?? 20) || 20, 100));
    const entries = filteredNetworkErrors(capture, action, max);
    const summary = networkSummary(capture);
    if (action.clear) clearNetworkState(capture);
    return {
      type,
      count: entries.length,
      total_error_count: summary.error_count,
      filter: String(action.filter || "") || null,
      kinds: Array.isArray(action.kinds) ? action.kinds : null,
      capture_started_now: captureStartedNow,
      ...(captureStartedNow ? {
        instruction: "Capture starts now; run network_watch before the navigation or action being diagnosed",
      } : {}),
      entries,
    };
  }

  if (type === "screenshot") {
    const format = action.format === 'png' ? 'png' : 'jpeg';
    // Viewport proof does not require CDP. captureVisibleTab avoids debugger
    // attachment failures caused by extension-owned frames. Explicitly activate
    // the leased tab first: a user or another extension may have opened a second
    // tab in this window, and captureVisibleTab otherwise captures that foreign tab.
    if (!action.full) {
      return enqueueViewportCapture(async () => {
        const leasedTab = await chrome.tabs.get(state.tabId);
        state.windowId = leasedTab.windowId;
        const options = { format };
        if (format === 'jpeg') options.quality = action.quality != null ? Number(action.quality) : 75;
        let dataUrl = null;
        let captureAttempts = 0;
        let source = "tabs.captureVisibleTab";
        await chrome.tabs.update(state.tabId, { active: true });
        const [activeTab] = await chrome.tabs.query({ active: true, windowId: leasedTab.windowId });
        if (!activeTab || Number(activeTab.id) !== Number(state.tabId)) {
          throw new Error("Could not activate the leased tab for screenshot proof");
        }
        for (let attempt = 0; attempt < 2; attempt += 1) {
          captureAttempts = attempt + 1;
          try {
            await waitForViewportCaptureSlot();
            dataUrl = await captureVisibleTabBounded(leasedTab.windowId, options);
            break;
          } catch (error) {
            const transientReadback = /image readback failed/i.test(String(error?.message || error));
            if (transientReadback && attempt === 0) {
              await chrome.scripting.executeScript({
                target: { tabId: state.tabId },
                func: () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
              });
              continue;
            }
            break;
          }
        }
        if (!dataUrl) {
          await sendToContentScript(state.tabId, "captureScreenshot");
          await ensureAttached(state.tabId);
          const params = { format };
          if (format === 'jpeg') params.quality = action.quality != null ? Number(action.quality) : 75;
          const capture = await withTimeout(
            chrome.debugger.sendCommand(
              { tabId: state.tabId },
              "Page.captureScreenshot",
              params
            ),
            VIEWPORT_CAPTURE_MS,
            "Page.captureScreenshot"
          );
          return {
            type,
            format,
            base64: capture.data,
            source: "cdp.Page.captureScreenshot",
            capture_attempts: captureAttempts,
          };
        }
        return {
          type,
          format,
          base64: dataUrl.slice(dataUrl.indexOf(',') + 1),
          source,
          capture_attempts: captureAttempts,
        };
      });
    }

    await ensureAttached(state.tabId);
    // Full-page capture still requires CDP. Use JPEG by default because PNG can
    // exceed Comet's 1 MB native-messaging limit.
    const params = { format, captureBeyondViewport: Boolean(action.full) };
    if (format === 'jpeg') params.quality = action.quality != null ? Number(action.quality) : 75;
    const capture = await chrome.debugger.sendCommand(
      { tabId: state.tabId },
      "Page.captureScreenshot",
      params
    );
    return { type, format, base64: capture.data };
  }

  if (type === "zoom") {
    await ensureAttached(state.tabId);
    const { x0 = 0, y0 = 0, x1, y1 } = action;
    if (x1 == null || y1 == null) throw new Error("zoom requires x0, y0, x1, y1");
    const quality = action.quality != null ? Number(action.quality) : 85;
    const capture = await chrome.debugger.sendCommand(
      { tabId: state.tabId },
      "Page.captureScreenshot",
      {
        format: 'jpeg',
        quality,
        clip: { x: Number(x0), y: Number(y0), width: Number(x1) - Number(x0), height: Number(y1) - Number(y0), scale: 1 },
        captureBeyondViewport: true,
      }
    );
    return { type, format: 'jpeg', x0, y0, x1, y1, base64: capture.data };
  }

  if (type === "close_tab") {
    if (state.strictLease) throw new Error("Use closeout to release an agent-owned tab/window");
    const tab = await chrome.tabs.get(state.tabId).catch(() => null);
    if (tab?.url) state.lastUrl = tab.url;
    await chrome.tabs.remove(state.tabId);
    const closed = state.tabId;
    state.tabId = undefined;
    return { type, tabId: closed, url: state.lastUrl };
  }

  // ---- Cursor overlay actions (visible to user) ----
  if (type === "cursor_move") {
    const { x, y } = action;
    await sendToContentScript(state.tabId, "moveTo", [x, y]);
    return { type, x, y };
  }

  if (type === "cursor_click") {
    await sendToContentScript(state.tabId, "click", []);
    return { type };
  }

  if (type === "cursor_right_click") {
    await sendToContentScript(state.tabId, "rightClick", []);
    return { type };
  }

  if (type === "cursor_double_click") {
    await sendToContentScript(state.tabId, "dblClick", []);
    return { type };
  }

  if (type === "cursor_triple_click") {
    await sendToContentScript(state.tabId, "tripleClick", []);
    return { type };
  }

  if (type === "cursor_type") {
    const { text, append } = action;
    await sendToContentScript(state.tabId, "focusAndType", [text, { append: !!append }]);
    return { type, text };
  }

  if (type === "cursor_key") {
    const { key, modifiers } = action;
    await sendToContentScript(state.tabId, "showKey", [key]);
    await pressKey(send, state.tabId, key, modifiers || []);
    return { type, key };
  }

  if (type === "cursor_drag") {
    const { x, y, duration } = action;
    await sendToContentScript(state.tabId, "dragTo", [x, y, duration || 500]);
    return { type, x, y };
  }

  if (type === "cursor_scroll") {
    const { deltaX, deltaY } = action;
    await sendToContentScript(state.tabId, "scroll", [deltaX || 0, deltaY || 0], 0);
    return { type, deltaX, deltaY };
  }

  if (type === "cursor_status") {
    let resp = await sendToContentScript(state.tabId, "getStatus", [], 0);
    let status = resp?.result || {};
    // After navigation races, reinject + re-label once before failing the contract.
    if (state.leaseRecord && (!status.agent_label || status.agent_label !== state.leaseRecord.agentLabel)) {
      invalidateTabInjection(state.tabId);
      await setAgentIdentity(state.tabId, state.leaseRecord);
      resp = await sendToContentScript(state.tabId, "getStatus", [], 0);
      status = resp?.result || {};
    }
    return { type, ...status };
  }

  if (type === "cursor_hide") {
    await sendToContentScript(state.tabId, "hide", []);
    return { type };
  }

  // ---- Extension interaction actions ----
  if (type === "click_text") {
    // Seller Dispatch (and similar) call window.prompt/confirm/alert. That freezes
    // the content-script click reply; without a CDP dialog race the SW mislabels
    // the hang as "Content script missing" and reload recovery dismisses the prompt.
    // Attach debugger first so Page.javascriptDialogOpening is observed.
    // Use clickResolvedTarget (same as click_selector) so CLICK_TARGET_MISMATCH
    // re-resolves once and reports retried:true for moved text targets.
    const debuggerAttached = await attachForClick(state.tabId);
    const runDialogAware = async () => {
      const clickPromise = withTimeout(
        clickResolvedTarget(
          state.tabId,
          () => findPointByText(state.tabId, action.text),
          { text: action.text }
        ),
        15000,
        "clickResolvedTarget"
      );
      if (!debuggerAttached) return clickPromise;
      const outcome = await raceClickWithDialog(state.tabId, clickPromise, 8000);
      if (outcome.dialog) {
        return {
          dialog_opened: outcome.dialog,
          click_pending: true,
        };
      }
      return outcome.value || {};
    };
    try {
      const result = await runDialogAware();
      return { type, text: action.text, ...result };
    } catch (error) {
      if (String(error?.code || "").startsWith("ACTIONABILITY_")) throw error;
      const openDialog = getParityDialog(state.tabId);
      if (openDialog) {
        return {
          type,
          text: action.text,
          dialog_opened: openDialog,
          click_pending: true,
          first_error: String(error?.message || error?.error || error),
        };
      }
      if (!debuggerAttached || isForeignExtensionRestriction(error)) throw error;
      if (isLocatorMissError(error) || isContentScriptTimeoutError(error)) throw error;
      const msg = String(error?.message || error?.error || error);
      // One hard reload recovery when there is no JS dialog. Same-URL tabs.update
      // is a Comet no-op — always reload so manifest content_scripts remount.
      // Missing locators, CS timeouts, and foreign-extension misses must not
      // enter this path (live miss on a 404 must fail fast, not reload).
      if (!isHalfDeadReloadError(error)) throw error;
      try {
        const tabBefore = await chrome.tabs.get(state.tabId).catch(() => null);
        const targetUrl = tabBefore?.url || "";
        tabScriptingPoisoned.delete(state.tabId);
        invalidateTabInjection(state.tabId);
        await chrome.tabs.reload(state.tabId, { bypassCache: true });
        await boundedWait(3500, state);
        let ready = false;
        for (let i = 0; i < 40; i += 1) {
          if (await probeContentScript(state.tabId, 500)) {
            ready = true;
            break;
          }
          await sleep(200);
        }
        if (!ready) {
          await executeScriptOnTab(
            state.tabId,
            {
              target: { tabId: state.tabId },
              files: ["content-scripts/cursor-agent.js"],
            },
            12000,
            "injectContentScriptAfterReload"
          ).catch(() => {});
          ready = await probeContentScript(state.tabId, 1500);
        }
        if (!ready) {
          throw new Error(
            `Content script still missing after navigate (${targetUrl || "unknown url"}); first error: ${msg}`
          );
        }
        if (state.leaseRecord) await setAgentIdentity(state.tabId, state.leaseRecord).catch(() => {});
        for (let i = 0; i < 30; i += 1) {
          try {
            const ctx = await withTimeout(
              sendContentScriptMessage(state.tabId, "getPageContext", [], 0),
              3000,
              "recoverPageContext"
            );
            const buttons = ctx?.result?.buttons || [];
            if (buttons.some((b) => String(b).includes(String(action.text || "")))) break;
          } catch {
            /* retry */
          }
          await sleep(250);
        }
        await ensureAttached(state.tabId);
        const result = await runDialogAware();
        return { type, text: action.text, ...result, recovered_via: "navigate", first_error: msg };
      } catch (recoverErr) {
        const dialogAfter = getParityDialog(state.tabId);
        if (dialogAfter) {
          return {
            type,
            text: action.text,
            dialog_opened: dialogAfter,
            click_pending: true,
            first_error: msg,
            recover_error: String(recoverErr?.message || recoverErr),
          };
        }
        throw new Error(
          `click_text(${action.text}) failed: ${msg}; recover: ${recoverErr?.message || recoverErr}`
        );
      }
    }
  }

  if (type === "fill_selector") {
    const result = await clickResolvedTarget(
      state.tabId,
      () => findPointBySelector(state.tabId, action.selector, "fill"),
      { selector: action.selector }
    );
    const response = await sendToContentScript(state.tabId, "focusAndType", [
      String(action.value || ""),
      { append: Boolean(action.append) },
      {
        selector: action.selector,
        targetToken: result.point.target_token || "",
        pageRevision: result.point.page_revision,
        editable: true,
      }
    ], result.point.frame_id);
    requireContentScriptResult(response, `Could not fill selector: ${action.selector}`);
    return { type, selector: action.selector, ...result };
  }

  if (type === "click_selector") {
    const debuggerAttached = await attachForClick(state.tabId);
    const clickPromise = withTimeout(
      clickResolvedTarget(
        state.tabId,
        () => findPointBySelector(state.tabId, action.selector),
        { selector: action.selector }
      ),
      15000,
      "clickResolvedTarget"
    );
    if (!debuggerAttached) {
      const result = await clickPromise;
      return { type, selector: action.selector, ...result };
    }
    const outcome = await raceClickWithDialog(state.tabId, clickPromise, 8000);
    if (outcome.dialog) {
      return {
        type,
        selector: action.selector,
        dialog_opened: outcome.dialog,
        click_pending: true,
      };
    }
    return { type, selector: action.selector, ...(outcome.value || {}) };
  }

  const parity = await runParityAction(action, state, {
    send,
    ensureAttached,
    moveCursorToPoint,
  });
  if (parity.handled) return parity.result;

  if (type === "evaluate") {
    const result = await evaluateReadOnly(state.tabId, action.expression, send);
    return { type, result };
  }

  throw new Error(`Unsupported action type: ${type}`);
}

function hostMessageIsReadOnly(message) {
  // A lease renewal changes only persisted ownership metadata. Treat it as
  // nonvisual so the owning browser campaign can stay alive while a disjoint
  // macOS CUA claim temporarily owns focus and input.
  return ["status", "sessions", "session_renew"].includes(message?.type);
}

function codedError(code, message, { retryable = false } = {}) {
  const error = new Error(message);
  error.code = code;
  error.retryable = Boolean(retryable);
  return error;
}

function compactFailureAction(action, index) {
  const type = String(action?.type || "unknown");
  return {
    index,
    type,
    ...(action?.selector ? { selector: String(action.selector).slice(0, 200) } : {}),
    ...(action?.text ? { text: String(action.text).slice(0, 120) } : {}),
    ...(action?.ref ? { ref: String(action.ref).slice(0, 120) } : {}),
  };
}

async function captureFailureRecord({ message, state, action, index, beforeUrl, startedAt, error }) {
  const tab = await chrome.tabs.get(state.tabId).catch(() => null);
  const consoleTail = (tabConsoleCdp.get(state.tabId) || []).slice(-10);
  const network = tabNetworkCdp.get(state.tabId);
  const networkTail = Array.isArray(network?.errors) ? network.errors.slice(-10) : [];
  let screenshot = null;
  if (action?.type !== "screenshot" && error?.code !== "SCREENSHOT_TIMEOUT") {
    screenshot = await runBrowserAction(
      { type: "screenshot", format: "jpeg", quality: 60 },
      state
    ).catch(() => null);
  }
  return {
    version: 1,
    command_id: String(message.id || ""),
    session_id: String(message.sessionId || ""),
    started_at_ms: startedAt,
    elapsed_ms: Date.now() - startedAt,
    action: compactFailureAction(action, index),
    before_url: beforeUrl || null,
    after_url: tab?.url || null,
    error_code: error?.code || "BROWSER_ACTION_FAILED",
    error: String(error?.message || error).slice(0, 2000),
    ...(error?.details ? { details: error.details } : {}),
    console_tail: consoleTail,
    network_tail: networkTail,
    ...(screenshot ? { screenshot } : {}),
  };
}

function detectNativeOverlayHandoffHint({ lastError, entries, page }) {
  const texts = [];
  if (lastError?.text) texts.push(String(lastError.text));
  for (const entry of entries || []) {
    if (entry?.text) texts.push(String(entry.text));
  }
  const blob = texts.join("\n").toLowerCase();
  const buttons = Array.isArray(page?.buttons) ? page.buttons.map((b) => String(b).toLowerCase()) : [];
  const popupBlocked =
    blob.includes("failed to open popup")
    || blob.includes("maybe blocked by the browser")
    || blob.includes("gsi_logger");
  const googleOauth =
    blob.includes("accounts.google.com/gsi")
    || blob.includes("accounts.google.com")
    || buttons.some((b) => b.includes("continue with google"));
  if (popupBlocked && googleOauth) {
    return {
      reason: "oauth_popup_or_native_overlay",
      owner: "macos-cua",
      intent: "native-dialog",
      action: "cua_slice",
      detail:
        "Google/OAuth UI is likely a popup or OS overlay Comet Control page screenshots cannot see. Use skills/comet-control/scripts/cua_slice.py on this lease.",
    };
  }
  if (popupBlocked) {
    return {
      reason: "popup_blocked",
      owner: "macos-cua",
      intent: "native-dialog",
      action: "cua_slice",
      detail:
        "Browser blocked a popup. Inspect via macos-cua native-dialog (cua_slice) rather than repeating in-page clicks.",
    };
  }
  return null;
}

async function acquireCuaRuntimeClaim(message) {
  await requireSessionStateReady();
  const intent = String(message.intent || "").trim();
  if (!["native-dialog", "comet-admin"].includes(intent)) {
    throw codedError("COMET_CONTROL_HANDOFF_REQUIRED", `Unsupported CUA handoff intent: ${intent || "missing"}`);
  }
  // Authenticate native-dialog before reclaim so only the owning lease can
  // replace an orphan claim left by a dead macos-cua process.
  const owningLease = intent === "native-dialog" ? requireSessionLease(message) : null;
  const existing = activeCuaClaim();
  let reclaimed = false;
  if (existing) {
    const sameSession =
      intent === "native-dialog"
      && message.reclaim !== false
      && existing.intent === "native-dialog"
      && existing.sessionId
      && owningLease
      && existing.sessionId === owningLease.sessionId;
    if (sameSession) {
      cuaRuntimeClaim = null;
      await persistCuaClaim().catch(() => {});
      reclaimed = true;
    } else {
      throw codedError(
        "CUA_RUNTIME_CLAIMED",
        `Managed Comet is already claimed for ${existing.intent} until ${existing.expiresAt}`
      );
    }
  }
  if (activeHostMutations > 0) {
    throw codedError(
      "COMET_CONTROL_RUNTIME_BUSY",
      `Cannot claim managed Comet while ${activeHostMutations} browser mutation(s) are active`
    );
  }

  // Reserve synchronously before any further await. Every new browser mutation
  // observes this provisional claim and fails closed while validation/persist runs.
  const now = Date.now();
  const ttlMs = boundedNumber(
    message.ttlSeconds,
    DEFAULT_CUA_CLAIM_TTL_MS / 1000,
    15,
    300,
    "ttlSeconds"
  ) * 1000;
  const claim = {
    claimId: crypto.randomUUID(),
    claimToken: crypto.randomUUID(),
    intent,
    sessionId: intent === "native-dialog" ? requireSessionId(message.sessionId) : null,
    createdAt: now,
    expiresAt: now + ttlMs,
  };
  cuaRuntimeClaim = claim;
  try {
    // Claim acquisition must never close or move windows: native-dialog claims
    // originate before CUA takes focus, and admin validation may run while a
    // disjoint native-app command owns visual-focus-v1. Host-locked lifecycle
    // requests perform deferred cleanup.
    const reaped = [];
    if (intent === "comet-admin" && sessionLeases.size > 0) {
      throw codedError(
        "COMET_CONTROL_RUNTIME_BUSY",
        `Comet administration requires zero leases; found ${sessionLeases.size}`
      );
    }
    if (intent === "native-dialog") {
      const lease = sessionLeases.get(claim.sessionId);
      if (!lease || lease !== owningLease) {
        throw codedError(
          "COMET_CONTROL_HANDOFF_REQUIRED",
          "The requested Comet Control session does not own an active lease"
        );
      }
      if (lease.busy) {
        throw codedError(
          "COMET_CONTROL_SESSION_BUSY",
          "The owning Comet Control session is executing a browser command"
        );
      }
    }
    await persistCuaClaim();
    post({
      id: message.id,
      success: true,
      claim_token: claim.claimToken,
      claim: publicCuaClaim(claim),
      active_sessions: sessionLeases.size,
      reaped,
      reclaimed,
    });
  } catch (error) {
    if (cuaRuntimeClaim === claim) {
      cuaRuntimeClaim = null;
      await persistCuaClaim().catch(() => {});
    }
    throw error;
  }
}

async function releaseCuaRuntimeClaim(message) {
  const claim = activeCuaClaim();
  if (!claim) {
    post({ id: message.id, success: true, released: false, already_released: true });
    return;
  }
  if (!message.claimToken || message.claimToken !== claim.claimToken) {
    throw codedError("INVALID_CUA_CLAIM_TOKEN", "Invalid CUA runtime claim token");
  }
  cuaRuntimeClaim = null;
  await persistCuaClaim();
  post({ id: message.id, success: true, released: true, claim_id: claim.claimId });
}

function validateCuaRuntimeClaim(message) {
  const claim = activeCuaClaim();
  if (
    !claim
    || !message.claimToken
    || message.claimToken !== claim.claimToken
    || (message.intent && message.intent !== claim.intent)
  ) {
    throw codedError("INVALID_CUA_CLAIM_TOKEN", "No matching active CUA runtime claim");
  }
  post({ id: message.id, success: true, claim: publicCuaClaim(claim) });
}

async function handleHostMessage(message) {
  await requireCuaClaimStateReady();
  await controlPauseReady;
  const brokerDeadlineAt = Number(message?.deadlineAt);
  if (Number.isFinite(brokerDeadlineAt) && brokerDeadlineAt <= Date.now()) {
    throw codedError("REQUEST_EXPIRED", "Comet Control request expired before extension execution");
  }
  if (controlPaused && !PAUSE_ALLOWED_HOST_TYPES.has(message?.type)) {
    throw codedError("CONTROL_PAUSED", "Comet Control is paused by the operator");
  }
  if (message?.type === "cua_runtime_claim") {
    await acquireCuaRuntimeClaim(message);
    return;
  }
  if (message?.type === "cua_runtime_release") {
    await releaseCuaRuntimeClaim(message);
    return;
  }
  if (message?.type === "cua_runtime_validate") {
    validateCuaRuntimeClaim(message);
    return;
  }

  const readOnly = hostMessageIsReadOnly(message);
  const claim = activeCuaClaim();
  if (claim && !readOnly) {
    throw codedError(
      "CUA_RUNTIME_CLAIMED",
      `Managed Comet is reserved for ${claim.intent} until ${claim.expiresAt}`
    );
  }
  if (!readOnly) activeHostMutations += 1;
  try {
    await handleUnlockedHostMessage(message);
  } finally {
    if (!readOnly) activeHostMutations = Math.max(0, activeHostMutations - 1);
  }
}

async function handleUnlockedHostMessage(message) {
  if (message?.type === "reload") {
    await requireSessionStateReady();
    await reapExpiredSessions();
    if (sessionLeases.size > 0) {
      post({ id: message.id, success: false, error_code: "ACTIVE_AGENT_LEASES", error: `Cannot reload while ${sessionLeases.size} agent browser session(s) are active` });
      return;
    }
    post({ id: message.id, success: true, message: "reloading" });
    setTimeout(() => chrome.runtime.reload(), 150);
    return;
  }

  if (message?.type === "status") {
    await requireSessionStateReady();
    post({
      id: message.id,
      success: true,
      extension: "Comet Control Bridge",
      active_agent_sessions: sessionLeases.size,
      active_session_labels: Array.from(sessionLeases.values()).map((record) => record.agentLabel),
      paused: controlPaused,
      protocol_version: PROTOCOL_VERSION,
      extension_version: chrome.runtime.getManifest().version,
      extension_build_sha256: await extensionBuildSha256,
      ...brokerInfo,
      capabilities: EXTENSION_CAPABILITIES,
      cua_claim: publicCuaClaim(activeCuaClaim()),
      page_metadata: "lease-required",
    });
    return;
  }

  if (message?.type === "sessions") {
    await requireSessionStateReady();
    const leases = Array.from(sessionLeases.values())
      .filter((record) => !message.sessionId || record.sessionId === message.sessionId)
      .map((record) => {
        const { lease_token: _privateToken, ...safe } = publicLease(record);
        return safe;
      });
    const removals = await readLeaseRemovals(message.sessionId || "");
    post({
      id: message.id,
      success: true,
      sessions: leases,
      cua_claim: publicCuaClaim(activeCuaClaim()),
      reaped: [],
      maintenance_deferred: Boolean(deferredAgentWindowLayoutReason),
      removals,
    });
    return;
  }

  if (message?.type === "session_preflight") {
    const lease = await sessionPreflight(message);
    post({ id: message.id, success: true, ...lease });
    return;
  }

  if (message?.type === "session_renew") {
    const renewal = await renewSessionLease(message);
    post({ id: message.id, success: true, ...renewal });
    return;
  }

  if (message?.type === "session_closeout") {
    await requireSessionStateReady();
    const sessionId = requireSessionId(message.sessionId);
    const existing = sessionLeases.get(sessionId);
    if (existing && (!message.leaseToken || existing.leaseToken !== message.leaseToken)) {
      throw new Error(`Invalid browser lease token for ${sessionId}`);
    }
    if (message.keepWindow) {
      throw codedError(
        "KEEP_WINDOW_UNSUPPORTED",
        "Comet Control closeout cannot keep an authenticated agent browser surface open"
      );
    }
    const cleanup = await closeSession(sessionId, { reason: "closeout" });
    post({ id: message.id, success: true, ...cleanup });
    return;
  }

  if (message?.type === "user_tabs") {
    post({
      id: message.id,
      success: true,
      tabs: await listUserTabs({ filter: message.filter, limit: message.limit }),
    });
    return;
  }

  if (message?.type === "history") {
    post({
      id: message.id,
      success: true,
      history: await readBrowserHistory(message),
    });
    return;
  }

  if (message?.type !== "run") {
    throw new Error(`Unsupported host message type: ${message?.type}`);
  }

  if (!message.sessionId) {
    throw new Error("WIP Comet Control run requires session_preflight plus sessionId and leaseToken");
  }

  const timeoutMs = Math.max(
    1000,
    boundedNumber(message.timeoutSeconds, 90, 1, 300, "timeoutSeconds") * 1000 - 500
  );
  const extensionDeadlineAt = Math.min(
    Date.now() + timeoutMs,
    Number.isFinite(Number(message.deadlineAt)) ? Number(message.deadlineAt) - 500 : Infinity
  );
  if (extensionDeadlineAt <= Date.now()) {
    throw codedError("REQUEST_EXPIRED", "Comet Control request expired before browser execution");
  }

  await requireSessionStateReady();
  const leaseRecord = requireSessionLease(message);
  const runTargets = await readOwnedLeaseTargets(leaseRecord);
  if (!ownedLeaseTargetsComplete(leaseRecord, runTargets)) {
    // Run already owns the visual-focus lane, so it can safely close a partial
    // target. Centralized closeout retains the lease if any removal is unproven.
    await closeSession(leaseRecord.sessionId, { reason: "run-target-partial" });
    throw codedError(
      "LEASE_TARGET_MISSING",
      `Agent browser target for ${leaseRecord.sessionId} was partial or moved and has been closed; run preflight again`
    );
  }
  await flushDeferredAgentWindowLayout();
  const rememberedTab = leaseRecord.tabId;
  leaseRecord.busy = true;
  leaseRecord.lastSeen = Date.now();
  // Activate the leased tab inside its owned window. Do not focus the OS window.
  await chrome.tabs.update(leaseRecord.tabId, { active: true }).catch(() => {});

  const state = {
    maxTextChars: message.maxTextChars || 20000,
    tabId: rememberedTab,
    groupName: leaseRecord.sessionName,
    strictLease: true,
    leaseRecord,
    deadlineAt: extensionDeadlineAt,
    cancelled: false,
  };

  activeRunStates.add(state);
  try {
    const startedAt = Date.now();
    const beforeTab = await chrome.tabs.get(state.tabId).catch(() => null);
    let currentAction = null;
    let currentActionIndex = -1;
    const dialogControlOnly = (message.actions || []).length > 0
      && (message.actions || []).every((action) => ["dialog_get", "dialog_handle"].includes(action?.type));
    if (!dialogControlOnly) await setAgentIdentity(state.tabId, leaseRecord);
    const results = [];
    try {
      for (const [index, action] of (message.actions || []).entries()) {
        currentAction = action;
        currentActionIndex = index;
        assertRequestLive(state);
        const result = await runBrowserAction(action, state);
        assertRequestLive(state);
        if (result?.url) state.lastUrl = result.url;
        results.push(result);
      }
    } catch (error) {
      error.failureRecord = await captureFailureRecord({
        message,
        state,
        action: currentAction,
        index: currentActionIndex,
        beforeUrl: beforeTab?.url,
        startedAt,
        error,
      });
      throw error;
    }
    const tab = state.tabId ? await chrome.tabs.get(state.tabId).catch(() => null) : null;
    post({
      id: message.id,
      success: true,
      session_id: leaseRecord.sessionId,
      window_id: leaseRecord.windowId,
      tab_id: state.tabId,
      final_url: tab?.url || state.lastUrl,
      results,
    });
  } finally {
    activeRunStates.delete(state);
    if (leaseRecord && sessionLeases.get(leaseRecord.sessionId) === leaseRecord) {
      leaseRecord.busy = false;
      leaseRecord.lastSeen = Date.now();
      await persistSessionLeases();
    }
  }
}

connectHost();

// Keep-alive: MV3 service workers terminate after 30s idle.
// An alarm every ~25s resets the idle clock before it expires.
// Unpacked (dev-loaded) extensions have no minimum alarm interval.
chrome.alarms.create("comet-control-keepalive", { periodInMinutes: 25 / 60 });
chrome.alarms.onAlarm.addListener((_alarm) => {
  // MV3: if the worker woke from idle without a broker poll, reconnect first.
  if (!port) scheduleHostReconnect(0);
  // Keep-alive is deliberately nonvisual. Expiry cleanup runs only inside the
  // next broker lifecycle request while visual-focus-v1 is held.
});
