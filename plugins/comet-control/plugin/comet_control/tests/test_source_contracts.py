import base64
import hashlib
import json
import subprocess
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICE_WORKER = ROOT / "extension" / "service_worker.js"
CURSOR_AGENT = ROOT / "extension" / "content-scripts" / "cursor-agent.js"
PARITY_CAPABILITIES = ROOT / "extension" / "parity_capabilities.js"
LEASE_DRIVER = (
    ROOT.parents[1] / "skills" / "comet-control" / "scripts" / "lease_driver.py"
)
DASHBOARD_TEST = ROOT / "tests" / "test_dashboard_interactions.py"
ISOLATION_TEST = ROOT / "tests" / "test_multi_agent_isolation.py"
CANONICAL_SKILL = ROOT.parents[1] / "skills" / "comet-control" / "SKILL.md"
BROKER = ROOT / "native" / "broker.py"
OWNER_PROBE = ROOT.parents[1] / "scripts" / "ensure-broker.sh"
RUNTIME_LAUNCHER = ROOT.parents[1] / "scripts" / "launch-comet.sh"
DIAGNOSTICS = ROOT / "diagnostics.py"
MANIFEST = ROOT / "extension" / "manifest.json"
PLUGIN_JSON = Path(__file__).resolve().parents[3] / "plugin.json"


def assert_packaged_version(test: unittest.TestCase) -> None:
    version = json.loads(PLUGIN_JSON.read_text())["version"]
    test.assertEqual(json.loads(MANIFEST.read_text()).get("version"), version)
    test.assertEqual(version, json.loads(PLUGIN_JSON.read_text()).get("version"))


