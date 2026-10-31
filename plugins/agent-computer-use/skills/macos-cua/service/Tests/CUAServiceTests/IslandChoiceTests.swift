import AppKit
import XCTest
@testable import CUAService

/// Choice cards, local-socket transport, long-poll, persistence and the key-window rule.
/// MemoryTokenStore + temp UserDefaults suite only — never the real Keychain.
@MainActor
final class IslandChoiceTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var mem: MemoryTokenStore!
    private var store: IslandSettingsStore!
    private var dir: URL!

    override func setUp() {
        suite = "cua.island.choice.test.\(UUID().uuidString)"
        defaults = TestDefaults.make(suite)
        mem = MemoryTokenStore()
        store = IslandSettingsStore(defaults: defaults, tokens: mem)
        // Short path (sun_path ≤ 104) under ~/.cache, never /tmp.
        dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/acu-ct-\(UUID().uuidString.prefix(6))")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: dir)
    }

    private func local(_ method: String, _ path: String, json: [String: Any]? = nil,
                       query: [String: String] = [:]) -> IslandHub.Request {
        var r = IslandHub.Request(method: method, path: path, query: query,
                                  body: json.map { try! JSONSerialization.data(withJSONObject: $0) } ?? Data())
        r.peerVerified = true
        return r
    }

    private func choiceBody(_ id: String = "q1", options: [[String: String]] = [["label": "Looks great", "value": "great"],
                                                                                 ["label": "Needs work", "value": "needs_work"]],
                            extra: [String: Any] = [:]) -> [String: Any] {
        ["source": "grok-bot", "id": id, "kind": "choice", "prompt": "Island test: which do you prefer?",
         "options": options, "allow_text": true].merging(extra) { $1 }
    }

    // MARK: Socket-peer auth (no Keychain read in the client)

    func testLocalPeerNeedsOptInButNoBearer() throws {
        let hub = IslandHub(store: store)
        let body: [String: Any] = ["source": "grok-bot", "kind": "running", "title": "Booking"]
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: body), port: 0).status, 401,
                       "source not set up (no Rotate yet)")
        try store.rotateToken(.grokBot)
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: body), port: 0).status, 200)
        store.set(false, for: AgentSource.grokBot.keyPrefix + "enabled")
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: body), port: 0).status, 403, "toggle still applies")
        // HTTP without peerVerified still needs the bearer.
        var http = IslandHub.Request(method: "POST", path: "/api/island/publish", headers: ["host": "127.0.0.1:8777"],
                                     body: try JSONSerialization.data(withJSONObject: body))
        http.peerVerified = false
        store.set(true, for: AgentSource.grokBot.keyPrefix + "enabled")
        XCTAssertEqual(hub.handle(http, port: 8777).status, 401)
    }

    func testDevFlagNeverBypassesSocketOptInAndChoiceStubRejected() throws {
        let hub = IslandHub(store: store)
        defaults.set(true, forKey: IslandHub.allowUnsignedKey)
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: choiceBody()), port: 0).status, 401,
                       "unsigned-dev flag is HTTP only")
        try store.rotateToken(.grokBot)
        _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody()), port: 0)
        XCTAssertEqual(hub.handle(local("POST", "/api/agent/approval", json: ["source": "grok-bot", "id": "q1", "approved": true]),
                                  port: 0).status, 400, "choice answered on the island only")
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish",
                                        json: choiceBody("t", extra: ["ttl_s": 120])), port: 0, now: Date(timeIntervalSince1970: 0)).status, 200)
        hub.expire(now: Date(timeIntervalSince1970: 100))
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "t")), .pending, "integer ttl_s honoured")
    }

    func testHubRefreshAndHoverAwayKeepAnOpenChoice() async throws {
        try store.rotateToken(.grokBot)
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        _ = c.hub.handle(local("POST", "/api/island/publish", json: choiceBody()), port: 0)
        c.debugModel.toggleCard()
        XCTAssertTrue(c.debugModel.cardOpen)
        _ = c.hub.handle(local("POST", "/api/island/publish", json: ["source": "grok-bot", "id": "row", "kind": "running", "title": "x"]), port: 0)
        _ = c.hub.handle(local("POST", "/api/island/publish", json: choiceBody("q2")), port: 0)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(c.debugModel.cardOpen, "hub refresh / queue growth keeps the open card")
        c.debugModel.pointerLeft()
        XCTAssertTrue(c.debugModel.cardOpen, "hover-away keeps an opened choice")
        c.debugModel.toggleCard()
        XCTAssertFalse(c.debugModel.cardOpen, "click closes it")
    }

    func testEscapeLeavesApprovalPending() throws {
        try store.rotateToken(.grokBot)
        let hub = IslandHub(store: store)
        hub.frontmostBundleID = { nil }
        let model = IslandModel()
        hub.onSnapshot = { model.apply($0) }
        var actions: [IslandCardAction] = []
        model.onAction = { actions.append($0) }
        let body: [String: Any] = ["source": "grok-bot", "id": "pending", "kind": "approval", "title": "Continue?"]
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: body), port: 0).status, 200)
        model.hotKeyPressed()
        XCTAssertTrue(model.cardOpen)
        XCTAssertTrue(model.handleKey(keyCode: 53, characters: "", shift: false))
        XCTAssertFalse(model.cardOpen)
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "pending")), .pending)
        XCTAssertNotNil(hub.compose().agent_approval)
        XCTAssertTrue(actions.isEmpty)
    }

    func testSocketParserMarksPeerAndHTTPParserNever() {
        let line = #"{"method":"get","path":"/api/agent/approval","query":{"source":"grok-bot","id":"q1","wait":5}}"#
        let r = IslandHubSocketServer.parse(Data(line.utf8))
        XCTAssertEqual(r?.method, "GET"); XCTAssertEqual(r?.query["wait"], "5"); XCTAssertEqual(r?.peerVerified, true)
        let raw = "POST /api/island/publish HTTP/1.1\r\nHost: 127.0.0.1:8777\r\nContent-Length: 2\r\n\r\n{}"
        guard case .complete(let h) = IslandHubServer.parse(Data(raw.utf8)) else { return XCTFail() }
        XCTAssertFalse(h.peerVerified, "HTTP can never claim local-peer trust")
        XCTAssertNil(IslandHubSocketServer.parse(Data("nope".utf8)))
    }

    func testRealSocketRoundTripIs0600AndAnswersChoice() async throws {
        try store.rotateToken(.grokBot)
        let path = dir.appendingPathComponent("i.sock").path
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil, socketPath: path)
        c.hub.frontmostBundleID = { nil }
        c.frontmostAppName = { nil }
        c.start()
        defer { c.shutdown() }
        let attrs = try FileManager.default.attributesOfItem(atPath: path)
        XCTAssertEqual((attrs[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        let r1 = try await Self.sock(path, ["method": "POST", "path": "/api/island/publish", "body": choiceBody()])
        XCTAssertEqual(r1["status"] as? Int, 200)
        // A client that writes after connecting late must not get 400 (accepted fd was non-blocking).
        let slow = try await Self.sock(path, ["method": "GET", "path": "/api/agent/approval",
                                              "query": ["source": "grok-bot", "id": "q1"]], delay: 200_000)
        XCTAssertEqual(slow["status"] as? Int, 200)
        XCTAssertEqual(c.debugModel.agentApproval?.isChoice, true)
        XCTAssertTrue(c.debugModel.cardOpen, "choice arrival = 4 s Quick Peek")
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "a peek never takes key")
        XCTAssertEqual(c.debugModel.presentation, .agentApproval)
        // Long-poll parks, the island answer wakes it.
        async let waited = Self.sock(path, ["method": "GET", "path": "/api/agent/approval",
                                            "query": ["source": "grok-bot", "id": "q1", "wait": "30"]])
        try await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertEqual(c.hub.waiterCount, 1)
        c.hub.answerChoice(id: IslandHub.key(.grokBot, "q1"), values: ["great"], text: "  nice  ")
        let r2 = try await waited
        let body = try XCTUnwrap(r2["body"] as? [String: Any])
        XCTAssertEqual(body["status"] as? String, "answered")
        XCTAssertEqual(body["values"] as? [String], ["great"])
        XCTAssertEqual(body["text"] as? String, "  nice  ")
        XCTAssertNil(c.hub.compose().agent_approval, "answered card leaves the island at once")
        XCTAssertEqual(c.hub.waiterCount, 0)
    }

    nonisolated static func sock(_ path: String, _ req: [String: Any], delay: useconds_t = 0) async throws -> [String: Any] {
        let data = try JSONSerialization.data(withJSONObject: req) + Data([0x0A])
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global().async {
                let s = socket(AF_UNIX, SOCK_STREAM, 0)
                var addr = sockaddr_un()
                addr.sun_family = sa_family_t(AF_UNIX)
                let b = Array(path.utf8)
                withUnsafeMutableBytes(of: &addr.sun_path) { raw in for (i, x) in b.enumerated() { raw[i] = x }; raw[b.count] = 0 }
                let rc = withUnsafePointer(to: &addr) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(s, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
                }
                guard rc == 0 else { close(s); cont.resume(throwing: NSError(domain: "sock", code: Int(errno))); return }
                if delay > 0 { usleep(delay) }
                data.withUnsafeBytes { _ = write(s, $0.baseAddress!, $0.count) }
                var out = Data(); var buf = [UInt8](repeating: 0, count: 4096)
                while true { let n = read(s, &buf, buf.count); if n <= 0 { break }; out.append(contentsOf: buf[0..<n]); if out.last == 0x0A { break } }
                close(s)
                let obj = (try? JSONSerialization.jsonObject(with: out)) as? [String: Any] ?? [:]
                cont.resume(returning: obj)
            }
        }
    }

    // MARK: Hub semantics

    func testChoiceValidationAndAnswers() throws {
        try store.rotateToken(.grokBot)
        let hub = IslandHub(store: store)
        let seven = (1...7).map { ["label": "o\($0)", "value": "v\($0)"] }
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: choiceBody(options: seven)), port: 0).status, 400, "max 6")
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish",
                                        json: choiceBody(options: [], extra: ["allow_text": false])), port: 0).status, 400)
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: choiceBody()), port: 0).status, 200)
        let id = IslandHub.key(.grokBot, "q1")
        hub.answerChoice(id: id, values: ["bogus"], text: nil)
        XCTAssertEqual(hub.decision(id), .pending, "values outside the options are ignored")
        hub.answerChoice(id: id, values: ["needs_work", "great"], text: nil)
        XCTAssertEqual(hub.answer(id)?.values, ["needs_work"], "single-select keeps one")
        // Dismiss + clear.
        _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody("q2")), port: 0)
        hub.dismissChoice(id: IslandHub.key(.grokBot, "q2"))
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "q2")), .dismissed)
        _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody("q3")), port: 0)
        _ = hub.handle(local("POST", "/api/island/publish", json: ["source": "grok-bot", "id": "q3", "kind": "clear"]), port: 0)
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "q3")), .dismissed, "answered in chat → clear wakes the waiter")
        XCTAssertNil(hub.compose().agent_approval)
        // Approval still maps to approved/denied.
        _ = hub.handle(local("POST", "/api/island/publish", json: ["source": "grok-bot", "id": "a1", "kind": "approval", "title": "Ok?"]), port: 0)
        hub.resolveApproval(id: IslandHub.key(.grokBot, "a1"), approved: false)
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "a1")), .denied)
    }

    func testPendingChoiceRowOpensItsOptionsWithoutAnswering() throws {
        try store.rotateToken(.grokBot)
        let hub = IslandHub(store: store)
        XCTAssertEqual(hub.handle(local("POST", "/api/island/publish", json: choiceBody("roster-choice")), port: 0).status, 200)
        let id = IslandHub.key(.grokBot, "roster-choice")
        let row = try XCTUnwrap(hub.compose().agents.first { $0.id == "pending:" + id })
        XCTAssertEqual(row.request_kind, "choice")
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
        guard case .choice(let details) = model.card.controls else { return XCTFail("choice details should open") }
        XCTAssertEqual(details.options.map(\.value), ["great", "needs_work"])
        XCTAssertEqual(hub.decision(id), .pending)
    }

    func testQueueOrderCountAndExpiry() throws {
        try store.rotateToken(.grokBot)
        let hub = IslandHub(store: store)
        let t0 = Date()
        for id in ["a", "b", "c"] {
            _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody(id, extra: ["ttl_s": 60])), port: 0, now: t0)
        }
        var s = hub.compose(now: t0)
        XCTAssertEqual(s.agent_approval?.id, IslandHub.key(.grokBot, "a"), "arrival order")
        XCTAssertEqual(s.pending_count, 3)
        XCTAssertEqual(s.agents.filter(\.needs_you).count, 3, "agent circle carries the queue")
        hub.answerChoice(id: IslandHub.key(.grokBot, "a"), values: ["great"], text: nil)
        s = hub.compose(now: t0)
        XCTAssertEqual(s.agent_approval?.id, IslandHub.key(.grokBot, "b"))
        hub.expire(now: t0.addingTimeInterval(61))
        XCTAssertEqual(hub.compose().pending_count, 0)
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "c")), .expired)
    }

    func testPendingCardsSurviveRestartAt0600() throws {
        try store.rotateToken(.grokBot)
        let url = dir.appendingPathComponent("pending.json")
        let hub = IslandHub(store: store)
        hub.persistURL = url
        _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody("p1")), port: 0)
        _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody("p2", extra: ["multi": true])), port: 0)
        hub.answerChoice(id: IslandHub.key(.grokBot, "p1"), values: ["great"], text: nil)
        let perms = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.intValue, 0o600)
        let again = IslandHub(store: store)
        again.persistURL = url
        XCTAssertEqual(again.compose().agent_approval?.id, IslandHub.key(.grokBot, "p2"))
        XCTAssertEqual(again.compose().agent_approval?.multi, true)
        XCTAssertEqual(again.answer(IslandHub.key(.grokBot, "p1"))?.values, ["great"], "answer readable after restart")
    }

    func testLongPollTimesOutCheaply() async throws {
        try store.rotateToken(.grokBot)
        let hub = IslandHub(store: store)
        _ = hub.handle(local("POST", "/api/island/publish", json: choiceBody()), port: 0)
        let start = Date()
        let r = await hub.handleAsync(local("GET", "/api/agent/approval", query: ["source": "grok-bot", "id": "q1", "wait": "0.3"]), port: 0)
        XCTAssertEqual(r.body["status"], "pending")
        XCTAssertGreaterThanOrEqual(Date().timeIntervalSince(start), 0.25)
        XCTAssertEqual(hub.waiterCount, 0)
    }

    // MARK: Model + key window

    func testChoiceLayoutTiers() {
        var a = IslandAgentApproval(id: "x", kind: "choice", options: [.init(label: "A", value: "a"), .init(label: "B", value: "b")])
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a), 176, "one row of options")
        a.allow_text = true
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a), 220, "two 44-point rows fit the tall card")
        a.options += [.init(label: "C", value: "c")]
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a), 244, "three 44-point rows fit without overlap")
        a.options = (1...6).map { .init(label: "\($0)", value: "\($0)") }
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a), 244)
        XCTAssertEqual(IslandChoiceLayout.shownOptions(a).count, 3, "> 4 options: 3 + More in chat")
        XCTAssertTrue(IslandChoiceLayout.hasMore(a))
        XCTAssertLessThanOrEqual(IslandChoiceLayout.contentHeight(a), IslandChoiceLayout.cardHeight(a), "never scrolls")
    }

    func testPanelNeverBecomesKeyWithoutAClick() async throws {
        store.set(false, for: IslandSettingsSchema.hideInFullscreen)
        try store.rotateToken(.grokBot)
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        let m = c.debugModel
        c.debugPanel.debugEnsurePanel()
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "idle")
        _ = c.hub.handle(local("POST", "/api/island/publish", json: choiceBody()), port: 0)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "a new choice card never takes key")
        m.hoverOpen()
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.cardHeight, 220)
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "opening the card never takes key")
        XCTAssertFalse(c.debugPanel.debugIsKey)
        m.beginTextEntry()   // == the click into the field
        XCTAssertTrue(c.debugPanel.debugCanBecomeKey)
        m.pointerLeft()
        XCTAssertTrue(m.cardOpen, "typing keeps the card open")
        m.endTextEntry()     // submit / Escape
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "key given back")
        m.beginTextEntry()
        c.hub.answerChoice(id: IslandHub.key(.grokBot, "q1"), values: ["great"], text: nil)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "answering ends text entry")
        XCTAssertFalse(m.cardOpen)
        XCTAssertNil(m.agentApproval)
    }

    func testBeginTextEntryRequiresOpenTextCard() async throws {
        store.set(false, for: IslandSettingsSchema.hideInFullscreen)
        try store.rotateToken(.grokBot)
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        c.debugPanel.debugEnsurePanel()
        c.debugModel.beginTextEntry()
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "no card → no key")
        _ = c.hub.handle(local("POST", "/api/island/publish", json: choiceBody(extra: ["allow_text": false])), port: 0)
        for _ in 0..<100 where c.debugModel.agentApproval == nil {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertNotNil(c.debugModel.agentApproval, "published card arrived before interaction")
        c.debugModel.hoverOpen()
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "hover-open never takes key")
        c.debugModel.beginTextEntry()
        XCTAssertTrue(c.debugPanel.debugCanBecomeKey, "a click may take key for keys 1–6 even without a field")
        XCTAssertFalse(c.debugModel.fieldFocusRequested, "no field to focus")
    }
}
