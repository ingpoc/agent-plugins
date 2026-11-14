import XCTest
@testable import CUAService

final class IslandGrokBotActivityTests: XCTestCase {
    @MainActor private var rosterSnapshots: [[String: Any]] = []
    func testOnlyRowsLabelledWorkingCount() {
        let labels = ["Grok Bot · Main contact, Main Bot", "Agent Computer Use · Macos computer use, Working",
                      "UI & UX agent", "Hisab Pune Bot, Working", "Hisab Pune Bot, Working", "Working", ", Working",
                      "Research Agent, Needs attention"]
        XCTAssertEqual(IslandGrokBotActivity.workingBots(labels: labels),
                       ["Agent Computer Use · Macos computer use", "Hisab Pune Bot"])
    }

    func testScreenReaderStatusCountsOnlyForASidebarBot() {
        let labels = ["Grok Bot · Main contact, Main Bot", "Agent Computer Use · Macos computer use", "Finance"]
        XCTAssertEqual(IslandGrokBotActivity.workingBots(labels: labels,
                       statuses: ["Agent Computer Use · Macos computer use is working", "Somebody else is working"]),
                       ["Agent Computer Use · Macos computer use"])
        XCTAssertEqual(IslandGrokBotActivity.workingBots(labels: labels, statuses: ["Finance replied"]), [])
        XCTAssertEqual(IslandGrokBotActivity.workingBots(labels: labels + ["Finance, Working"],
                       statuses: ["Finance is working"]), ["Finance"])
    }

    func testRowShowsBotNameAndGrokBotMeta() {
        let now = Date()
        let rows = IslandGrokBotActivity.rows([("Agent Computer Use · Macos computer use", now.addingTimeInterval(-185))], now: now)
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].title, "Agent Computer Use")
        XCTAssertEqual(rows[0].name, "Grok Bot")
        XCTAssertEqual(rows[0].app, "Grok Bot")
        XCTAssertEqual(rows[0].status, "running")
        XCTAssertEqual(rows[0].local_detail, "Grok Bot · 3m")
        XCTAssertEqual(rows[0].tooltip, "Agent Computer Use · Macos computer use")
    }

    @MainActor
    func testHubShowsWorkingBotsAndDropsThemWhenTheTurnEnds() {
        let suite = "cua.island.grokbot.test.\(UUID().uuidString)"
        let defaults: UserDefaults = TestDefaults.make(suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let hub = IslandHub(store: IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore()))
        let start = Date()
        hub.updateGrokBotWorking(["Agent Computer Use · Macos computer use"], now: start)
        var rows = hub.compose(now: start.addingTimeInterval(120)).agents.filter { $0.app == "Grok Bot" }
        XCTAssertEqual(rows.map { $0.title }, ["Agent Computer Use"])
        XCTAssertEqual(rows.first?.local_detail, "Grok Bot · 2m")
        // Still working on the next AX event: keeps its start time.
        hub.updateGrokBotWorking(["Agent Computer Use · Macos computer use", "Finance"], now: start.addingTimeInterval(60))
        rows = hub.compose(now: start.addingTimeInterval(180)).agents.filter { $0.app == "Grok Bot" }
        XCTAssertEqual(rows.map { $0.local_detail }, ["Grok Bot · 3m", "Grok Bot · 2m"])
        hub.updateGrokBotWorking([], now: start.addingTimeInterval(200))
        XCTAssertTrue(hub.compose(now: start.addingTimeInterval(200)).agents.filter { $0.app == "Grok Bot" }.isEmpty)
    }

    @MainActor
    func testFreshHeartbeatsMergeWithAXAndDropWhenEndedOrStale() throws {
        let suite = "cua.island.grokbot.heartbeat.\\(UUID().uuidString)"
        let defaults: UserDefaults = TestDefaults.make(suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        try store.rotateToken(.grokBot)
        store.set(true, for: AgentSource.grokBot.keyPrefix + "enabled")
        store.set(true, for: AgentSource.agentRoster.keyPrefix + "enabled")
        let hub = IslandHub(store: store)
        rosterSnapshots = []
        IslandEventLog.sink = { [weak self] event in
            MainActor.assumeIsolated { self?.rosterSnapshots.append(event) }
        }
        defer { IslandEventLog.sink = nil }

        func post(_ id: String, _ kind: String, at now: Date) {
            let body: [String: Any] = ["source": "grok-bot", "id": id, "kind": kind,
                                       "title": id, "controls": ["stop"]]
            var request = IslandHub.Request(method: "POST", path: "/api/island/publish",
                                            body: try! JSONSerialization.data(withJSONObject: body))
            request.peerVerified = true
            XCTAssertEqual(hub.handle(request, port: 0, now: now).status, 200)
        }
        func grokRows(at now: Date) -> [IslandAgent] {
            hub.compose(now: now).agents.filter { $0.app == "Grok Bot" }
        }

        let start = Date()
        _ = hub.compose(now: start)
        hub.updateGrokBotWorking(["Agent Computer Use · Macos computer use"], now: start)
        post("job-a", "running", at: start)
        post("job-b", "running", at: start.addingTimeInterval(30))
        var rows = grokRows(at: start.addingTimeInterval(30))
        XCTAssertEqual(Set(rows.map(\.id)), Set(["local:grok-bot:Agent Computer Use · Macos computer use", "grok:job-a", "grok:job-b"]))
        XCTAssertEqual(rows.first { $0.id == "grok:job-a" }?.controls, ["stop"])

        post("job-a", "clear", at: start.addingTimeInterval(40))
        rows = grokRows(at: start.addingTimeInterval(40))
        XCTAssertEqual(Set(rows.map(\.id)), Set(["local:grok-bot:Agent Computer Use · Macos computer use", "grok:job-b"]))
        hub.updateGrokBotWorking([], now: start.addingTimeInterval(629))
        XCTAssertEqual(grokRows(at: start.addingTimeInterval(629)).map(\.id), ["grok:job-b"])
        hub.expire(now: start.addingTimeInterval(630))
        let expired = grokRows(at: start.addingTimeInterval(630))
        XCTAssertEqual(expired.map(\.id), ["grok:job-b"], "a silent heartbeat is retained as stale at its TTL")
        XCTAssertEqual(expired.first?.status, "stale")
        XCTAssertTrue(hub.dismissStale(id: expired[0].id, now: start.addingTimeInterval(630)))
        XCTAssertTrue(grokRows(at: start.addingTimeInterval(630)).isEmpty)
        post("job-b", "clear", at: start.addingTimeInterval(631))

        post("job-c", "running", at: start.addingTimeInterval(640))
        XCTAssertEqual(grokRows(at: start.addingTimeInterval(640)).map(\.id), ["grok:job-c"])
        post("job-c", "clear", at: start.addingTimeInterval(641))
        XCTAssertTrue(grokRows(at: start.addingTimeInterval(641)).isEmpty)

        let snapshots = rosterSnapshots.filter { $0["event"] as? String == "roster-snapshot" }
        XCTAssertTrue(snapshots.contains { $0["status"] as? String == "3 active" })
        XCTAssertEqual(snapshots.last?["state"] as? String, "idle")
        XCTAssertEqual(snapshots.last?["status"] as? String, "0 active")
    }
}