class SourceContractTests(unittest.TestCase):
    def test_page_context_keeps_sections_and_rejects_failed_reads(self) -> None:
        source = SERVICE_WORKER.read_text()
        branch = source.split('  if (type === "page_context") {', 1)[1].split('  if (type === "console_tail")', 1)[0]
        helper = source.split('function requireContentScriptResult(', 1)[1].split('function isLocatorMissError', 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
const codedError = (code, message) => Object.assign(new Error(message), {code});
function requireContentScriptResult(HELPER
const state = {tabId:7}, type = 'page_context';
const chrome = {tabs:{get:async () => ({status:'complete'})}};
const tabConsoleCdp = new Map(), tabNetworkCdp = new Map();
const mergeConsoleEntries = () => [], consoleErrorCount = () => 0;
const detectNativeOverlayHandoffHint = () => null;
let response;
const sendToContentScript = async (tab, method, args, frame) => {
  assert.equal(tab, 7); assert.equal(frame, 0);
  assert.equal(method, 'getPageContext'); assert.deepEqual(args, [['headings'], { compact: false }]);
  return response;
};
const run = async action => { BRANCH;
(async () => {
  response = {success:true,result:{url:'https://example.test/',title:'Fixture',headings:[]}};
  assert.equal((await run({sections:['headings']})).title, 'Fixture');
  for (const failure of [{success:false,error:'invalid sections'}, {success:true}, null]) {
    response = failure;
    await assert.rejects(() => run({sections:['headings']}));
  }
})().catch(e => {console.error(e); process.exit(1);});
'''.replace('HELPER', helper).replace('BRANCH', branch)
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        mutant = harness.replace('requireContentScriptResult(resp, "Page context returned no result")', 'resp?.result || {}')
        result = subprocess.run(['node', '-e', mutant], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, 'failed reads must not become empty success')

    def test_attached_viewport_capture_avoids_tabs_quota(self):
        source = SERVICE_WORKER.read_text()
        branch = source.split('  if (type === "screenshot") {', 1)[1].split('  if (type === "zoom") {', 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
async function check(attached, override, expectedSource) {
  let tabsCalls = 0, cdpCalls = 0;
  const state = {tabId: 7, deviceMetricsOverrideActive: override};
  const action = {format:'png'}, type = 'screenshot';
  const attachedTabs = new Set(attached ? [7] : []);
  const chrome = {
    tabs: {get: async () => ({windowId:3}), update: async () => {}, query: async () => [{id:7}]},
    debugger: {sendCommand: async (target, method) => {
      assert.equal(target.tabId,7); assert.equal(method,'Page.captureScreenshot');
      cdpCalls++; return {data:'fresh-image'};
    }}
  };
  const enqueueViewportCapture = fn => fn();
  const waitForViewportCaptureSlot = async () => {};
  const captureVisibleTabBounded = async () => {tabsCalls++; return 'data:image/png;base64,tabs-image';};
  const sendToContentScript = async () => {}, ensureAttached = async () => {};
  const withTimeout = async promise => promise, VIEWPORT_CAPTURE_MS = 8000;
  const run = async () => { BODY;
  const result = await run();
  assert.equal(result.source, expectedSource);
  assert.equal(tabsCalls, attached || override ? 0 : 1);
  assert.equal(cdpCalls, attached || override ? 1 : 0);
}
(async () => {
  await check(true, false, 'cdp.Page.captureScreenshot');
  await check(false, false, 'tabs.captureVisibleTab');
  await check(false, true, 'cdp.Page.captureScreenshot');
})().catch(e => {console.error(e); process.exit(1);});
'''.replace('BODY', branch)
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_context_exposes_visible_content_links_for_locator_planning(self) -> None:
        source = CURSOR_AGENT.read_text()
        start = source.index("  function getPageContext")
        end = source.index("  function flashLabel", start)
        function = source[start:end]
        if "function _accessibleName" in source:
            an_start = source.index("  function _accessibleName")
            an_end = source.index("  function findPointBySelector", an_start)
            function = source[an_start:an_end] + function
        harness = r"""
const assert = require('node:assert/strict');
const location = {href: 'https://example.test/'};
const contentLink = {
  innerText: 'Learn more', href: 'https://example.test/help',
  closest: () => null, getAttribute: () => null, tagName: 'A',
};
const navLink = {
  innerText: 'Home', href: 'https://example.test/home',
  closest: () => ({}), getAttribute: () => null, tagName: 'A',
};
const headerRoot = {
  querySelectorAll(selector) {
    if (selector.includes('a')) return [navLink];
    return [];
  },
};
const queries = [];
const document = {
  title: 'Fixture',
  querySelector(selector) {
    if (selector.includes('header') || selector.includes('nav')) return headerRoot;
    return null;
  },
  querySelectorAll(selector) {
    queries.push(selector);
    if (selector === 'a' || selector === 'a,[role="link"]') return [navLink, contentLink];
    if (selector === 'h1,h2,h3') return [{tagName: 'H1', innerText: 'Fixture'}];
    if (selector.includes('nav a') || selector.includes('role="navigation"')) return [navLink];
    if (selector.includes('button')) return [];
    if (selector.includes('input,textarea,select')) return [];
    return [];
  },
  getElementById() { return null; },
};
const _isVisible = () => true;
const pageRevision = 4;
FUNCTION
const page = getPageContext();
assert.equal(page.compact, true);
assert.deepEqual(page.links, [{text: 'Learn more', href: '/help'}]);
assert.ok(page.nav && page.nav[0].text === 'Home');
queries.length = 0;
const selective = getPageContext(['links']);
assert.deepEqual(selective, {
  url: location.href, title: 'Fixture', page_revision: 4,
  links: [{text: 'Learn more', href: 'https://example.test/help'}],
});
assert.deepEqual(queries, ['main,article,[role="main"]', 'a,[role="link"]'], 'unrequested sections must not scan the DOM');
queries.length = 0;
assert.deepEqual(getPageContext([]), {url: location.href, title: 'Fixture', page_revision: 4});
assert.deepEqual(queries, []);
const full = getPageContext(undefined, {compact: false});
assert.equal(full.compact, undefined);
assert.deepEqual(full.links, [{text: 'Learn more', href: 'https://example.test/help'}]);
for (const invalid of [null, 'links', ['typo'], Array(8).fill('links')]) {
  assert.throws(() => getPageContext(invalid), /sections must be an array/);
}
""".replace('FUNCTION', function)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        link_start = function.index("      ...(wants('links')")
        link_end = function.index("      ...(wants('buttons')", link_start)
        mutant = function[:link_start] + "      links: [],\n" + function[link_end:]
        result = subprocess.run(
            ["node", "-e", harness.replace(function, mutant)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, "link discovery regression escaped probe")

    def test_remount_navigation_skips_identity_and_mutation_is_caught(self):
        source = SERVICE_WORKER.read_text()
        start = source.index("    const navigationOnly =")
        end = source.index("    const results = [];", start)
        guard = source[start:end]
        harness = """
const assert = require('node:assert/strict');
async function check(actions, expected) {
  const message = {actions}, state = {tabId: 1}, leaseRecord = {};
  const dialogControlOnly = false, cdpObservationOnly = false;
  let calls = 0;
  const setAgentIdentity = async () => { calls++; };
  GUARD
  assert.equal(calls, expected);
}
(async () => {
 await check([{type:'reload_page'}], 0);
 await check([{type:'goto'}], 0);
 await check([{type:'reload_page'}, {type:'page_context'}], 1);
 await check([{type:'page_context'}], 1);
 await check([], 1);
})().catch(e => { console.error(e); process.exit(1); });
"""
        result = subprocess.run(["node", "-e", harness.replace("GUARD", guard)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        mutant = guard.replace("!navigationOnly && ", "")
        result = subprocess.run(["node", "-e", harness.replace("GUARD", mutant)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, "identity regression escaped probe")
        self.assertIn('throw codedError(ready.error_code || "CONTENT_SCRIPT_MISSING"', source)
        self.assertIn('error_code: "CS_DEAD_AFTER_REMOUNT"', source)
        self.assertIn('throw codedError(status.error_code || "CONTENT_SCRIPT_MISSING"', source)
        self.assertIn('"CS_DEAD_AFTER_REMOUNT",', source)

    def test_unpackaged_extension_id_is_stable_across_install_paths(self) -> None:
        manifest = json.loads(MANIFEST.read_text())
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).digest()[:16]
        extension_id = "".join(
            chr(ord("a") + nibble)
            for byte in digest
            for nibble in (byte >> 4, byte & 15)
        )
        self.assertIn(extension_id, OWNER_PROBE.read_text())

    def test_client_caches_route_to_the_shared_runtime(self) -> None:
        shared = ".agents/plugins/comet-control"
        self.assertIn(shared, OWNER_PROBE.read_text())
        self.assertIn(shared, LEASE_DRIVER.read_text())
        self.assertIn(shared, (LEASE_DRIVER.parent / "durable_lease_controller.py").read_text())
        cua_slice = (LEASE_DRIVER.parent / "cua_slice.py").read_text()
        self.assertIn(shared, cua_slice)
        self.assertIn(".agents/plugins/agent-computer-use", cua_slice)
        self.assertNotIn(".agents/skills/macos-cua", cua_slice)

    def test_actionability_revisions_pause_and_failure_trace_are_owned_in_place(self) -> None:
        worker = SERVICE_WORKER.read_text()
        cursor = CURSOR_AGENT.read_text()
        broker = BROKER.read_text()

        for contract in (
            "ACTIONABILITY_TARGET_COUNT",
            "ACTIONABILITY_NOT_VISIBLE",
            "ACTIONABILITY_DISABLED",
            "ACTIONABILITY_NOT_EDITABLE",
            "ACTIONABILITY_UNSTABLE",
            "ACTIONABILITY_OBSCURED",
        ):
            self.assertIn(contract, cursor)
        self.assertIn("document.elementFromPoint(x, y)", cursor)
        self.assertIn("requestAnimationFrame(resolve)", cursor)
        self.assertIn("page_revision", cursor)
        self.assertIn("semanticTargetCache.clear()", cursor)
        cache = cursor.split("function _semanticCacheKey", 1)[1].split(
            "async function _pointForElement", 1
        )[0]
        self.assertNotIn("chrome.storage", cache)
        self.assertNotIn("targetToken", cache)
        self.assertNotIn(".x", cache)
        self.assertNotIn(".y", cache)
        self.assertIn("invalidateConnectionState", worker)
        self.assertIn('message?.type === "broker_hello_ack"', worker)
        self.assertIn('"type": "broker_hello_ack"', broker)
        reconnect = worker.split("nextPort.onopen = () =>", 1)[1].split(
            "nextPort.onmessage", 1
        )[0]
        self.assertLess(
            reconnect.index("restoreLeasedConnectionState()"),
            reconnect.index("sendBrokerHello(nextPort)"),
        )
        disconnect_at = worker.index("nextPort.onclose = () =>")
        invalidate_at = worker.index("invalidateLeasedConnectionState()", disconnect_at)
        reconnect_at = worker.index(".finally(() => scheduleHostReconnect(2000))", disconnect_at)
        self.assertLess(invalidate_at, reconnect_at)
        self.assertIn("CONTROL_PAUSED", worker)
        self.assertIn("failure_record", worker)
        self.assertIn("MAX_EXTENSION_RESPONSE_BYTES = 16 * 1024 * 1024", broker)
        self.assertIn("MAX_PENDING_REQUESTS = 64", broker)
        self.assertNotIn("200 * 1024 * 1024", broker)

    def test_click_identity_survives_unrelated_dom_churn_and_same_url_goto_reloads(self) -> None:
        cursor = CURSOR_AGENT.read_text()
        worker = SERVICE_WORKER.read_text()
        expected_target = cursor.split("function _expectedTargetAtCursor", 1)[1].split(
            "function click", 1
        )[0]
        self.assertNotIn("expectation.pageRevision", expected_target)
        self.assertIn("targetElementsByToken.get", expected_target)
        self.assertIn("_relatedTargetAtCursor([target])", expected_target)
        self.assertIn("if (!expectation.targetHref)", expected_target)
        self.assertNotIn("target?.isConnected || !expectation.targetHref", expected_target)
        self.assertIn("el.href === expectation.targetHref", expected_target)

        page_revision = cursor.split("function _bumpPageRevision", 1)[1].split(
            "new MutationObserver", 1
        )[0]
        self.assertNotIn("targetElementsByToken.clear()", page_revision)
        self.assertIn("if (!target.isConnected) targetElementsByToken.delete(token)", page_revision)

        resolved_click = worker.split("async function clickResolvedTarget", 1)[1].split(
            "function leaseForTab", 1
        )[0]
        self.assertIn('targetHref: point.href || ""', resolved_click)

        goto = worker.split('if (type === "goto")', 1)[1].split(
            'if (type === "back"', 1
        )[0]
        self.assertIn("if (current.url === action.url)", goto)
        self.assertIn("chrome.tabs.reload(state.tabId", goto)
        self.assertIn("bypassCache: action.bypassCache !== false", goto)

    def test_manifest_omits_unused_ambient_tab_permissions(self) -> None:
        permissions = json.loads(MANIFEST.read_text())["permissions"]
        self.assertNotIn("activeTab", permissions)
        self.assertNotIn("tabGroups", permissions)

    def test_canonical_skill_is_in_the_plugin_package(self) -> None:
        self.assertTrue(CANONICAL_SKILL.is_file())
        self.assertEqual(CANONICAL_SKILL.parent.name, "comet-control")
        self.assertEqual(CANONICAL_SKILL.parents[2].name, "comet-control")

    def test_session_restore_failure_disables_all_leased_browser_work(self) -> None:
        source = SERVICE_WORKER.read_text()
        restore = source.split("async function restoreSessionLeases() {", 1)[1].split(
            "let sessionStateError", 1
        )[0]
        guard = source.split("async function requireSessionStateReady() {", 1)[1].split(
            "function enqueueSession", 1
        )[0]

        # A storage read failure is lease-state ambiguity, not an empty registry.
        self.assertNotIn(".catch(() => ({}))", restore)
        self.assertIn("sessionStateError = error;", source)
        self.assertIn("await sessionStateReady;", guard)
        self.assertIn('error.code = "LEASE_STATE_UNAVAILABLE";', guard)
        # No caller may await the swallowed bootstrap promise directly.
        self.assertEqual(source.count("await sessionStateReady;"), 1)

    def test_restore_retains_window_only_provisional_acquisition(self) -> None:
        source = SERVICE_WORKER.read_text()
        restore = source.split("async function restoreSessionLeases() {", 1)[1].split(
            "let sessionStateError", 1
        )[0]
        self.assertIn("const ownsWindow = raw.ownsWindow !== false;", restore)
        self.assertIn("const ownsTab = raw.ownsTab !== false;", restore)
        self.assertIn("const validOwnedWindow = !ownsWindow || Number.isInteger(raw.windowId);", restore)
        self.assertIn("const validOwnedTab = !ownsTab || Number.isInteger(raw.tabId);", restore)
        self.assertNotIn("!raw.tabId || !raw.windowId", restore)
        self.assertIn("sessionLeases.set(sessionId, record);", restore)

        preflight = source.split("async function sessionPreflight(message) {", 1)[1].split(
            "function requireSessionLease", 1
        )[0]
        close = source.split("async function closeSession(", 1)[1].split(
            "async function reapExpiredSessions", 1
        )[0]
        reaper = source.split("async function reapExpiredSessions(", 1)[1].split(
            "function isControllableUrl", 1
        )[0]
        self.assertIn("await withTimeout(requireSessionStateReady()", preflight)
        self.assertIn("await requireSessionStateReady();", close)
        self.assertIn("await requireSessionStateReady();", reaper)

        host_messages = source.split('if (message?.type === "session_preflight") {', 1)[1]
        session_closeout = host_messages.split('if (message?.type === "session_closeout") {', 1)[1].split(
            'if (message?.type === "user_tabs") {', 1
        )[0]
        run = host_messages.split('if (message?.type !== "run") {', 1)[1]
        self.assertIn("await requireSessionStateReady();", session_closeout)
        self.assertIn("await requireSessionStateReady();", run)

    def test_expiry_reaper_uses_target_fifo_and_rechecks_at_execution_time(self) -> None:
        source = SERVICE_WORKER.read_text()
        preflight = source.split("async function sessionPreflight(message) {", 1)[1].split(
            "function requireSessionLease", 1
        )[0]
        reaper = source.split("async function reapExpiredSessions(", 1)[1].split(
            "function isControllableUrl", 1
        )[0]

        # The enclosing native message already owns this session queue, so its
        # identity must be validated first and passed to the reaper for inline
        # handling. Re-enqueueing the same key would deadlock preflight.
        self.assertIn("const sessionId = requireSessionId(message.sessionId);", preflight)
        self.assertIn("reapExpiredSessions({ ownedSessionId: sessionId })", preflight)
        self.assertLess(
            preflight.index("const sessionId = requireSessionId(message.sessionId);"),
            preflight.index("reapExpiredSessions({ ownedSessionId: sessionId })"),
        )
        self.assertIn("sessionId === ownedSessionId", reaper)
        self.assertIn("? await reapIfStillExpired(sessionId)", reaper)

        # Other leases enter their own FIFO. The candidate is looked up again
        # there, after any queued renewal, before destructive cleanup begins.
        self.assertIn(
            "await enqueueSession(sessionId, () => reapIfStillExpired(sessionId))",
            reaper,
        )
        # Two concurrent preflights must not own A and B while each awaits the
        # other's FIFO. An already-active foreign FIFO owns its own lifecycle.
        self.assertIn(
            "sessionId !== ownedSessionId && sessionQueues.has(sessionId)",
            reaper,
        )
        self.assertLess(
            reaper.index("sessionQueues.has(sessionId)"),
            reaper.index("await enqueueSession(sessionId"),
        )
        self.assertIn("const record = sessionLeases.get(sessionId);", reaper)
        recheck = "Date.now() - record.lastSeen <= record.ttlMs"
        self.assertIn(recheck, reaper)
        self.assertLess(reaper.index(recheck), reaper.index('closeSession(sessionId, { reason: "lease-expired" })'))

    def test_private_lease_tokens_are_process_owned_and_redacted(self) -> None:
        driver_source = LEASE_DRIVER.read_text()
        self.assertIn("def redact_private(value: Any, private_token: str = \"\")", driver_source)
        self.assertIn('replace("_", "").lower() == "leasetoken"', driver_source)
        self.assertIn('for name in ("SIGTERM", "SIGHUP")', driver_source)
        self.assertIn('closeout("driver-exit")', driver_source)

    def test_dashboard_regression_uses_a_lease_not_the_active_tab_or_legacy_home(self) -> None:
        source = DASHBOARD_TEST.read_text()
        lease_harness = ISOLATION_TEST.read_text()

        self.assertNotIn("useSelectedTab", source)
        self.assertNotIn("~/.comet-control", source)
        self.assertIn("preflight,", source)
        self.assertIn("close_session,", source)
        self.assertIn("finally:", source)
        self.assertIn('"leaseToken"', source)
        self.assertIn('"type": "session_preflight"', lease_harness)
        self.assertIn('"type": "session_closeout"', lease_harness)

    def test_logged_in_comet_uses_owned_loopback_broker(self) -> None:
        worker = SERVICE_WORKER.read_text()
        broker = BROKER.read_text()
        probe = OWNER_PROBE.read_text()
        launcher = RUNTIME_LAUNCHER.read_text()
        diagnostics = DIAGNOSTICS.read_text()
        permissions = json.loads(MANIFEST.read_text())["permissions"]

        self.assertNotIn("nativeMessaging", permissions)
        self.assertIn('const BROKER_URL = "ws://127.0.0.1:38927";', worker)
        self.assertNotIn("connectNative", worker)
        self.assertIn("def extension_connection", broker)
        self.assertIn('BROKER_HOST = "127.0.0.1"', broker)
        self.assertIn("hmac.compare_digest(actual_origin, expected_origin)", broker)
        self.assertIn('message?.type === "broker_ping"', worker)
        self.assertIn('"type": "broker_ping"', broker)
        self.assertIn("_attest_comet_runtime()", broker)
        self.assertIn('request.get("type") == "broker_status"', broker)
        self.assertIn("COMET_CONTROL_EXPECTED_USER_DATA_DIR", probe)
        self.assertNotIn("NativeMessagingHosts", probe)
        self.assertNotIn("com.perplexity", probe)
        self.assertIn('open -a "Comet"', launcher)
        self.assertNotIn('--user-data-dir=', launcher)
        self.assertNotIn("--load-extension", launcher)
        self.assertIn('[str(PROBE), "probe", "--json"]', diagnostics)
        self.assertNotIn("socket.socket", diagnostics)
        self.assertNotIn("NativeMessagingHosts", diagnostics)

    def test_viewport_screenshot_activates_and_verifies_the_leased_tab(self) -> None:
        source = SERVICE_WORKER.read_text()
        branch = source.split('if (type === "screenshot") {', 1)[1].split(
            'if (type === "download_wait") {', 1
        )[0]
        activate = 'await chrome.tabs.update(state.tabId, { active: true });'
        verify = 'Number(activeTab.id) !== Number(state.tabId)'
        capture = 'captureVisibleTabBounded(leasedTab.windowId, options)'
        fallback = 'await sendToContentScript(state.tabId, "captureScreenshot")'

        self.assertIn('return enqueueViewportCapture(async () => {', branch)
        self.assertIn('state.windowId = leasedTab.windowId;', branch)
        self.assertIn(activate, branch)
        self.assertIn(verify, branch)
        self.assertIn(capture, branch)
        self.assertIn(fallback, branch)
        self.assertIn('const useAttachedCapture = attachedTabs.has(state.tabId);', branch)
        self.assertLess(branch.index('const useAttachedCapture ='), branch.index(activate))
        self.assertIn('"Page.captureScreenshot"', branch)
        self.assertIn('/image readback failed/i', branch)
        self.assertIn('for (let attempt = 0; attempt < 2 && !state.deviceMetricsOverrideActive && !useAttachedCapture; attempt += 1)', branch)
        self.assertLess(branch.index(activate), branch.index(capture))
        self.assertLess(branch.index(verify), branch.index(capture))
        self.assertLess(branch.index(capture), branch.index(fallback))

    def test_viewport_override_skips_stale_captureVisibleTab(self) -> None:
        """After viewport_set, screenshot must not reuse a silent stale captureVisibleTab frame."""
        parity = PARITY_CAPABILITIES.read_text()
        source = SERVICE_WORKER.read_text()
        viewport_set = parity.split('if (type === "viewport_set") {', 1)[1].split(
            'if (type === "viewport_reset") {', 1
        )[0]
        viewport_reset = parity.split('if (type === "viewport_reset") {', 1)[1].split(
            "return { handled: false, result: null };", 1
        )[0]
        self.assertIn("state.deviceMetricsOverrideActive = true;", viewport_set)
        self.assertIn("state.deviceMetricsOverrideActive = false;", viewport_reset)
        screenshot = source.split('if (type === "screenshot") {', 1)[1].split(
            'if (type === "download_wait") {', 1
        )[0]
        self.assertIn("state.deviceMetricsOverrideActive", screenshot)
        self.assertIn("staleCaptureSkipped", screenshot)
        self.assertIn("stale_capture_skipped", screenshot)
        self.assertLess(
            screenshot.index("if (state.deviceMetricsOverrideActive)"),
            screenshot.index("captureVisibleTabBounded(leasedTab.windowId, options)"),
        )
        self.assertIn("for (let attempt = 0; attempt < 2 && !state.deviceMetricsOverrideActive && !useAttachedCapture; attempt += 1)", screenshot)
        self.assertIn('"Page.captureScreenshot"', screenshot)
        self.assertIn("cdp.Page.captureScreenshot", screenshot)
        self.assertIn("lastVisibleTabCaptureFingerprint", screenshot)
        self.assertIn("slice(-64)", screenshot)
        fingerprint = screenshot.split("lastVisibleTabCaptureFingerprint", 1)[1]
        self.assertIn("slice(-64)", fingerprint)
        self.assertIn("requestAnimationFrame(resolve)", fingerprint)
        self.assertIn("staleCaptureSkipped = true", fingerprint)
        self.assertIn("dataUrl = null", fingerprint)
        # Same-fingerprint stale skip falls through to the CDP capture.
        self.assertLess(
            screenshot.index("lastVisibleTabCaptureFingerprint"),
            screenshot.index('"cdp.Page.captureScreenshot"'),
        )
        self.assertLess(
            screenshot.index("slice(-64)"),
            screenshot.index("stale_capture_skipped"),
        )

    def test_browser_work_never_requests_os_focus(self) -> None:
        source = SERVICE_WORKER.read_text()
        self.assertNotIn('focused: true', source)
        self.assertNotIn('drawAttention: true', source)
        self.assertIn(
            'chrome.windows.create({ url: startUrl, focused: false, type: "normal" })',
            source,
        )

    def test_viewport_capture_queue_is_global_and_failure_tolerant(self) -> None:
        source = SERVICE_WORKER.read_text()
        self.assertIn('let viewportCaptureQueue = Promise.resolve();', source)
        self.assertIn('let lastViewportCaptureStartedAt = 0;', source)
        queue = source.split('function enqueueViewportCapture(operation) {', 1)[1].split(
            'async function waitForTabReady', 1
        )[0]
        self.assertIn('viewportCaptureQueue.catch(() => {}).then(operation)', queue)
        self.assertIn('viewportCaptureQueue = next;', queue)
        self.assertIn('async function waitForViewportCaptureSlot()', queue)
        self.assertIn('lastViewportCaptureStartedAt + minGapMs - Date.now()', queue)
        self.assertIn('lastViewportCaptureStartedAt = Date.now();', queue)

        screenshot = source.split('if (type === "screenshot") {', 1)[1].split(
            'if (type === "download_wait") {', 1
        )[0]
        self.assertLess(
            screenshot.index('await waitForViewportCaptureSlot();'),
            screenshot.index('captureVisibleTabBounded(leasedTab.windowId, options)'),
        )

    def test_cursor_key_uses_native_cdp_keyboard_and_keeps_visible_intent(self) -> None:
        source = SERVICE_WORKER.read_text()
        branch = source.split('if (type === "cursor_key") {', 1)[1].split(
            'if (type === "cursor_drag") {', 1
        )[0]
        cursor_source = CURSOR_AGENT.read_text()
        parity_source = PARITY_CAPABILITIES.read_text()

        self.assertIn('await sendToContentScript(state.tabId, "showKey", [key]);', branch)
        self.assertIn('await pressKey(send, state.tabId, key, modifiers || []);', branch)
        self.assertNotIn('"keyPress"', branch)
        self.assertIn('function showKey(key)', cursor_source)
        self.assertIn('export async function pressKey(', parity_source)
        self.assertIn('"Input.dispatchKeyEvent"', parity_source)

    def test_custom_combobox_select_option_fails_closed(self) -> None:
        source = PARITY_CAPABILITIES.read_text()
        branch = source.split('} else if (operation === "select_option") {', 1)[1].split(
            '} else {', 1
        )[0]

        self.assertIn('String(match.tag || "").toLowerCase() !== "select"', branch)
        self.assertIn('requires a native select element', branch)
        self.assertIn('selectedOptions.length === 0', branch)

    def test_read_only_evaluation_retries_only_transient_cdp_internal_error(self) -> None:
        source = PARITY_CAPABILITIES.read_text()
        branch = source.split('export async function evaluateReadOnly', 1)[1].split(
            'async function cdpAction', 1
        )[0]

        self.assertEqual(branch.count('result = await evaluate();'), 2)
        self.assertIn('parsed?.code === -32603', branch)
        self.assertIn('parsed?.message === "Internal error"', branch)
        self.assertIn('if (!transient) throw error', branch)
        self.assertIn('throwOnSideEffect: true', branch)

    def test_foreign_extension_url_is_not_reported_as_debugger_ownership(self) -> None:
        source = SERVICE_WORKER.read_text()
        ensure_attached = source.split('async function ensureAttached(tabId) {', 1)[1].split(
            'async function send(tabId, method', 1
        )[0]
        attach_failure = ensure_attached.split('} catch (e) {', 1)[1].split(
            '  try {', 1
        )[0]

        self.assertIn('foreign-extension URL/frame restriction: ${msg}', attach_failure)
        self.assertNotIn('controlled by another extension', attach_failure)
        self.assertEqual(
            ensure_attached.count('foreign-extension URL/frame restriction: ${msg}'),
            2,
        )
        self.assertIn('if (!msg.includes("already attached"))', ensure_attached)
        self.assertIn('if (msg.includes("not attached"))', ensure_attached)
        self.assertIn('controlled by another extension: ${msg}', ensure_attached)

    def test_extension_build_attestation_covers_imported_control_runtime(self) -> None:
        source = SERVICE_WORKER.read_text()
        build = source.split('const extensionBuildSha256', 1)[1].split(
            'async function pairingSecret', 1
        )[0]

        self.assertIn('"service_worker.js"', build)
        self.assertIn('"parity_capabilities.js"', build)
        self.assertIn('"content-scripts/cursor-agent.js"', build)

    def test_lease_driver_disables_canonical_tty_line_buffering(self) -> None:
        source = LEASE_DRIVER.read_text()

        self.assertIn('termios.ICANON | termios.ECHO', source)
        self.assertIn('current[6][termios.VMIN] = 1', source)
        self.assertIn('restore_tty_input(tty_state)', source)

    def test_cua_claim_is_atomic_authenticated_and_token_redacted(self) -> None:
        source = SERVICE_WORKER.read_text()
        acquire = source.split('async function acquireCuaRuntimeClaim(message) {', 1)[1].split(
            'async function releaseCuaRuntimeClaim', 1
        )[0]
        dispatch = source.split('async function handleHostMessage(message) {', 1)[1].split(
            'async function handleUnlockedHostMessage', 1
        )[0]
        sessions = source.split('if (message?.type === "sessions") {', 1)[1].split(
            'if (message?.type === "session_preflight") {', 1
        )[0]

        self.assertIn('const owningLease = intent === "native-dialog" ? requireSessionLease(message) : null;', acquire)
        self.assertNotIn('reapExpiredSessions()', acquire)
        self.assertIn('if (activeHostMutations > 0)', acquire)
        self.assertIn('if (claim && !readOnly)', dispatch)
        self.assertLess(dispatch.index('if (claim && !readOnly)'), dispatch.index('activeHostMutations += 1'))
        self.assertIn('const { lease_token: _privateToken, ...safe } = publicLease(record);', sessions)
        self.assertIn('cua_claim: publicCuaClaim(activeCuaClaim())', sessions)
        self.assertNotIn('claimToken', source.split('function publicCuaClaim(claim) {', 1)[1].split('async function persistCuaClaim', 1)[0])

    def test_session_renew_is_authenticated_nonvisual_and_cua_safe(self) -> None:
        source = SERVICE_WORKER.read_text()
        broker = BROKER.read_text()
        renewal = source.split('async function renewSessionLease(message) {', 1)[1].split(
            'async function closeSession(', 1
        )[0]
        lease_auth = source.split('function requireSessionLease(message) {', 1)[1].split(
            'async function renewSessionLease', 1
        )[0]
        persistence = source.split('function persistSessionLeases() {', 1)[1].split(
            'function publicCuaClaim', 1
        )[0]
        read_only = source.split('function hostMessageIsReadOnly(message) {', 1)[1].split(
            'function codedError', 1
        )[0]
        dispatch = source.split('if (message?.type === "session_renew") {', 1)[1].split(
            'if (message?.type === "session_closeout") {', 1
        )[0]
        visual_types = broker.split('VISUAL_REQUEST_TYPES = ', 1)[1].split('\n', 1)[0]

        # The opaque capability is mandatory and expiry changes are persisted.
        self.assertIn('await requireSessionStateReady();', renewal)
        self.assertIn('const record = requireSessionLease(message);', renewal)
        self.assertIn('!message.leaseToken || message.leaseToken !== record.leaseToken', lease_auth)
        self.assertIn('message.ttlSeconds', renewal)
        self.assertIn('record.lastSeen = renewedAt;', renewal)
        self.assertIn('record.ttlMs = ttlMs;', renewal)
        self.assertIn('await persistSessionLeases();', renewal)
        self.assertIn('leasePersistenceQueue.catch(() => {}).then(async () => {', persistence)
        self.assertIn('leasePersistenceQueue = next;', persistence)
        self.assertLess(
            persistence.index('leasePersistenceQueue.catch'),
            persistence.index('Object.fromEntries(Array.from(sessionLeases.entries())'),
        )

        # A fully absent target is finalized, while a half-missing or moved
        # target keeps its authenticated ownership for a later closeout retry.
        self.assertIn('const targets = await readOwnedLeaseTargets(record);', renewal)
        self.assertIn('if (ownedLeaseTargetsAbsent(targets)) {', renewal)
        self.assertIn('finalizeAbsentLease(record, "renew-target-absent"', renewal)
        self.assertIn('"LEASE_TARGET_MISSING"', renewal)
        self.assertIn('if (!ownedLeaseTargetsComplete(record, targets)) {', renewal)
        self.assertIn('await retainLeaseForCleanupRetry(record);', renewal)
        self.assertIn('"LEASE_TARGET_PARTIAL"', renewal)
        self.assertIn('{ retryable: true }', renewal)
        self.assertNotIn('sessionLeases.delete(', renewal)

        # Heartbeats never mutate visible browser surfaces and never expose the
        # private lease capability in their compact response.
        for forbidden in (
            'chrome.windows.create',
            'chrome.windows.update',
            'chrome.windows.remove',
            'chrome.tabs.create',
            'chrome.tabs.update',
            'chrome.tabs.remove',
            'applyAgentWindowLayout',
            'flushDeferredAgentWindowLayout',
            'setAgentIdentity',
            'sendToContentScript',
            'chrome.debugger',
            'sessionPreflight',
            'closeSession',
            'lease_token',
        ):
            self.assertNotIn(forbidden, renewal)
        for stable_identity in ('session_id:', 'window_id:', 'tab_id:', 'expires_at:', 'ttl_seconds:'):
            self.assertIn(stable_identity, renewal)

        # Renewal bypasses the visual-focus exclusion while a disjoint macOS
        # CUA claim is active; the broker likewise must not take that lock.
        self.assertIn('"session_renew"', read_only)
        self.assertIn('await renewSessionLease(message);', dispatch)
        self.assertNotIn('"session_renew"', visual_types)

    def test_terminal_closeout_is_exact_bounded_verified_and_retryable(self) -> None:
        source = SERVICE_WORKER.read_text()
        targets = source.split('function chromeTargetLookupProvesAbsent(error, kind) {', 1)[1].split(
            'async function renewSessionLease', 1
        )[0]
        finalizer = source.split('async function finalizeAbsentLease(', 1)[1].split(
            'async function renewSessionLease', 1
        )[0]
        closeout = source.split('async function closeSession(', 1)[1].split(
            'async function reapExpiredSessions', 1
        )[0]
        dispatch = source.split('if (message?.type === "session_closeout") {', 1)[1].split(
            'if (message?.type === "user_tabs") {', 1
        )[0]

        # Ownership is verified by exact IDs. A tab moved out of its original
        # window remains owned and therefore makes the target incomplete.
        self.assertIn('await chrome.tabs.get(id)', targets)
        self.assertIn('await chrome.windows.get(id)', targets)
        self.assertIn('chromeTargetLookupProvesAbsent(error, kind)', targets)
        self.assertIn('"LEASE_TARGET_READ_FAILED"', targets)
        self.assertIn('{ retryable: true }', targets)
        self.assertNotIn('.catch(() => null)', targets)
        self.assertIn('tab.windowId !== record.windowId', targets)
        self.assertIn('function ownedLeaseTargetsAbsent(targets)', targets)
        self.assertIn('function ownedLeaseTargetsComplete(record, targets)', targets)

        # Closeout makes two bounded exact-ID attempts. It re-reads the tab
        # after window removal so a moved tab cannot be abandoned.
        self.assertIn('const initialTargets = await readOwnedLeaseTargetsForCleanup(record);', closeout)
        self.assertIn('attempt <= 2', closeout)
        self.assertIn('await chrome.windows.remove(record.windowId);', closeout)
        self.assertIn('const afterWindowAttempt = await readOwnedLeaseTargetsForCleanup(record);', closeout)
        self.assertIn('await chrome.tabs.remove(record.tabId);', closeout)
        self.assertLess(
            closeout.index('await chrome.windows.remove(record.windowId);'),
            closeout.index('const afterWindowAttempt = await readOwnedLeaseTargetsForCleanup(record);'),
        )
        self.assertLess(
            closeout.index('const afterWindowAttempt = await readOwnedLeaseTargetsForCleanup(record);'),
            closeout.index('await chrome.tabs.remove(record.tabId);'),
        )
        self.assertIn('removalErrors.push(`window:', closeout)
        self.assertIn('removalErrors.push(`tab:', closeout)

        # Remove rejection or a surviving ID retains the lease, clears closing,
        # persists it, and exposes a machine-readable retryable error.
        incomplete = closeout.split('if (!ownedLeaseTargetsAbsent(verified)) {', 1)[1].split(
            'tabsClosed = initialTargets.tabPresent', 1
        )[0]
        self.assertIn('await retainLeaseForCleanupRetry(record);', incomplete)
        self.assertIn('"LEASE_CLEANUP_INCOMPLETE"', incomplete)
        self.assertIn('{ retryable: true }', incomplete)
        self.assertIn('record.closing = false;', targets)
        self.assertIn('await persistSessionLeases();', targets)
        self.assertLess(
            closeout.index('if (!ownedLeaseTargetsAbsent(verified)) {'),
            closeout.index('await finalizeAbsentLease(record, reason'),
        )

        # Counts describe verified presence-to-absence transitions. Window
        # removal closing its tab still reports one closed tab.
        self.assertIn(
            'tabsClosed = initialTargets.tabPresent && !verified.tabPresent ? 1 : 0;',
            closeout,
        )
        self.assertIn(
            'windowsClosed = initialTargets.windowPresent && !verified.windowPresent ? 1 : 0;',
            closeout,
        )

        # The deletion/tombstone boundary performs one final exact re-read and
        # cannot delete until both owned IDs are proven absent.
        self.assertIn('const verified = await readOwnedLeaseTargetsForCleanup(record);', finalizer)
        guard = finalizer.index('if (!ownedLeaseTargetsAbsent(verified)) {')
        deletion = finalizer.index('sessionLeases.delete(record.sessionId);')
        tombstone = finalizer.index('await recordLeaseRemoval(record, reason')
        self.assertLess(guard, deletion)
        self.assertLess(deletion, tombstone)
        self.assertIn('verified_absent: true', finalizer)
        self.assertIn('catch (error)', finalizer)
        self.assertIn('layout deferred after verified closeout', finalizer)

        # Keeping authenticated content after releasing ownership is forbidden.
        self.assertNotIn('keepWindow', closeout)
        self.assertIn('if (message.keepWindow)', dispatch)
        self.assertIn('"KEEP_WINDOW_UNSUPPORTED"', dispatch)
        self.assertNotIn('keepWindow:', dispatch)

        # Retryability is included in broker error responses.
        self.assertIn('...(error?.retryable ? { retryable: true } : {})', source)
        self.assertIn('function codedError(code, message, { retryable = false, details = null } = {})', source)

    def test_partial_targets_retain_ownership_across_restore_renew_and_events(self) -> None:
        source = SERVICE_WORKER.read_text()
        restore = source.split('async function restoreSessionLeases() {', 1)[1].split(
            'let sessionStateError', 1
        )[0]
        preflight = source.split('const existing = sessionLeases.get(sessionId);', 1)[1].split(
            'const startUrl =', 1
        )[0]
        reconcile = source.split('async function reconcileExternallyRemovedLeaseTarget(', 1)[1].split(
            '// Clean up per-tab storage', 1
        )[0]
        layout = source.split('async function tileOwnedAgentWindows(', 1)[1].split(
            'function enqueueAgentWindowLayout', 1
        )[0]
        removed = source.split('chrome.tabs.onRemoved.addListener', 1)[1].split(
            '// ---- Content Script Messaging ----', 1
        )[0]

        # Restore tombstones only when both exact surfaces are absent; partial
        # and moved records return to the authenticated registry.
        self.assertIn('const targets = await readOwnedLeaseTargets(record);', restore)
        self.assertIn('if (ownedLeaseTargetsAbsent(targets)) {', restore)
        self.assertIn('recordLeaseRemoval(record, "restore-target-absent"', restore)
        self.assertIn('sessionLeases.set(sessionId, record);', restore)
        self.assertLess(
            restore.index('if (ownedLeaseTargetsAbsent(targets)) {'),
            restore.index('sessionLeases.set(sessionId, record);'),
        )

        # Reuse cannot delete a partial lease and create a replacement window;
        # visual preflight must pass through verified centralized cleanup first.
        self.assertIn('ownedLeaseTargetsComplete(existing, targets)', preflight)
        self.assertIn('closeSession(sessionId, { reason: "preflight-target-partial" })', preflight)
        self.assertNotIn('sessionLeases.delete(', preflight)

        # Autonomous tab/window removal is nonvisual and serialized. It only
        # finalizes when both exact surfaces are absent; otherwise it persists
        # the surviving ownership for authenticated closeout.
        self.assertIn('if (ownedLeaseTargetsAbsent(targets)) {', reconcile)
        self.assertIn('await finalizeAbsentLease(record, reason, details, { visual: false });', reconcile)
        self.assertIn('await retainLeaseForCleanupRetry(record);', reconcile)
        self.assertIn('enqueueSession(sessionId, () => reconcileExternallyRemovedLeaseTarget(', removed)
        self.assertIn('chrome.windows.onRemoved.addListener((windowId)', removed)
        self.assertNotIn('sessionLeases.delete(', removed)
        for forbidden in (
            'chrome.windows.update',
            'chrome.windows.remove',
            'chrome.tabs.update',
            'chrome.tabs.remove',
            'applyAgentWindowLayout',
        ):
            self.assertNotIn(forbidden, reconcile)

        # A retained partial peer cannot break or visually move complete
        # sessions when deferred geometry is eventually flushed.
        self.assertIn('targets: await readOwnedLeaseTargets(record)', layout)
        self.assertIn('ownedLeaseTargetsComplete(record, targets)', layout)
        self.assertNotIn('sessionLeases.delete(', layout)

    def test_run_cleans_partial_target_before_deferred_layout(self) -> None:
        source = SERVICE_WORKER.read_text()
        run = source.split('if (message?.type !== "run") {', 1)[1]
        auth = run.index('const leaseRecord = requireSessionLease(message);')
        target_read = run.index('const runTargets = await readOwnedLeaseTargets(leaseRecord);')
        cleanup = run.index('await closeSession(leaseRecord.sessionId, { reason: "run-target-partial" });')
        layout = run.index('await flushDeferredAgentWindowLayout();')

        self.assertLess(auth, target_read)
        self.assertLess(target_read, cleanup)
        self.assertLess(cleanup, layout)
        partial = run[target_read:layout]
        self.assertIn('if (!ownedLeaseTargetsComplete(leaseRecord, runTargets)) {', partial)
        self.assertIn('"LEASE_TARGET_MISSING"', partial)
        self.assertNotIn('sessionLeases.delete(', partial)

    def test_preflight_reclaims_orphan_without_leaking_lease_token(self) -> None:
        source = SERVICE_WORKER.read_text()
        preflight = source.split("async function sessionPreflight(message) {", 1)[1].split(
            "function requireSessionLease", 1
        )[0]
        held = preflight.split('codedError(', 1)[1].split(");", 1)[0]
        self.assertIn('"LEASE_HELD"', held)
        self.assertIn("already leased by another caller", held)
        self.assertIn("{ retryable: true }", held)
        self.assertIn('reason: "preflight-orphan-reclaim"', preflight)
        self.assertIn("ownedLeaseTargetsAbsent(targets)", preflight)
        self.assertIn("renewStale", preflight)
        self.assertIn("idleStale", preflight)
        self.assertIn("stuckBusyStale", preflight)
        # Idle live owners renew lastSeen; busy=false alone must not reclaim
        # or disclose the private capability on the foreign-token path.
        # Stuck busy after EXTENSION_TIMEOUT may reclaim only after a longer
        # silence (max(ownerGoneMs, 180s)).
        foreign = preflight.split("tokenOk", 1)[1]
        self.assertIn("idleStale", foreign)
        self.assertIn("stuckBusyStale", foreign)
        self.assertIn("!existing.busy", foreign)
        self.assertIn("180_000", foreign)
        self.assertIn("!existing.closing", foreign)
        self.assertNotIn("leaseToken = existing.leaseToken", foreign)
        self.assertNotIn(".leaseToken = existing", foreign)
        held_call = foreign.split('codedError(\n          "LEASE_HELD"', 1)[1].split(");", 1)[0]
        self.assertNotIn("leaseToken", held_call)
        self.assertIn("retryable: true", held_call)

        driver = LEASE_DRIVER.read_text()
        self.assertIn("preflight_retry_waiting", driver)
        self.assertIn("retrying_same_session_id", driver)
        self.assertIn('error_code") == "LEASE_HELD"', driver)

    def test_content_script_force_reinject_after_half_dead_probe(self) -> None:
        """Seller Accept→Dispatch: getStatus can succeed while click hangs.

        Retries must force reinject instead of trusting probe forever.
        """
        source = SERVICE_WORKER.read_text()
        self.assertIn("contentScriptForceReinjection", source)
        self.assertIn("ensureContentScript(tabId, { force: true })", source)
        ensure = source.split("async function ensureContentScriptUnlocked", 1)[1].split(
            "function withTimeout", 1
        )[0]
        self.assertIn("mustForce", ensure)
        self.assertIn("force || contentScriptForceReinjection.has(tabId)", ensure)
        self.assertIn("waitForTabComplete", ensure)
        self.assertIn("reload required", ensure)
        self.assertIn("Prefer probe even when force is set", ensure)
        self.assertIn("injectContentScript", ensure)
        self.assertIn("executeScriptOnTab", ensure)
        self.assertIn('via: "executeScript"', ensure)
        self.assertIn('recovered_via: "navigate"', source)
        self.assertIn("Content script action timed out", source)
        # Same-URL tabs.update is a no-op; recovery must hard-reload.
        # Seller Dispatch uses window.prompt — click_text must race CDP dialogs.
        click_text = source.split('if (type === "click_text")', 1)[1].split(
            'if (type === "fill_selector")', 1
        )[0]
        self.assertIn("chrome.tabs.reload(state.tabId, { bypassCache: true })", click_text)
        self.assertNotIn("chrome.tabs.update(state.tabId, { url: targetUrl })", click_text)
        self.assertIn("raceClickWithDialog", click_text)
        self.assertIn("dialog_opened", click_text)
        self.assertIn("attachForClick(state.tabId)", click_text)
        no_reload_guard = "if (!debuggerAttached || isForeignExtensionRestriction(error)) throw error;"
        self.assertIn(no_reload_guard, click_text)
        self.assertLess(click_text.index(no_reload_guard), click_text.index("chrome.tabs.reload"))
        self.assertNotIn("cua", click_text.lower())
        parity = (ROOT / "extension" / "parity_capabilities.js").read_text()
        self.assertIn("export function getParityDialog", parity)
        self.assertIn("export async function raceClickWithDialog", parity)
        self.assertIn("ensureContentScriptChain", source)
        self.assertIn("async function ensureContentScript(", source)
        self.assertIn("tabScriptingPoisoned", source)
        self.assertIn("executeScriptOnTab", source)
        self.assertIn("enqueueTabScripting", source)
        manifest = (ROOT / "extension" / "manifest.json").read_text()
        # Chrome-parity: no static always-on content_scripts; action-scoped executeScript.
        self.assertNotIn("content_scripts", json.loads(manifest))
        self.assertIn("scripting", json.loads(manifest)["permissions"])
        self.assertTrue((ROOT / "extension" / "content-scripts" / "cursor-agent.js").is_file())
        # onUpdated must not call ensureContentScript (races Dispatch inject).
        on_updated = source.split(
            "// ---- Navigation invalidation (lazy inject on interaction) ----", 1
        )[1].split("// ---- Content Script Injection Tracking ----", 1)[0]
        self.assertNotIn("ensureContentScript(", on_updated)
        self.assertIn("probeContentScript(tabId)", on_updated)
        send = source.split("async function sendToContentScript", 1)[1].split(
            "function assertRequestLive", 1
        )[0]
        self.assertIn("force: true", send)
        self.assertIn("const ensureMs = 32000", send)
        self.assertIn("const messageMs = 12000", send)
        self.assertIn("sendMessageFast", send)
        self.assertIn("sendMessageRetry", send)
        cursor = (ROOT / "extension" / "content-scripts" / "cursor-agent.js").read_text()
        find_text = cursor.split("function findPointByText(text, mode = 'click')", 1)[1].split(
            "function hasSelector", 1
        )[0]
        self.assertIn(
            "'a,button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=button],input[type=submit],label,summary'",
            find_text,
        )
        self.assertNotIn("p,span,li,td,th", find_text)
        self.assertNotIn(
            "tabScriptingPoisoned.delete(tabId)",
            source.split("function invalidateTabInjection", 1)[1].split(
                "// ---- Navigation invalidation", 1
            )[0],
        )
        probe = source.split("async function probeContentScript", 1)[1].split(
            "async function waitForTabComplete", 1
        )[0]
        self.assertIn("timeoutMs = 1500", probe)
        self.assertIn("probeContentScript", probe)
        self.assertNotIn("clearContentScriptGuard", ensure)
        self.assertIn(
            "async function waitForTabComplete",
            source.split("async function probeContentScript", 1)[1],
        )

    def test_live_content_script_skips_tab_complete_wait_on_lingering_loading(self) -> None:
        """LinkedIn article editor keeps tab.status "loading" (anti-bot iframes).

        Every run's setAgentIdentity used to wait 4s for complete before probing,
        so Browser Use CDP enables outran the harness's 4s timeout (settle
        cdp_slow, "duplicate response for request 5"). Probe first; wait for
        complete only on the inject path. Run envelopes carry the tab title so
        the Browser Use bridge never needs page_context for Target metadata.
        """
        source = SERVICE_WORKER.read_text()
        ensure = source.split("async function ensureContentScriptUnlocked", 1)[1].split(
            "function withTimeout", 1
        )[0]
        early_probe = ensure.index("await probeContentScript(tabId)")
        self.assertLess(early_probe, ensure.index("await waitForTabComplete(tabId)"))
        early = ensure[:ensure.index("await waitForTabComplete(tabId)")]
        self.assertIn('early.status !== "complete"', early)
        self.assertIn("isControllableUrl(early.url)", early)
        self.assertIn("!tabScriptingPoisoned.has(tabId)", early)
        self.assertNotIn("executeScriptOnTab", early)
        self.assertLess(
            ensure.index("await waitForTabComplete(tabId)"), ensure.index("executeScriptOnTab")
        )
        self.assertIn('final_title: tab?.title || ""', source)
        driver = LEASE_DRIVER.read_text()
        page_info = driver.split("def browser_use_page_info", 1)[1].split("def browser_use_hide_cursor", 1)[0]
        self.assertIn('"type": "cdp_events"', page_info)
        self.assertNotIn('{"type": "page_context"}', page_info)

    def test_clicks_fall_back_to_controllable_content_frames_on_foreign_attach_miss(self) -> None:
        source = SERVICE_WORKER.read_text()
        attach = source.split("async function attachForClick", 1)[1].split(
            "async function send", 1
        )[0]
        self.assertIn("await ensureAttached(tabId)", attach)
        self.assertIn("if (!isForeignExtensionRestriction(error)) throw error", attach)
        self.assertIn("return false", attach)

        frame_search = source.split("async function controllableFrameIds", 1)[1].split(
            "async function moveCursorToPoint", 1
        )[0]
        self.assertIn("chrome.webNavigation.getAllFrames", frame_search)
        self.assertIn("isControllableUrl(frame.url)", frame_search)
        self.assertIn("frame_id: frameId", frame_search)
        send = source.split("function sendContentScriptMessage", 1)[1].split(
            "async function sendToContentScript", 1
        )[0]
        self.assertIn("Number.isInteger(frameId) ? frameId : 0", send)

        click_text = source.split('if (type === "click_text")', 1)[1].split(
            'if (type === "fill_selector")', 1
        )[0]
        self.assertIn("if (!debuggerAttached) return clickPromise", click_text)

        click_selector = source.split('if (type === "click_selector")', 1)[1].split(
            "const parity =", 1
        )[0]
        self.assertIn("attachForClick(state.tabId)", click_selector)
        self.assertIn("if (!debuggerAttached)", click_selector)
        self.assertIn("raceClickWithDialog", click_selector)

    def test_parity_capabilities_resolve_named_targets(self) -> None:
        self.assertIn("resolveNamed", PARITY_CAPABILITIES.read_text())

    def test_locator_press_skips_mouse_when_already_focused(self) -> None:
        """Clicking a tall contenteditable's box center moves the caret mid-document."""
        parity = PARITY_CAPABILITIES.read_text()
        press = parity.split('} else if (operation === "press") {', 1)[1].split("} else if", 1)[0]
        self.assertIn("activeElementIsInside", press)
        self.assertIn("if (!focused)", press)
        self.assertIn("dispatchMouse", press)
        typ = parity.split("await hooks.send(state.tabId, \"Input.insertText\"", 1)[0]
        self.assertIn("if (!typeFocused)", typ)
        keys = parity.split("function keyDefinition(value)", 1)[1].split("export async function pressKey", 1)[0]
        self.assertIn("End:", keys)
        self.assertIn("Home:", keys)

    def test_locator_fill_uses_bounded_executeScriptOnTab(self) -> None:
        """Raw chrome.scripting.executeScript on locator fill wedges Draft.js / large contenteditable."""
        parity = PARITY_CAPABILITIES.read_text()
        fill = parity.split('if (operation === "fill")', 1)[1].split("await dispatchMouse", 1)[0]
        self.assertIn("hooks.executeScriptOnTab", fill)
        self.assertIn('"locatorFill"', fill)
        self.assertNotIn("chrome.scripting.executeScript", fill)
        worker = SERVICE_WORKER.read_text()
        hooks = worker.split("runParityAction(action, state, {", 1)[1].split("});", 1)[0]
        self.assertIn("executeScriptOnTab", hooks)

    def test_preflight_failure_uses_verified_cleanup_without_dropping_ownership(self) -> None:
        source = SERVICE_WORKER.read_text()
        preflight = source.split("async function sessionPreflight(message) {", 1)[1].split(
            "function requireSessionLease", 1
        )[0]
        create = preflight.index("await chrome.windows.create")
        register = preflight.index("sessionLeases.set(sessionId, record);")
        discover = preflight.index("await chrome.tabs.query({ windowId })")
        ready = preflight.index("await waitForTabReady(record.tabId")
        catch = preflight.split("} catch (error) {", 1)[1]

        self.assertLess(create, register)
        self.assertLess(register, discover)
        self.assertLess(discover, ready)
        self.assertIn("await persistSessionLeases();", preflight[register:discover])
        self.assertIn(
            'await closeSession(sessionId, { reason: "preflight-failed" });', catch
        )
        self.assertNotIn("sessionLeases.delete(", catch)
        self.assertNotIn("chrome.windows.remove(", catch)
        self.assertIn("cleanupError.cause = error;", catch)
        self.assertIn("cleanupError.leaseToken = record.leaseToken;", catch)

        response_boundary = source.split(
            "enqueueSession(queueKey, () => handleHostMessage(message))", 1
        )[1].split("nextPort.onDisconnect", 1)[0]
        self.assertIn("lease_token: error.leaseToken", response_boundary)

        driver = LEASE_DRIVER.read_text()
        failed_preflight = driver.split(
            'if not preflight.get("success") or not preflight.get("lease_token"):', 1
        )[1].split("lease_token = str(preflight[\"lease_token\"])", 1)[0]
        self.assertIn('lease_token = str(preflight.get("lease_token") or "")', failed_preflight)

    def test_inventory_and_autonomous_events_never_move_browser_windows(self) -> None:
        source = SERVICE_WORKER.read_text()
        status = source.split('if (message?.type === "status") {', 1)[1].split(
            'if (message?.type === "sessions") {', 1
        )[0]
        sessions = source.split('if (message?.type === "sessions") {', 1)[1].split(
            'if (message?.type === "session_preflight") {', 1
        )[0]
        restore = source.split('async function restoreSessionLeases() {', 1)[1].split(
            'let sessionStateError', 1
        )[0]
        removed = source.split('chrome.tabs.onRemoved.addListener', 1)[1].split(
            '// ---- Content Script Messaging ----', 1
        )[0]
        alarm = source.split('chrome.alarms.onAlarm.addListener', 1)[1]

        self.assertNotIn('reapExpiredSessions()', status)
        self.assertNotIn('chrome.tabs.query', status)
        self.assertIn('page_metadata: "lease-required"', status)
        self.assertNotIn('reapExpiredSessions()', sessions)
        self.assertIn('deferAgentWindowLayout("restore")', restore)
        self.assertNotIn('enqueueAgentWindowLayout', restore)
        self.assertIn('reconcileExternallyRemovedLeaseTarget(', removed)
        self.assertNotIn('sessionLeases.delete(', removed)
        self.assertNotIn('enqueueAgentWindowLayout', removed)
        self.assertNotIn('reapExpiredSessions()', alarm)

    def test_mandatory_window_leases_have_no_active_tab_or_tab_group_fallback(self) -> None:
        source = SERVICE_WORKER.read_text()
        self.assertNotIn('function currentTab(', source)
        self.assertNotIn('ensureSessionGroup', source)
        self.assertNotIn('sessionGroups', source)
        self.assertNotIn('sessionTabs', source)
        self.assertNotIn('chrome.tabs.create', source)

    def test_token_private_driver_owns_native_dialog_claim_creation(self) -> None:
        source = LEASE_DRIVER.read_text()
        branch = source.split('elif command == "native_handoff":', 1)[1].split(
            'elif command == "sessions":', 1
        )[0]
        self.assertIn('"type": "cua_runtime_claim"', branch)
        self.assertIn('"leaseToken": lease_token', branch)
        self.assertIn('"intent": "native-dialog"', branch)

    def test_broker_owns_visual_focus_for_every_browser_client(self) -> None:
        driver = LEASE_DRIVER.read_text()
        broker = BROKER.read_text()
        self.assertIn('MACOS_CUA_VISUAL_LOCK_MODULE', broker)
        self.assertIn('VISUAL_REQUEST_TYPES', broker)
        for request_type in ('"session_preflight"', '"session_closeout"', '"run"'):
            self.assertIn(request_type, broker)
        self.assertIn('visual_lease, focus_error = _acquire_visual_focus', broker)
        self.assertIn('visual_lease.release()', broker)
        self.assertIn('target=handle_client, args=(conn,), daemon=False', broker)
        self.assertNotIn('bridge_with_visual_focus', driver)

    def test_hittest_prefers_in_page_over_sticky_and_pins_top_frame(self) -> None:
        """FPL Pick Team: sticky fixture chips and ad iframes must not win locators."""
        cursor = CURSOR_AGENT.read_text()
        worker = SERVICE_WORKER.read_text()

        self.assertIn("function _querySelectorAllDeep", cursor)
        self.assertIn("if (el.shadowRoot) visit(el.shadowRoot)", cursor)
        self.assertIn("if (pos === 'sticky' || pos === 'fixed') return true", cursor)
        self.assertIn("function _preferInPageTargets", cursor)
        self.assertIn("function _hitIsOnTarget", cursor)
        self.assertIn("_clickableAncestor", cursor)
        self.assertIn("_querySelectorAllDeep(value).filter(_isVisible)", cursor)
        find_text = cursor.split("function findPointByText(text, mode = 'click')", 1)[1].split(
            "function hasSelector", 1
        )[0]
        self.assertIn("_preferInPageTargets", find_text)
        self.assertIn("_querySelectorAllDeep", find_text)
        self.assertIn("window !== window.top", cursor)
        self.assertIn("msg?.action === 'getPageContext' || msg?.action === 'getStatus'", cursor)

        self.assertIn("a.frameId === 0", worker)
        self.assertIn("isAdOrTrackerFrameUrl", worker)
        self.assertIn("doubleclick", worker)
        self.assertIn("googlesyndication", worker)
        self.assertIn("twitter", worker)
        pin = worker.split("async function sendToContentScript", 1)[1].split(
            "const ensureMs", 1
        )[0]
        self.assertIn('action === "getPageContext" || action === "getStatus"', pin)
        self.assertIn("frameId = 0", pin)
        self.assertIn('const resp = await sendToContentScript(state.tabId, "getPageContext", pcArgs, 0)', worker)
        self.assertIn('action.compact !== false', worker)
        self.assertIn('pcArgs', worker)
        frames = worker.split("async function findPointOnControllableFrames", 1)[1].split(
            "async function moveCursorToPoint", 1
        )[0]
        self.assertIn("isAdOrTrackerFrameUrl(frame.url)", frames)
        self.assertIn("nonAd.length ? nonAd.concat(ads) : ids", frames)



    def test_sticky_card_y_and_native_checkbox_click(self) -> None:
        """FPL: unique sticky name uses card Y, not the stuck inset; checkbox uses HTMLElement.click().

        Source-contract style cannot execute a live sticky layout. This matches the
        helper that would fail today's case: unique sticky match kept, ancestor
        card taller/non-sticky, click Y from that card rect.
        """
        cursor = CURSOR_AGENT.read_text()
        parity = PARITY_CAPABILITIES.read_text()

        prefer = cursor.split("function _preferInPageTargets", 1)[1].split(
            "function findPointBySelector", 1
        )[0]
        self.assertIn("if (!elements || elements.length <= 1) return elements || [];", prefer)

        self.assertIn("function _stickyCardRect", cursor)
        self.assertIn("function _checkboxControl", cursor)
        self.assertIn("function _hitIsOnStickyCard", cursor)
        card = cursor.split("function _stickyCardRect", 1)[1].split(
            "function _hitIsOnStickyCard", 1
        )[0]
        self.assertIn("tag === 'HTML' || tag === 'BODY' || tag === 'MAIN' || role === 'main'", card)
        self.assertIn("pos === 'sticky' || pos === 'fixed'", card)
        self.assertIn("nr.height <= nameRect.height * 1.5", card)
        self.assertIn("if (hops > 12) break;", card)

        point = cursor.split("async function _pointForElement", 1)[1].split(
            "async function _actionablePoint", 1
        )[0]
        self.assertIn("_stickyCardRect(el, r)", point)
        self.assertIn("_stableRect(el)", point)
        self.assertIn("_hitIsOnStickyCard(el, top)", point)
        # 0.1.13: name-center first; sticky remap only when name center is obscured
        self.assertIn("const nameY = Math.round(r.top + r.height / 2)", point)
        self.assertIn("if (!_hitIsOnTarget(el, top))", point)
        self.assertNotIn("const y = Math.round(clickRect.top + clickRect.height / 2)", point)

        hit = cursor.split("function _hitIsOnTarget(el, top)", 1)[1].split(
            "function _isStickyOrFixedDescendant", 1
        )[0]
        self.assertIn("_hitIsOnStickyCard(el, top)", hit)

        click_fn = cursor.split("function click(expectation = {})", 1)[1].split(
            "function tripleClick", 1
        )[0]
        self.assertIn("_checkboxControl(el)", click_fn)
        self.assertIn("control.click()", click_fn)
        self.assertIn("control.checked === before", click_fn)

        locator = parity.split("function queryLocator(spec)", 1)[1].split(
            "async function resolveLocator", 1
        )[0]
        self.assertIn("const stickyCardRect = (element, nameRect)", locator)
        self.assertIn("tag === \"HTML\" || tag === \"BODY\" || tag === \"MAIN\" || role === \"main\"", locator)
        self.assertIn("y = rect.top + rect.height / 2", locator)

        check = parity.split('["check", "uncheck", "set_checked"]', 1)[1].split(
            'operation === "select_option"', 1
        )[0]
        self.assertIn("control.click()", check)
        self.assertIn("control.checked !== desiredChecked", check)
        self.assertIn("fallback.click()", check)
        self.assertNotIn("dispatchMouse(hooks.send, state.tabId, match.point, 1)", check)


    def test_other_tab_activate_focus_fail_fast_lease_tab_scoped(self) -> None:
        """Foreign activate_tab/focus_tab must not burn EXTENSION_TIMEOUT (~87s footgun)."""
        source = SERVICE_WORKER.read_text()
        self.assertIn("LEASE_TAB_SCOPED", source)
        self.assertIn('type === "activate_tab" || type === "focus_tab"', source)
        self.assertIn("action.tab_id != null && action.tabId == null", source)
        block = source.split('if (type === "activate_tab" || type === "focus_tab") {', 1)[1].split(
            "if (action.tabId) {", 1
        )[0]
        self.assertIn("throw codedError", block)
        self.assertIn("target !== Number(state.tabId)", block)

    def test_viewport_capture_aborts_so_hung_screenshots_cannot_wedge_local_exec(self) -> None:
        """captureVisibleTab hang must fail the run in seconds, not occupy the FIFO for minutes."""
        source = SERVICE_WORKER.read_text()
        self.assertIn("async function captureVisibleTabBounded", source)
        self.assertIn('wrapped.code = "SCREENSHOT_TIMEOUT"', source)
        helper = source.split("async function captureVisibleTabBounded", 1)[1].split(
            "function withTimeout", 1
        )[0]
        self.assertIn("chrome.tabs.captureVisibleTab(windowId, options)", helper)
        self.assertIn('withTimeout(capturePromise, ms, "tabs.captureVisibleTab")', helper)
        self.assertIn("Promise.race", helper)
        fail = source.split("async function captureFailureRecord", 1)[1].split(
            "function compactFailureAction", 1
        )
        # captureFailureRecord is before compact in file? actually compact is first.
        fail = source.split("async function captureFailureRecord", 1)[1].split(
            "return {", 1
        )[0]
        self.assertIn("function failureScreenshotNeeded", source)
        self.assertIn("EXPECT_UNVERIFIED", source)
        self.assertIn('error?.code === "SCREENSHOT_TIMEOUT"', source)
        screenshot = source.split('if (type === "screenshot") {', 1)[1].split(
            'if (type === "zoom") {', 1
        )[0]
        self.assertIn("captureVisibleTabBounded", screenshot)
        self.assertIn("Page.captureScreenshot", screenshot)
        self.assertIn("VIEWPORT_CAPTURE_MS", screenshot)




    def test_chrome_parity_action_scoped_injection_and_trusted_nav(self) -> None:
        """P0: action-scoped inject, trusted CDP nav click, idle park (no overlay/observer)."""
        source = SERVICE_WORKER.read_text()
        cursor = CURSOR_AGENT.read_text()
        manifest = json.loads(MANIFEST.read_text())
        self.assertNotIn("content_scripts", manifest)
        self.assertIn("trustedBrowserClickAtPoint", source)
        self.assertIn("Input.dispatchMouseEvent", source)
        self.assertIn("confirmNavigation", source)
        self.assertIn("parkContentScriptIdle", source)
        self.assertIn("navigation_only", source)
        self.assertIn("nav_click_text", source)
        self.assertIn("nav_click_selector", source)
        self.assertIn("function parkIdle(", cursor)
        self.assertIn("function ensurePageObserver()", cursor)
        self.assertIn("function stopPageObserver()", cursor)
        self.assertIn("observer_active", cursor)
        self.assertIn("overlay_present", cursor)
        # Observer must not start at script load — only via ensurePageObserver.
        self.assertNotIn("new MutationObserver(_bumpPageRevision).observe", cursor)
        # nav_click_* aliases confirm navigation but stay watchable by default
        # (do not force visual_cursor:false / navigation_only:true).
        alias = source.split('if (type === "nav_click_text")', 1)[1].split(
            'if (action.tab_id != null', 1
        )[0]
        self.assertIn("confirm_navigation: true", alias)
        self.assertNotIn("visual_cursor: false", alias)
        self.assertNotIn("navigation_only: true", alias)
        click_text = source.split('if (type === "click_text")', 1)[1].split(
            'if (type === "fill_selector")', 1
        )[0]
        self.assertIn("confirmNav", click_text)
        self.assertIn("const visualCursor = !silent && actionWantsVisualCursor(action, state)", click_text)
        self.assertIn("const silent = actionIsSilent(action, state)", click_text)
        self.assertIn("lingerMs: action.linger_ms", click_text)
        # Watchable lease: park only on silent path (not after every click).
        self.assertIn("if (silent) await parkContentScriptIdle(state.tabId, 0)", click_text)
        self.assertNotIn("if (!visualCursor) await parkContentScriptIdle", click_text)
        self.assertIn("} finally {", click_text)
        self.assertIn(
            "if (silent) await parkContentScriptIdle(state.tabId, 0)",
            click_text.split("} finally {", 1)[1],
        )
        click_selector = source.split('if (type === "click_selector")', 1)[1].split(
            "const parity = await runParityAction", 1
        )[0]
        self.assertIn("confirmNav", click_selector)
        self.assertIn("const visualCursor = !silent && actionWantsVisualCursor(action, state)", click_selector)
        self.assertIn("const silent = actionIsSilent(action, state)", click_selector)
        self.assertIn("if (silent) await parkContentScriptIdle(state.tabId, 0)", click_selector)
        self.assertIn("} finally {", click_selector)
        self.assertIn(
            "if (silent) await parkContentScriptIdle(state.tabId, 0)",
            click_selector.split("} finally {", 1)[1],
        )
        click_at = source.split("async function clickAtPoint", 1)[1].split("async function clickResolvedTarget", 1)[0]
        self.assertIn("trustedBrowserClickAtPoint", click_at)
        self.assertIn("moveCursorToPoint", click_at)
        self.assertIn("lingerMs", click_at)
        # Visual path: no hide-before-trusted CDP; keepVisible through click.
        self.assertNotIn('sendToContentScript(tabId, "hide"', click_at)
        self.assertIn("keepVisible: visual", click_at)
        self.assertIn("cursor_visible_during_click", click_at)
        # Visual path must not park-before-move via navClickMode.
        self.assertNotIn("navClickMode || !visual", click_at)
        # Silent failure still parks; watchable leaves cursor for the lease.
        self.assertIn("if (!visual) await parkContentScriptIdle", click_at)
        trusted = source.split("async function trustedBrowserClickAtPoint", 1)[1].split(
            "async function clickAtPoint", 1
        )[0]
        self.assertIn("keepVisible", trusted)
        self.assertIn("if (!keepVisible)", trusted)
        click_xy = source.split('if (type === "click_at_xy"', 1)[1].split(
            'if (type === "click_text")', 1
        )[0]
        self.assertNotIn('sendToContentScript(state.tabId, "hide"', click_xy)
        self.assertIn("keepVisible: visual", click_xy)
        self.assertIn("width: 18px;", cursor)
        self.assertIn("height: 18px;", cursor)
        self.assertNotIn("width: 28px;", cursor)
        self.assertIn("drop-shadow(0 0 8px", cursor)
        # UX Step 2: on-demand click ring (never always-on at load).
        self.assertIn("function showClickRing", cursor)
        self.assertIn("function clearClickRings", cursor)
        self.assertIn("comet-control-click-ring", cursor)
        self.assertIn("function pulseClick()", cursor)
        self.assertIn("showClickRing(cursorX, cursorY)", cursor)
        self.assertIn('sendToContentScript(tabId, "pulseClick"', click_at)
        self.assertIn("keepClickRing", click_at)
        self.assertIn("click_ring", click_at)
        # Ring CSS/helpers must not auto-run at script load.
        load_tail = cursor.split("createOverlay", 1)[0]
        self.assertNotIn("showClickRing(", load_tail)
        self.assertNotIn("pulseClick()", load_tail)

    def test_session_warm_continuous_cursor_018(self) -> None:
        """0.1.8: lease watchable default, session-warm visible after setIdentity, no park between watchable acts."""
        source = SERVICE_WORKER.read_text()
        cursor = CURSOR_AGENT.read_text()
        assert_packaged_version(self)

        # Lease watchable default on record + publicLease
        self.assertIn("function leaseIsWatchable", source)
        self.assertIn("function actionIsSilent", source)
        self.assertIn("function actionWantsVisualCursor", source)
        self.assertIn("const watchable = !(message.silent === true || message.watchable === false)", source)
        self.assertIn("watchable: record.watchable !== false", source)

        # setAgentIdentity warms watchable sessions
        sai = source.split("async function setAgentIdentity", 1)[1].split("async function sessionPreflight", 1)[0]
        self.assertIn("options.sessionWarm === true", sai)
        self.assertIn("ensureSessionCursorWarm", sai)
        self.assertIn("setAgentIdentity(record.tabId, record, { sessionWarm: true })", source)
        self.assertIn("setAgentIdentity(state.tabId, state.leaseRecord, { sessionWarm: true })", source)
        self.assertIn("cursorSilentPark", source)
        self.assertIn("record.cursorSilentPark !== true", source)

        # click paths force visual when lease watchable; keepVisible CDP; no park on watchable
        self.assertIn("actionIsSilent(action, state)", source)
        self.assertIn("actionWantsVisualCursor(action, state)", source)
        click_xy = source.split('if (type === "click_at_xy"', 1)[1].split(
            'if (type === "click_text")', 1
        )[0]
        self.assertIn("actionWantsVisualCursor(action, state)", click_xy)
        self.assertIn("keepVisible: visual", click_xy)
        self.assertIn("if (!visual) await parkContentScriptIdle", click_xy)

        click_at = source.split("async function clickAtPoint", 1)[1].split(
            "async function clickResolvedTarget", 1
        )[0]
        self.assertIn("keepVisible: visual", click_at)
        self.assertNotIn('sendToContentScript(tabId, "hide"', click_at)
        self.assertIn("if (!visual) await parkContentScriptIdle", click_at)

        # Content-script session warm
        self.assertIn("function ensureSessionCursorWarm", cursor)
        self.assertIn("function createOverlay(options = {})", cursor)
        self.assertIn("options.sessionWarm || options.forceVisible", cursor)
        self.assertIn("__cometControlSessionWarm", cursor)
        set_id = cursor.split("function setIdentity(identity = {})", 1)[1].split(
            "function clearIdentity", 1
        )[0]
        self.assertIn("identity.sessionWarm === true", set_id)
        self.assertIn("comet-control-visible", set_id)
        self.assertIn("isVisible = true", set_id)
        actions = cursor.split("const actions = {", 1)[1].split("};", 1)[0]
        self.assertIn("ensureSessionCursorWarm", actions)
        park = cursor.split("function parkIdle", 1)[1].split("function evaluateInPage", 1)[0]
        self.assertIn("__cometControlSessionWarm = false", park)
        click_text = source.split('if (type === "click_text")', 1)[1].split(
            'if (type === "fill_selector")', 1
        )[0]
        self.assertIn("if (silent) await parkContentScriptIdle(state.tabId, 0)", click_text)
        self.assertNotIn("if (!visualCursor) await parkContentScriptIdle", click_text)



    def test_compact_semantic_page_context_019(self) -> None:
        """0.1.9: compact page_context default + accessible-name click_text finders."""
        cursor = CURSOR_AGENT.read_text()
        worker = SERVICE_WORKER.read_text()
        assert_packaged_version(self)
        self.assertIn("function _accessibleName", cursor)
        self.assertIn("options.compact !== false", cursor)
        self.assertIn("compact: true", cursor)
        self.assertIn("[role=link],[role=tab],[role=menuitem]", cursor)
        find_text = cursor.split("function findPointByText(text, mode = 'click')", 1)[1].split(
            "function hasSelector", 1
        )[0]
        self.assertIn("_accessibleName(el)", find_text)
        self.assertIn("[role=link]", find_text)
        pc = cursor.split("function getPageContext", 1)[1].split("function flashLabel", 1)[0]
        self.assertIn("Primary chrome only", pc)
        self.assertIn("hrefOf", pc)
        branch = worker.split('  if (type === "page_context") {', 1)[1].split(
            '  if (type === "console_tail")', 1
        )[0]
        self.assertIn("pcArgs", branch)
        self.assertIn("action.compact !== false", branch)
        self.assertIn("...(last_error ? {", branch)
        self.assertNotIn(
            "last_console_error: last_error ? { level: last_error.level, text: last_error.text, t: last_error.t } : null,",
            branch,
        )

    def test_oncall_preflight_page_context_goto_0110(self) -> None:
        """0.1.10 keepers: longer tab-ready floor, url/title sections, goto final url, link prefs, navigate alias."""
        worker = SERVICE_WORKER.read_text()
        cursor = CURSOR_AGENT.read_text()
        assert_packaged_version(self)

        wait = worker.split("async function waitForTabReady", 1)[1].split("async function setAgentIdentity", 1)[0]
        self.assertIn("lastTab", wait)
        self.assertIn("status=${status}", wait)
        self.assertIn("Math.max(25000, Math.floor(remainingMs() * 0.6))", worker)

        goto = worker.split('  if (type === "goto") {', 1)[1].split('  if (type === "back"', 1)[0]
        self.assertIn("const settled = await chrome.tabs.get(state.tabId)", goto)
        self.assertIn("const finalUrl = settled?.url || action.url", goto)
        self.assertIn("url: finalUrl", goto)

        alias = worker.split("async function runBrowserAction", 1)[1].split("if (action.tab_id", 1)[0]
        self.assertIn('type === "navigate"', alias)
        self.assertIn('type === "scroll"', alias)
        self.assertIn('type: "goto"', alias)
        self.assertIn('type: "cursor_scroll"', alias)

        pc = cursor.split("function getPageContext", 1)[1].split("function flashLabel", 1)[0]
        self.assertIn("'url', 'title', 'headings', 'nav', 'links', 'buttons', 'inputs'", pc)
        self.assertIn("url, title, headings, nav, links, buttons, inputs", pc)
        self.assertIn("const linkCap = compact ? 20 : 35", pc)
        self.assertIn('main,article,[role="main"]', pc)
        self.assertIn("[...preferred, ...rest, ...secondary]", pc)

    def test_oncall_preflight_soft_ready_0111(self) -> None:
        """0.1.11: 90s default preflight + soft-ready while status=loading on controllable URL."""
        worker = SERVICE_WORKER.read_text()
        assert_packaged_version(self)

        wait = worker.split("async function waitForTabReady", 1)[1].split("async function setAgentIdentity", 1)[0]
        self.assertIn("SOFT_READY_MS", wait)
        self.assertIn("stableUrl", wait)
        self.assertIn("stableSince", wait)
        self.assertIn('if (tab.status === "complete") return tab;', wait)
        self.assertIn("Date.now() - stableSince >= SOFT_READY_MS", wait)

        preflight = worker.split("async function sessionPreflight", 1)[1].split("async function ", 1)[0]
        self.assertIn('boundedNumber(message.timeoutSeconds, 90, 1, 300, "timeoutSeconds")', preflight)
        self.assertNotIn('boundedNumber(message.timeoutSeconds, 45, 1, 300, "timeoutSeconds")', preflight)

    def test_action_expect_and_decisive_failure_screenshots(self) -> None:
        source = SERVICE_WORKER.read_text()
        cursor = CURSOR_AGENT.read_text()
        self.assertIn("action-expect", source)
        self.assertIn("verifyActionExpect(action, result, state)", source)
        fill = source.split('  if (type === "fill_selector") {', 1)[1].split(
            '  if (type === "click_selector") {', 1
        )[0]
        self.assertIn("fillBySelector", fill)
        self.assertNotIn("clickResolvedTarget", fill)
        self.assertNotIn("focusAndType", fill)
        self.assertIn("function fillBySelector", cursor)
        self.assertIn("TYPE_CURSOR_NOT_READY", cursor)
        focus_and_type = cursor.split("function focusAndType(text, opts = {}, expectation = {}) {", 1)[1].split(
            "function keyPress", 1
        )[0]
        self.assertIn("TYPE_CURSOR_NOT_READY", focus_and_type)
        self.assertNotIn("if (!cursorEl) return;", focus_and_type)
        helpers = source.split("function normalizeExpect(raw) {", 1)[1].split(
            "async function collectExpectEvidence", 1
        )[0]
        harness = r'''
const assert = require('node:assert/strict');
function codedError(code, message) { return Object.assign(new Error(message), {code}); }
function normalizeExpect(raw) {HELPER
const evidence = {
  url: 'https://example.com/dashboard',
  title: 'Overview',
  headings: [{tag:'H1', text:'Overview'}],
  buttons: ['Save'],
  inputs: [{name:'degree', value:'Bachelor'}],
};
assert.deepEqual(normalizeExpect('Saved')[0], {kind:'text', value:'Saved'});
assert.equal(unmatchedExpect(normalizeExpect({url_contains:'/dashboard'}), evidence), null);
assert.equal(unmatchedExpect(normalizeExpect({heading:'Overview'}), evidence), null);
assert.equal(unmatchedExpect(normalizeExpect({value:'Bachelor'}), evidence), null);
assert.equal(unmatchedExpect(normalizeExpect({text:'Save'}), evidence), null);
assert.equal(unmatchedExpect(normalizeExpect({text:'Saved'}), { ...evidence, body:'Payment Saved' }), null);
assert.equal(unmatchedExpect(normalizeExpect({text:'Missing'}), evidence)?.kind, 'text');
assert.equal(failureScreenshotNeeded({code:'ACTIONABILITY_TARGET_COUNT'}, {type:'click_text'}), false);
assert.equal(failureScreenshotNeeded({code:'EXPECT_UNVERIFIED'}, {type:'page_context'}), false);
assert.equal(failureScreenshotNeeded({code:'SCREENSHOT_TIMEOUT'}, {type:'goto'}), false);
assert.equal(failureScreenshotNeeded({message:'EvalError: side-effect'}, {type:'evaluate'}), false);
assert.equal(failureScreenshotNeeded({code:'UNKNOWN_VISUAL'}, {type:'click_text'}), true);
console.log('ok');
'''
        script = harness.replace("HELPER", helpers)
        completed = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("ok", completed.stdout)

    def test_first_click_stable_rect_0112(self) -> None:
        """0.1.12: three consecutive matching samples, not two; first-sample-only motion is not enough."""
        cursor = CURSOR_AGENT.read_text()
        assert_packaged_version(self)
        self.assertIn("async function _stableRect", cursor)
        self.assertIn("viewport_changed", cursor)
        self.assertIn("scrolls", cursor)
        self.assertIn("viewports", cursor)
        helpers = cursor.split("function _sameRect(left, right) {", 1)[1].split(
            "function _semanticCacheKey", 1
        )[0]
        harness = r'''
const assert = require('node:assert/strict');
const window = { scrollX: 0, scrollY: 0, innerWidth: 800, innerHeight: 600 };
let now = 0;
const performance = { now: () => now };
const requestAnimationFrame = (cb) => { now += 16; queueMicrotask(() => cb()); };
const setTimeout = (cb) => { queueMicrotask(() => cb()); };
function _actionabilityError(code, message, details = {}) {
  return Object.assign(new Error(message), { code, details });
}
function _sameRect(left, right) {HELPER
function rect(top) { return { top, left: 0, width: 80, height: 18 }; }
function element(seq) {
  let i = 0;
  return { getBoundingClientRect: () => seq[Math.min(i++, seq.length - 1)] };
}
(async () => {
  const settled = await _stableRect(element([rect(215.922), rect(215.922), rect(215.328), rect(215.328), rect(215.328)]));
  assert.equal(settled.top, 215.328);
  await assert.rejects(
    () => _stableRect(element([rect(1), rect(2), rect(3), rect(4), rect(5)]), { maxFrames: 4, maxMs: 1000 }),
    (err) => err.code === 'ACTIONABILITY_UNSTABLE' && err.details.rects.length >= 3
  );
  await assert.rejects(
    () => _stableRect(element([rect(1), rect(1), rect(2), rect(2), rect(1), rect(1)]), { maxFrames: 6, maxMs: 1000 }),
    (err) => err.code === 'ACTIONABILITY_UNSTABLE'
  );
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
'''
        script = harness.replace("HELPER", helpers)
        completed = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("ok", completed.stdout)

    def test_review_cta_locator_hardening_0113(self) -> None:
        """0.1.13: ambiguous selector count>1 must not become count=0 via iframe walk;
        click_selector honors optional text; name-center click before sticky remap."""
        cursor = CURSOR_AGENT.read_text()
        worker = SERVICE_WORKER.read_text()
        assert_packaged_version(self)
        self.assertEqual(json.loads(PLUGIN_JSON.read_text())["version"], "0.1.13")

        miss = worker.split("function isLocatorMissError(error) {", 1)[1].split(
            "function isContentScriptTimeoutError", 1
        )[0]
        self.assertIn('code === "ACTIONABILITY_TARGET_COUNT"', miss)
        self.assertIn("count === 0", miss)

        frames = worker.split(
            "async function findPointOnControllableFrames(tabId, action, args, fallback) {", 1
        )[1].split("async function moveCursorToPoint", 1)[0]
        self.assertIn("Ambiguous match on this frame is definitive", frames)
        self.assertIn("Number(error?.details?.count) > 1", frames)

        fps = cursor.split("function findPointBySelector", 1)[1].split(
            "function findPointByText", 1
        )[0]
        self.assertIn("text = ''", fps)
        self.assertIn("||text=", fps)

        branch = worker.split('if (type === "click_selector") {', 1)[1][:3500]
        self.assertIn('action.text || ""', branch)
        self.assertIn(
            'findPointBySelector(state.tabId, action.selector, "click", action.text || "")',
            branch,
        )

        point = cursor.split("async function _pointForElement", 1)[1].split(
            "async function _actionablePoint", 1
        )[0]
        self.assertIn("Prefer the matched control's own center", point)




if __name__ == "__main__":
    unittest.main()
