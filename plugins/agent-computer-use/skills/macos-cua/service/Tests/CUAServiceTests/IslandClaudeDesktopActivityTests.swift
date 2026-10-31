import XCTest
@testable import CUAService

final class IslandClaudeDesktopActivityTests: XCTestCase {
    typealias Activity = IslandClaudeDesktopActivity

    func testStatusLabels() {
        for (word, status) in [("Working", Activity.Status.working), ("Needs input", .needsYou),
            ("Ready", .done), ("Completed", .done), ("Done", .done), ("Idle", .idle), ("Something went wrong", .error)] {
            for prefix in ["", "Mark as unread "] {
                let result = Activity.parseClaudeRow(prefix + word + " Trip plan")
                XCTAssertEqual(result?.status, status)
                XCTAssertEqual(result?.title, "Trip plan")
            }
        }
    }

    func testUnrecognisedAndEmptyTitlesAreIgnored() {
        for label in ["", "Working", "Working ", "Workingish Trip", "Needs input", "Trip plan", "Model: Sonnet 5.5 Medium"] {
            XCTAssertNil(Activity.parseClaudeRow(label), label)
        }
    }

    func testModelEfforts() {
        for effort in ["Low", "Medium", "High", "Extra high", "Max"] {
            let parsed = Activity.parseModel("Model: Sonnet 5.5 " + effort)
            XCTAssertEqual(parsed?.model, "Sonnet 5.5")
            XCTAssertEqual(parsed?.effort, effort.lowercased())
        }
        XCTAssertEqual(Activity.parseModel("Model: Opus 5")?.model, "Opus 5")
        XCTAssertNil(Activity.parseModel("Model: "))
        XCTAssertNil(Activity.parseModel("Sonnet 5.5 Medium"))
    }

    func testOnlySelectedSoleWorkingRowGetsModel() {
        var snapshot = Activity.Snapshot(rows: [.init(status: .working, title: "First", selected: true)],
                                         modelLabel: "Model: Sonnet 5.5 Medium")
        XCTAssertEqual(snapshot.agents.first?.local_detail, "Sonnet 5.5 · medium")
        snapshot.rows[0].selected = false
        XCTAssertNil(snapshot.agents.first?.local_detail)
        snapshot.rows[0].selected = true
        snapshot.rows.append(.init(status: .working, title: "Second"))
        XCTAssertTrue(snapshot.agents.allSatisfy { $0.local_detail == nil })
        snapshot.rows = [.init(status: .needsYou, title: "First", selected: true)]
        XCTAssertTrue(snapshot.agents[0].needs_you)
        XCTAssertNil(snapshot.agents[0].local_detail)
    }

    @MainActor
    func testOnlyActiveRowsAndStableTitleHash() {
        let active = Activity.Row(status: .working, title: "First")
        let finished = Activity.Row(status: .done, title: "First")
        XCTAssertEqual(active.id, finished.id)
        XCTAssertTrue(active.id.hasPrefix("claude-desktop:"))
        XCTAssertNotEqual(active.id, Activity.Row(status: .working, title: "Second").id)
        let snapshot = Activity.Snapshot(rows: [active, .init(status: .needsYou, title: "Second"),
            .init(status: .done, title: "Old"), .init(status: .idle, title: "Idle"), .init(status: .error, title: "Error")])
        XCTAssertEqual(snapshot.agents.map(\.title), ["First", "Second"])
        XCTAssertEqual(snapshot.agents.map(\.status), ["running", "needs_you"])
        XCTAssertTrue(snapshot.agents.allSatisfy { $0.name == "Claude" && $0.app == "Claude" })
        XCTAssertEqual(AppIconResolver.bundleIDs["Claude"], Activity.bundleID)
    }

    @MainActor
    func testPeeksRequireObservedActiveTransitionAndRespectSettings() {
        let suite = "cua.island.claude.test.\(UUID().uuidString)"
        let defaults = TestDefaults.make(suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        store.set("critical", for: AgentSource.claude.keyPrefix + "cap")
        let hub = IslandHub(store: store)
        var peeks: [IslandPeekItem] = []
        hub.onPeek = { peeks.append($0) }
        hub.updateClaudeDesktop(.init(rows: [.init(status: .error, title: "Old error"), .init(status: .idle, title: "Idle")]))
        XCTAssertTrue(peeks.isEmpty)
        hub.updateClaudeDesktop(.init(rows: [.init(status: .working, title: "First")]))
        XCTAssertEqual(hub.compose().agents.filter { $0.app == "Claude" }.map(\.title), ["First"])
        hub.updateClaudeDesktop(.init(rows: [.init(status: .done, title: "First")]))
        XCTAssertEqual(peeks.count, 1)
        XCTAssertTrue(hub.compose().agents.filter { $0.app == "Claude" }.isEmpty)
        hub.updateClaudeDesktop(.init(rows: [.init(status: .done, title: "First")]))
        XCTAssertEqual(peeks.count, 1)
        hub.updateClaudeDesktop(.init(rows: [.init(status: .needsYou, title: "Second")]))
        hub.updateClaudeDesktop(.init(rows: [.init(status: .error, title: "Second")]))
        XCTAssertEqual(peeks.count, 2)
        hub.updateClaudeDesktop(.init(rows: [.init(status: .working, title: "Third")]))
        store.set(false, for: AgentSource.claude.keyPrefix + "enabled")
        XCTAssertTrue(hub.compose().agents.filter { $0.app == "Claude" }.isEmpty)
        hub.updateClaudeDesktop(.init(rows: [.init(status: .error, title: "Third")]))
        XCTAssertEqual(peeks.count, 2)
    }

    @MainActor
    func testSourceDefaultsAndOldSettingsMigration() {
        let suite = "cua.island.claude.migration.\(UUID().uuidString)"
        let defaults = TestDefaults.make(suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set(false, forKey: AgentSource.cursor.keyPrefix + "enabled")
        let store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        XCTAssertTrue(store.sourceEnabled(.claude))
        XCTAssertFalse(store.sourceEnabled(.cursor))
        XCTAssertEqual(store.cap(.claude), store.cap(.codex))
        XCTAssertEqual(AgentSource.match("Claude Code"), .claude)
        XCTAssertEqual(AgentSource.parse("claude"), .claude)
        XCTAssertEqual(AgentSource.claude.cliName, "claude")
        XCTAssertEqual(AgentSource.claude.defaultApp, "Claude")
        XCTAssertEqual(IslandSettingsSchema.tableSources, [.samantha, .grokBot, .codex, .claude, .cursor], "Agent Roster has no row in the table")
    }

    func testInitiatorBotChain() {
        let parsed = IslandInitiator.fromPost("acu_exec.grokbot.cursor")
        XCTAssertEqual(parsed?.agent, "ACU")
        XCTAssertEqual(parsed?.chain, ["Grok Bot", "Cursor"])
        XCTAssertNil(IslandInitiator.fromPost(nil))
        XCTAssertEqual(IslandInitiator.fromPost("Grok Bot")?.agent, "Grok Bot")
    }
}
