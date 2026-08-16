import importlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICE_WORKER = ROOT / "extension" / "service_worker.js"
CURSOR_AGENT = ROOT / "extension" / "content-scripts" / "cursor-agent.js"
PARITY_CAPABILITIES = ROOT / "extension" / "parity_capabilities.js"
LEASE_DRIVER = (
    ROOT.parents[1] / "skills" / "comet-control" / "scripts" / "lease_driver.py"
)
_SIBLING_LOCK = (
    ROOT.parents[1].parent
    / "agent-computer-use"
    / "skills"
    / "macos-cua"
    / "scripts"
    / "visual_focus_lock.py"
)
VISUAL_FOCUS_LOCK = (
    _SIBLING_LOCK
    if _SIBLING_LOCK.is_file()
    else Path.home() / ".agents" / "skills" / "macos-cua" / "scripts" / "visual_focus_lock.py"
)
TOOLS = ROOT / "tools.py"
DASHBOARD_TEST = ROOT / "tests" / "test_dashboard_interactions.py"
ISOLATION_TEST = ROOT / "tests" / "test_multi_agent_isolation.py"
CANONICAL_SKILL = ROOT.parents[1] / "skills" / "comet-control" / "SKILL.md"
BROKER = ROOT / "native" / "broker.py"
OWNER_PROBE = ROOT.parents[1] / "scripts" / "ensure-wip-broker.sh"
RUNTIME_LAUNCHER = ROOT.parents[1] / "scripts" / "launch-wip-comet.sh"
DIAGNOSTICS = ROOT / "diagnostics.py"
MANIFEST = ROOT / "extension" / "manifest.json"


class _RegistrationContext:
    def __init__(self) -> None:
        self.skills = []

    def register_tool(self, **_kwargs) -> None:
        pass

    def register_skill(self, *args) -> None:
        self.skills.append(args)


class SourceContractTests(unittest.TestCase):
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

    def test_plugin_registers_the_single_repository_skill_realpath(self) -> None:
        sys.path.insert(0, str(ROOT.parent))
        try:
            package = importlib.import_module("comet_control")
        finally:
            sys.path.pop(0)

        context = _RegistrationContext()
        package.register(context)

        self.assertEqual(len(context.skills), 1)
        name, registered_path, _description = context.skills[0]
        self.assertEqual(name, "comet-control")
        self.assertTrue(CANONICAL_SKILL.is_file())
        self.assertEqual(Path(registered_path).resolve(), CANONICAL_SKILL.resolve())
        self.assertTrue(Path(registered_path).samefile(CANONICAL_SKILL))

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

    def test_private_lease_tokens_are_process_owned_and_not_tool_arguments(self) -> None:
        sys.path.insert(0, str(ROOT.parent))
        try:
            tools = importlib.import_module("comet_control.tools")
        finally:
            sys.path.pop(0)

        properties = tools.COMET_CONTROL_BROWSER_SCHEMA["parameters"]["properties"]
        self.assertNotIn("lease_token", properties)
        self.assertNotIn("leaseToken", properties)

        tool_source = TOOLS.read_text()
        driver_source = LEASE_DRIVER.read_text()
        self.assertIn("_LEASE_TOKENS: dict[str, str]", tool_source)
        self.assertIn('session.pop("lease_token", "")', tool_source)
        self.assertIn("def _redact_private(value: Any)", tool_source)
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
        focus = 'await chrome.windows.update(leasedTab.windowId, { focused: true, drawAttention: true });'
        verify = 'Number(activeTab.id) !== Number(state.tabId)'
        capture = 'chrome.tabs.captureVisibleTab(leasedTab.windowId, options)'

        self.assertIn('return enqueueViewportCapture(async () => {', branch)
        self.assertIn('state.windowId = leasedTab.windowId;', branch)
        self.assertIn(focus, branch)
        self.assertIn(activate, branch)
        self.assertIn(verify, branch)
        self.assertIn(capture, branch)
        self.assertIn('/image readback failed/i', branch)
        self.assertIn('for (let attempt = 0; attempt < 2; attempt += 1)', branch)
        self.assertLess(branch.index(focus), branch.index(capture))
        self.assertLess(branch.index(activate), branch.index(capture))
        self.assertLess(branch.index(verify), branch.index(capture))

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
            screenshot.index('chrome.tabs.captureVisibleTab(leasedTab.windowId, options)'),
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
        self.assertIn('function codedError(code, message, { retryable = false } = {})', source)

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
        self.assertIn("manifest-settle", ensure)
        self.assertNotIn("executeScriptOnTab", ensure)
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
        self.assertIn("ensureAttached(state.tabId)", click_text)
        parity = (ROOT / "extension" / "parity_capabilities.js").read_text()
        self.assertIn("export function getParityDialog", parity)
        self.assertIn("export async function raceClickWithDialog", parity)
        self.assertIn("ensureContentScriptChain", source)
        self.assertIn("async function ensureContentScript(", source)
        self.assertIn("tabScriptingPoisoned", source)
        self.assertIn("executeScriptOnTab", source)
        self.assertIn("enqueueTabScripting", source)
        manifest = (ROOT / "extension" / "manifest.json").read_text()
        self.assertIn("content_scripts", manifest)
        self.assertIn("cursor-agent.js", manifest)
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
            "'a,button,[role=button],input[type=button],input[type=submit],label,summary'",
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

    def test_deploy_extension_mirrors_plugin_service_worker(self) -> None:
        deploy = ROOT.parents[1] / "deploy" / "extension" / "service_worker.js"
        self.assertTrue(deploy.is_file())
        self.assertEqual(
            SERVICE_WORKER.read_text(),
            deploy.read_text(),
            "deploy/extension/service_worker.js is stale; run scripts/sync-wip.sh",
        )

    def test_deploy_extension_mirrors_plugin_parity_capabilities(self) -> None:
        deploy = ROOT.parents[1] / "deploy" / "extension" / "parity_capabilities.js"
        self.assertTrue(deploy.is_file(), "deploy/extension/parity_capabilities.js missing")
        self.assertEqual(
            PARITY_CAPABILITIES.read_text(),
            deploy.read_text(),
            "deploy/extension is stale; run scripts/sync-wip.sh after editing plugin/",
        )
        self.assertIn("resolveNamed", PARITY_CAPABILITIES.read_text())

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
        tools = TOOLS.read_text()
        focus = VISUAL_FOCUS_LOCK.read_text()
        self.assertIn('MACOS_CUA_VISUAL_LOCK_MODULE', broker)
        self.assertIn('VISUAL_REQUEST_TYPES', broker)
        for request_type in ('"session_preflight"', '"session_closeout"', '"run"'):
            self.assertIn(request_type, broker)
        self.assertIn('visual_lease, focus_error = _acquire_visual_focus', broker)
        self.assertIn('visual_lease.release()', broker)
        self.assertIn('target=handle_client, args=(conn,), daemon=False', broker)
        self.assertIn('_run_extension_bridge(request, timeout_seconds=timeout_seconds)', tools)
        self.assertNotIn('bridge_with_visual_focus', driver)
        self.assertIn('fcntl.LOCK_EX | fcntl.LOCK_NB', focus)
        self.assertIn('_LOCAL_LOCK = threading.Lock()', focus)
        self.assertIn('def release(self)', focus)
        self.assertIn('visual-focus-v1', focus)


if __name__ == "__main__":
    unittest.main()
