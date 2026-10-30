import AppKit
import XCTest
@testable import CUAService

/// Island hosted by CUAService: alive with voice off, hub auth/gating, panel lifecycle.
/// MemoryTokenStore + temp UserDefaults suite only — never the real Keychain.
@MainActor
final class IslandHubTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var mem: MemoryTokenStore!
    private var store: IslandSettingsStore!

    override func setUp() {
        suite = "cua.island.hub.test.\(UUID().uuidString)"
        defaults = TestDefaults.make(suite)
        mem = MemoryTokenStore()
        store = IslandSettingsStore(defaults: defaults, tokens: mem)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
    }

    private func req(_ method: String, _ path: String, token: String? = nil, json: [String: Any]? = nil,
                     host: String = "127.0.0.1:8777", query: [String: String] = [:],
                     extra: [String: String] = [:]) -> IslandHub.Request {
        var h = ["host": host].merging(extra) { $1 }
        if let token { h["authorization"] = "Bearer \(token)" }
        return .init(method: method, path: path, query: query, headers: h,
                     body: json.map { try! JSONSerialization.data(withJSONObject: $0) } ?? Data(), localPort: 8777)
    }

    private func token(_ s: AgentSource) throws -> String {
        try store.rotateToken(s)
        return try XCTUnwrap(mem.tokens[s.rawValue])
    }

    // MARK: - Hub (transport-independent)

    func testTokenRequiredAndChecked() throws {
        let hub = IslandHub(store: store)
        let body: [String: Any] = ["source": "grok-bot", "kind": "running", "title": "Booking table"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", json: body), port: 8777).status, 401, "unsigned")
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: "nope", json: body), port: 8777).status, 401, "no token provisioned")
        let t = try token(.grokBot)
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t + "x", json: body), port: 8777).status, 401, "wrong token")
        let cursorTok = try token(.cursor)
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursorTok, json: body), port: 8777).status, 401,
                       "another source's token can't post as grok")
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: body), port: 8777).status, 200)
        XCTAssertEqual(hub.compose().agents.map(\.title), ["Booking table"])
        // Local-dev flag (off by default) admits unsigned requests.
        defaults.set(true, forKey: IslandHub.allowUnsignedKey)
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", json: body), port: 8777).status, 200)
    }

    func testOriginAndHostChecks() throws {
        let hub = IslandHub(store: store)
        let t = try token(.cursor)
        let body: [String: Any] = ["source": "cursor", "title": "x"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: body, extra: ["origin": "https://evil.example"]), port: 8777).status, 403)
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: body, host: "evil.example:8777"), port: 8777).status, 403)
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: body, host: "localhost:8777"), port: 8777).status, 200)
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "mallory", "title": "x"]), port: 8777).status, 400)
    }

    func testDisabledSourceIsDropped() throws {
        let hub = IslandHub(store: store)
        let t = try token(.cursor)
        store.set(false, for: AgentSource.cursor.keyPrefix + "enabled")
        let r = hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "cursor", "title": "Tests"]), port: 8777)
        XCTAssertEqual(r.status, 403)
        XCTAssertTrue(hub.compose().agents.isEmpty)
        // Switching a source off later drops its rows at once.
        store.set(true, for: AgentSource.cursor.keyPrefix + "enabled")
        _ = hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "cursor", "title": "Tests"]), port: 8777)
        XCTAssertEqual(hub.compose().agents.count, 1)
        store.set(false, for: AgentSource.cursor.keyPrefix + "enabled")
        hub.applyGating()
        XCTAssertTrue(hub.compose().agents.isEmpty)
    }

    func testAgentRosterRegistrationLinksChildrenAndRejectsSelfParent() throws {
        let hub = IslandHub(store: store)
        let t = try token(.agentRoster)
        let parent: [String: Any] = ["source": "agent", "id": "parent-1", "name": "Codex",
                                     "kind": "running", "title": "Implement roster"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: parent), port: 8777).status, 200)
        let child: [String: Any] = ["source": "agent", "id": "child-1", "name": "Claude Code",
                                   "parent_id": "parent-1", "kind": "running", "title": "Review UI"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: child), port: 8777).status, 200)

        let rows = IslandAgentRows.hierarchical(hub.compose().agents)
        XCTAssertEqual(rows.map(\.id), ["agent:parent-1", "agent:child-1"])
        XCTAssertEqual(rows.map(\.depth), [0, 1])
        XCTAssertEqual(rows.map { $0.agent.name }, ["Codex", "Claude Code"])
        XCTAssertEqual(hub.entries["agent:parent-1"]?.ttl, 3600)

        let selfParent: [String: Any] = ["source": "agent", "id": "loop", "kind": "running",
                                        "title": "Invalid", "parent_id": "loop"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: selfParent), port: 8777).status, 400)
    }

    func testLocalHarnessRowsMergeIntoRoster() throws {
        let hub = IslandHub(store: store)
        let detected = (42...45).map {
            IslandAgent(id: "local:codex-thread:t\($0)", name: "Codex", app: "Codex", title: "Thread \($0)",
                        status: "running", elapsed_s: Double($0), last_seen_at: Double($0))
        }
        hub.updateLocalHarnesses(detected)
        XCTAssertEqual(hub.localHarnesses, detected)
        let snapshot = hub.compose()
        XCTAssertEqual(Set(snapshot.agents.map(\.id)), Set(detected.map(\.id)))

        let model = IslandModel()
        model.idleActivity = "agents"
        _ = model.apply(snapshot)
        XCTAssertEqual(model.listRows.map(\.id), detected.map(\.id))
        XCTAssertTrue(model.localProcessRows.isEmpty, "no process section")
        XCTAssertFalse(model.sourceGroups.contains { $0.processCount > 0 })
        XCTAssertNil(IslandAgentRows.control(for: detected[0]), "local threads have no Stop")
        model.openAgentList()
        XCTAssertEqual(model.rosterVisibleRows, 3)
        XCTAssertTrue(model.rosterHasMore, "+1 more")
        XCTAssertFalse(model.accessibilityText.contains("local observations"))

        let state = hub.handle(req("GET", "/api/island/state", token: try token(.codex), query: ["source": "codex"]), port: 8777)
        XCTAssertEqual(state.status, 200)
        XCTAssertEqual(state.body["agents"], "4", "local threads are roster rows")
        XCTAssertEqual(state.body["local_processes"], "0")
    }

    func testApprovalFlowAndGating() throws {
        let hub = IslandHub(store: store)
        let codex = try token(.codex)
        let body: [String: Any] = ["source": "codex", "id": "m1", "kind": "approval", "title": "Run migration?"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: codex, json: body), port: 8777).status, 403,
                       "Codex defaults to info cap → no approval cards")
        let cursor = try token(.cursor)
        var b = body; b["source"] = "cursor"; b["detail"] = "staging"
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: b), port: 8777).status, 200)
        let card = try XCTUnwrap(hub.compose().agent_approval)
        XCTAssertEqual(card.agent, "Cursor"); XCTAssertFalse(card.destructive)
        let poll = req("GET", "/api/agent/approval", token: cursor, query: ["source": "cursor", "id": "m1"])
        XCTAssertEqual(hub.handle(poll, port: 8777).body["status"], "pending")
        // Another source can't answer Cursor's card even with the same id.
        let grok = try token(.grokBot)
        XCTAssertEqual(hub.handle(req("POST", "/api/agent/approval", token: grok, json: ["source": "grok", "id": "m1", "approved": true]), port: 8777).status, 404)
        hub.resolveApproval(id: card.id, approved: true)
        XCTAssertNil(hub.compose().agent_approval)
        XCTAssertEqual(hub.handle(poll, port: 8777).body["status"], "approved")
    }

    func testDestructiveApprovalsRequireTargetScope() throws {
        let hub = IslandHub(store: store)
        let cursor = try token(.cursor)
        var body: [String: Any] = ["source": "cursor", "id": "delete-1", "kind": "approval",
                                   "title": "Delete stale branches?", "destructive": true]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: body), port: 8777).status, 400)
        body["detail"] = "Targets: feature/cache, stale/logging, migration/legacy"
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: body), port: 8777).status, 200)
        XCTAssertEqual(hub.compose().agent_approval?.subtitle, "Targets: feature/cache, stale/logging, migration/legacy")
    }

    /// Agent prompts stay off the island while their source app is frontmost (already inline there);
    /// switching away shows them, switching back hides them again — never answered or dismissed.
    func testPromptsHiddenWhileSourceAppIsFrontmost() throws {
        let hub = IslandHub(store: store)
        var front: String? = "com.todesktop.230313mzl4w4u92"   // Cursor.app
        hub.frontmostBundleID = { front }
        var snaps = 0
        hub.onSnapshot = { _ in snaps += 1 }
        let cursor = try token(.cursor)
        let body: [String: Any] = ["source": "cursor", "id": "g1", "kind": "approval", "title": "Run migration?"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: body), port: 8777).status, 200)
        XCTAssertNil(hub.compose().agent_approval, "inline in Cursor: not on the island")
        XCTAssertEqual(hub.compose().pending_count, 0)
        let poll = req("GET", "/api/agent/approval", token: cursor, query: ["source": "cursor", "id": "g1"])
        XCTAssertEqual(hub.handle(poll, port: 8777).body["status"], "pending")

        front = "com.apple.finder"; snaps = 0
        hub.frontmostAppChanged()
        XCTAssertEqual(snaps, 1)
        XCTAssertEqual(hub.compose().agent_approval?.id, "cursor:g1", "switched away: shown")

        front = "com.todesktop.230313mzl4w4u92"
        hub.frontmostAppChanged()
        XCTAssertNil(hub.compose().agent_approval, "back in Cursor: hidden again")
        XCTAssertEqual(hub.handle(poll, port: 8777).body["status"], "pending", "hiding never answers the prompt")

        // Another source's card is unaffected by Cursor being frontmost.
        store.set("attention", for: AgentSource.grokBot.keyPrefix + "cap")
        let grok = try token(.grokBot)
        let g: [String: Any] = ["source": "grok", "id": "g2", "kind": "approval", "title": "Open the page?"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: grok, json: g), port: 8777).status, 200)
        XCTAssertEqual(hub.compose().agent_approval?.id, "grok:g2")
        // Unknown front app → shown.
        front = nil
        XCTAssertEqual(hub.compose().pending_count, 2)
    }

    func testRowsExpireAndClear() throws {
        let hub = IslandHub(store: store)
        let t = try token(.grokBot)
        let t0 = Date()
        _ = hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "grok", "id": "a", "kind": "done", "title": "Booked"]), port: 8777, now: t0)
        _ = hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "grok", "id": "b", "title": "Research"]), port: 8777, now: t0)
        _ = hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "grok", "id": "c", "kind": "error", "title": "Run tests",
                                                                           "detail": "live-fd: 44 → 51 fds\nmore", "error": "fd 44 -> 51"]), port: 8777, now: t0)
        XCTAssertEqual(hub.compose(now: t0).agents.count, 3)
        let failed = try XCTUnwrap(hub.compose(now: t0).agents.first { $0.title == "Run tests" })
        XCTAssertEqual(failed.tooltip, "fd 44 -> 51", "technical error in the tooltip")
        XCTAssertEqual(IslandAgentRows.statusText(failed), "live-fd: 44 → 51 fds", "error meta is the error itself")
        hub.expire(now: t0.addingTimeInterval(181))
        XCTAssertEqual(hub.compose(now: t0.addingTimeInterval(181)).agents.map(\.title).sorted(), ["Research", "Run tests"],
                       "done rows fade after 3 min; error rows stay")
        hub.expire(now: t0.addingTimeInterval(1801))
        XCTAssertFalse(hub.compose(now: t0.addingTimeInterval(1801)).agents.contains { $0.title == "Run tests" }, "error rows end at 30 min")
        _ = hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "grok", "id": "b", "kind": "clear"]), port: 8777)
        XCTAssertTrue(hub.compose().agents.isEmpty)
    }

    func testCursorRunningRowShowsMetaWithLiveAgeAndCursorIcon() throws {
        let hub = IslandHub(store: store)
        let cursor = try token(.cursor)
        let t0 = Date(timeIntervalSince1970: 20_000)
        let body: [String: Any] = ["source": "cursor", "id": "chat-1", "kind": "running", "title": "General chat",
                                   "app": "Cursor", "meta": "Grok 4.7 · high"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: body), port: 8777, now: t0).status, 200)
        let row = try XCTUnwrap(hub.compose(now: t0.addingTimeInterval(75)).agents.first)
        XCTAssertEqual(row.app, "Cursor")
        XCTAssertEqual(row.title, "General chat")
        XCTAssertEqual(IslandAgentRows.statusText(row), "Grok 4.7 · high · 1m")
    }

    func testPostedMetaRowAgesOnTheClock() throws {
        let hub = IslandHub(store: store)
        let cursor = try token(.cursor)
        let t0 = Date(timeIntervalSince1970: 20_000)
        var changes = 0
        hub.onSnapshot = { _ in changes += 1 }
        let body: [String: Any] = ["source": "cursor", "id": "chat-1", "kind": "running", "title": "General chat",
                                   "app": "Cursor", "meta": "Grok 4.7 · high"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: body), port: 8777, now: t0).status, 200)
        let before = changes
        hub.refreshLocalHarnesses(now: t0.addingTimeInterval(61))
        XCTAssertGreaterThan(changes, before, "the clock republishes a posted row when its age text moves")
        let after = changes
        hub.refreshLocalHarnesses(now: t0.addingTimeInterval(61.4))
        XCTAssertEqual(changes, after, "no change, no republish")
    }

    func testCursorDoneRowKeepsItsModelMeta() throws {
        let hub = IslandHub(store: store)
        let cursor = try token(.cursor)
        let t0 = Date(timeIntervalSince1970: 20_000)
        let body: [String: Any] = ["source": "cursor", "id": "chat-1", "kind": "done", "title": "General chat",
                                   "app": "Cursor", "meta": "Grok 4.7 · high · fast"]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: cursor, json: body), port: 8777, now: t0).status, 200)
        let row = try XCTUnwrap(hub.compose(now: t0.addingTimeInterval(65)).agents.first)
        XCTAssertEqual(IslandAgentRows.statusText(row), "Grok 4.7 · high · fast · done 1m")
    }

    func testSilentRunningAgentBecomesStaleAndCanBeDismissed() throws {
        let hub = IslandHub(store: store)
        let grok = try token(.grokBot)
        let t0 = Date(timeIntervalSince1970: 10_000)
        let body: [String: Any] = ["source": "grok", "id": "quiet", "kind": "running", "title": "Research",
                                   "ttl_s": 5, "controls": ["stop"]]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: grok, json: body), port: 8777, now: t0).status, 200)

        hub.expire(now: t0.addingTimeInterval(6))
        let stale = try XCTUnwrap(hub.compose(now: t0.addingTimeInterval(6)).agents.first)
        XCTAssertEqual(stale.status, "stale")
        XCTAssertEqual(stale.last_seen_at, t0.timeIntervalSince1970)
        XCTAssertEqual(stale.elapsed_s, 6)
        XCTAssertTrue(stale.controls.isEmpty)
        XCTAssertEqual(IslandAgentRows.control(for: stale), .dismiss)
        XCTAssertFalse(hub.requestControl(id: IslandHub.key(.grokBot, "quiet"), action: "stop"))

        XCTAssertTrue(hub.dismissStale(id: stale.id, now: t0.addingTimeInterval(6)))
        XCTAssertTrue(hub.compose(now: t0.addingTimeInterval(6)).agents.isEmpty)
    }

    func testRosterReviewSelectsChoiceDetailWithoutAnsweringIt() throws {
        store.set("attention", for: AgentSource.grokBot.keyPrefix + "cap")
        let grok = try token(.grokBot)
        let hub = IslandHub(store: store)
        let body: [String: Any] = ["source": "grok-bot", "id": "pick", "kind": "choice",
                                   "prompt": "Choose an option", "options": [["label": "Keep", "value": "keep"],
                                                                                 ["label": "Delete", "value": "delete"]]]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: grok, json: body), port: 8777).status, 200)
        let id = IslandHub.key(.grokBot, "pick")
        let row = try XCTUnwrap(hub.compose().agents.first { $0.id == "pending:" + id })
        XCTAssertEqual(IslandAgentRows.control(for: row), .review)

        let model = IslandModel()
        model.apply(hub.compose())
        model.onRowControl = { selected, action in
            guard action == .review, hub.selectApproval(id: String(selected.id.dropFirst("pending:".count))) else { return }
            model.apply(hub.compose())
            model.openApprovalFromRoster()
        }
        model.tapRowControl(row)
        XCTAssertTrue(model.cardOpen)
        XCTAssertEqual(model.agentApproval?.id, id)
        guard case .choice(let selected) = model.card.controls else { return XCTFail("choice detail should be shown") }
        XCTAssertEqual(selected.options.map(\.value), ["keep", "delete"])
        XCTAssertEqual(hub.decision(id), .pending, "opening the detail must not answer the request")
    }

    func testSamanthaSnapshotCannotImpersonateOthers() throws {
        let hub = IslandHub(store: store)
        let t = try token(.samantha)
        let snap: [String: Any] = ["source": "samantha", "kind": "listening", "title": "Listening",
                                   "agents": [["id": "x", "name": "Codex"]], "agent_approval": ["id": "evil", "title": "Pwn?"]]
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: snap), port: 8777).status, 409,
                       "not accepted while CUAService has no Samantha session (no resurrection after Stop)")
        hub.samanthaActive = true
        XCTAssertEqual(hub.handle(req("POST", "/api/island/publish", token: t, json: snap), port: 8777).status, 200)
        let c = hub.compose()
        XCTAssertEqual(c.kind, "listening")
        XCTAssertTrue(c.agents.isEmpty); XCTAssertNil(c.agent_approval)
    }

    func testEnsureTokenOnlyCreatesWhenMissing() {
        XCTAssertNil(mem.tokens["samantha"])
        store.ensureToken(.samantha)
        let first = mem.tokens["samantha"]
        XCTAssertNotNil(first)
        store.ensureToken(.samantha)
        XCTAssertEqual(mem.tokens["samantha"], first)
    }

    func testHTTPParser() {
        let raw = "POST /api/island/publish?x=1 HTTP/1.1\r\nHost: 127.0.0.1:8777\r\nContent-Length: 2\r\n\r\n{}"
        guard case .complete(let r) = IslandHubServer.parse(Data(raw.utf8)) else { return XCTFail() }
        XCTAssertEqual(r.path, "/api/island/publish"); XCTAssertEqual(r.query["x"], "1"); XCTAssertEqual(r.headers["host"], "127.0.0.1:8777")
        if case .complete = IslandHubServer.parse(Data("POST / HTTP/1.1\r\nContent-Length: 10\r\n\r\n{}".utf8)) { XCTFail("incomplete body") }
    }

    // MARK: - Controller: island alive with voice off, publish over real HTTP

    func testIslandAliveWithVoiceOffAndPublishOverHTTP() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: 0)
        c.start()
        defer { c.shutdown() }
        let port = try XCTUnwrap(c.boundHubPort)
        XCTAssertTrue(c.debugPanel.isShown, "panel up at launch with Samantha off")
        XCTAssertTrue(c.debugModel.isIdlePresentation)

        func post(_ body: [String: Any], token: String?) async throws -> Int {
            var r = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/island/publish")!)
            r.httpMethod = "POST"
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            if let token { r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (_, resp) = try await URLSession.shared.data(for: r)
            return (resp as? HTTPURLResponse)?.statusCode ?? -1
        }
        let body: [String: Any] = ["source": "grok-bot", "kind": "running", "title": "Booking table", "app": "Comet"]
        let unsigned = try await post(body, token: nil)
        XCTAssertEqual(unsigned, 401)
        let t = try token(.grokBot)
        let signed = try await post(body, token: t)
        XCTAssertEqual(signed, 200)
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(c.debugModel.agents.map(\.title), ["Booking table"], "published with voice off")
        XCTAssertTrue(c.debugModel.showAgentCircle)
    }

    func testIdleActivityNoneHidesUntilSomethingPosts() async throws {
        store.set("none", for: IslandSettingsSchema.idleActivity)
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        XCTAssertTrue(c.debugModel.idleHidden)
        let t = try token(.cursor)
        _ = c.hub.handle(req("POST", "/api/island/publish", token: t, json: ["source": "cursor", "id": "m", "kind": "approval", "title": "Run tests?"]), port: 8777)
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertFalse(c.debugModel.idleHidden, "an approval card always shows")
    }

    func testSamanthaStartStopKeepsTheSamePanel() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        let first = try XCTUnwrap(c.debugPanel.panelID)
        c.startStreaming()
        c.hub.ingestSamantha(IslandSnapshot(kind: "listening", title: "Listening"))
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(c.debugModel.presentation, .listening)
        c.stopStreaming()
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(c.debugPanel.panelID, first, "same panel instance")
        XCTAssertTrue(c.debugPanel.isShown, "island stays up after Samantha stops")
        XCTAssertTrue(c.debugModel.isIdlePresentation)
        c.startStreaming(); c.stopStreaming()
        XCTAssertEqual(c.debugPanel.panelID, first)
    }

    func testShowDynamicIslandToggle() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        XCTAssertTrue(c.debugPanel.isShown)
        store.set(false, for: IslandSettingsSchema.showIsland)
        XCTAssertFalse(c.debugPanel.isShown)
        store.set(true, for: IslandSettingsSchema.showIsland)
        XCTAssertTrue(c.debugPanel.isShown)
    }
}
