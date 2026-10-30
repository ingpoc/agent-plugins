import XCTest
@testable import CUAService

final class IslandGrokBotActivityTests: XCTestCase {
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
}
