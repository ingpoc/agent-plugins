import XCTest
@testable import CUAService

final class IslandClaudeDesktopActivityTests: XCTestCase {
    typealias Activity = IslandClaudeDesktopActivity

    private final class PoisonConversation: Activity.AXElement {
        var role: String { "AXArticle" }
        var subrole: String? { XCTFail("must not read conversation subrole"); return nil }
        var roleDescription: String? { XCTFail("must not read conversation role description"); return nil }
        func labels() -> [String] { XCTFail("must not read conversation text"); return [] }
        func isSelected() -> Bool { XCTFail("must not inspect conversation selection"); return false }
        func childElements(limit: Int) -> [any Activity.AXElement] { XCTFail("must not traverse conversation subtree"); return [] }
    }

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

    func testNewSidebarRowsAndSafeTraversal() {
        let idle = Activity.Element(role: "AXButton", title: "Idle Notes", description: "Mark as unread Idle Notes")
        let work = Activity.Element(role: "AXButton", title: "Working Research", description: "Mark as unread Working Research", selected: true)
        let error = Activity.Element(role: "AXButton", title: "Something went wrong Retry", description: "Something went wrong Retry")
        let root = Activity.Element(role: "AXApplication", children: [
            Activity.Element(role: "AXGroup", roleDescription: "Sidebar", children: [idle, work, error]),
            Activity.Element(role: "AXPopUpButton", title: "Model: Sonnet 5.5 Medium"),
            Activity.Element(role: "AXButton", title: "Stop response"),
            PoisonConversation(),
            Activity.Element(role: "AXTextArea", children: [Activity.Element(role: "AXButton", title: "Working Secret editor")])
        ])
        let result = Activity.read(root)
        XCTAssertEqual(Set(result.rows.map(\.title)), ["Notes", "Research", "Retry"])
        XCTAssertTrue(result.stopButtonVisible)
        XCTAssertEqual(result.modelLabel, "Model: Sonnet 5.5 Medium")
    }

    func testMoreOptionsSiblingAndChatStopSignal() {
        let root = Activity.Element(role: "AXApplication", children: [
            Activity.Element(role: "AXGroup", children: [
                Activity.Element(role: "AXButton", title: "Working Draft"),
                Activity.Element(role: "AXButton", title: "More options for Draft")
            ]),
            Activity.Element(role: "AXButton", description: "Stop")
        ])
        let result = Activity.read(root)
        XCTAssertEqual(Set(result.rows.map(\.title)), ["Draft"])
        XCTAssertTrue(result.stopButtonVisible)
        XCTAssertFalse(Activity.read(Activity.Element(role: "AXApplication")).stopButtonVisible)
    }

    /// Shaped like the live Claude 4.4212 tree (synthetic titles): a group titled "Sidebar" with no
    /// sidebar subrole, rows nested in unlabeled groups, "More options" popups in a different parent,
    /// a toolbar duplicate, and "Mark as unread <title>" rows without a status word.
    func testLive4212TreeShape() {
        typealias E = Activity.Element
        func nest(_ e: any Activity.AXElement, _ n: Int) -> any Activity.AXElement {
            var cur = e
            for _ in 0..<n { cur = E(role: "AXGroup", children: [cur]) }
            return cur
        }
        let sidebar = E(role: "AXGroup", title: "Sidebar", children: [
            E(role: "AXRadioGroup", title: "Mode", children: [
                E(role: "AXRadioButton", title: "Chat and Cowork", selected: true),
                E(role: "AXRadioButton", title: "Code")
            ]),
            nest(E(role: "AXButton", title: "Working Alpha notes", selected: true), 5),
            nest(E(role: "AXButton", title: "Idle Beta plan"), 5),
            nest(E(role: "AXButton", title: "Mark as unread Gamma review"), 5),
            nest(E(role: "AXButton", title: "Something went wrong Delta run"), 5),
            nest(E(role: "AXPopUpButton", title: "More options for Alpha notes"), 6),
            nest(E(role: "AXButton", title: "Mark as unread"), 6)
        ])
        let root = E(role: "AXApplication", children: [E(role: "AXWindow", children: [nest(sidebar, 8),
            nest(E(role: "AXToolbar", title: "Page tools", children: [E(role: "AXPopUpButton", title: "More options for Alpha notes")]), 8),
            nest(E(role: "AXButton", title: "Stop response"), 10)])])
        let result = Activity.read(root)
        XCTAssertEqual(result.rows.count, 4)
        XCTAssertEqual(Dictionary(uniqueKeysWithValues: result.rows.map { ($0.title, $0.status) }),
                       ["Alpha notes": .working, "Beta plan": .idle, "Gamma review": .done, "Delta run": .error])
        XCTAssertTrue(result.chatTabActive)
        XCTAssertTrue(result.stopButtonVisible)
        XCTAssertEqual(result.agents.map(\.title), ["Alpha notes"])

        // Same rows outside any titled sidebar are still accepted via a matching popup anywhere in the window.
        let loose = E(role: "AXApplication", children: [
            nest(E(role: "AXButton", title: "Working Epsilon"), 4),
            nest(E(role: "AXPopUpButton", title: "More options for Epsilon"), 7),
            nest(E(role: "AXButton", title: "Working Unrelated"), 4)
        ])
        XCTAssertEqual(Activity.read(loose).rows.map(\.title), ["Epsilon"])

        // Working -> gone lingers as done for 10 s.
        var finished: [String: Date] = [:]
        let t0 = Date(timeIntervalSince1970: 1000)
        let held = Activity.retainFinishedRows([], previous: result.rows, finishedAt: &finished, now: t0)
        XCTAssertEqual(held.first { $0.title == "Alpha notes" }?.status, .done)
        XCTAssertTrue(Activity.retainFinishedRows([], previous: held, finishedAt: &finished, now: t0.addingTimeInterval(10)).allSatisfy { !$0.completionHeld })
    }

    func testMarkAsUnreadWithoutStatusIsDone() {
        XCTAssertEqual(Activity.parseClaudeRow("Mark as unread Organize files")?.status, .done)
        XCTAssertEqual(Activity.parseClaudeRow("Mark as unread Organize files")?.title, "Organize files")
        XCTAssertNil(Activity.parseClaudeRow("Mark as unread"))
        XCTAssertNil(Activity.parseClaudeRow("Mark as unread "))
    }

    func testFinishedWorkingRowsExpireAfterTenSecondsIncludingDisappearance() {
        var finished: [String: Date] = [:]
        let working = [Activity.Row(status: .working, title: "Task")]
        XCTAssertEqual(Activity.retainFinishedRows([], previous: working, finishedAt: &finished, now: Date(timeIntervalSince1970: 100)).first?.status, .done)
        XCTAssertEqual(Activity.retainFinishedRows([], previous: working, finishedAt: &finished, now: Date(timeIntervalSince1970: 109)).count, 1)
        XCTAssertEqual(Activity.retainFinishedRows([], previous: [], finishedAt: &finished, now: Date(timeIntervalSince1970: 110)).count, 0)

        let chatWorking = Activity.read(Activity.Element(role: "AXApplication", children: [
            Activity.Element(role: "AXButton", title: "Stop response")
        ])).rows
        var chatFinished: [String: Date] = [:]
        let chatDone = Activity.retainFinishedRows([], previous: chatWorking, finishedAt: &chatFinished,
                                                    now: Date(timeIntervalSince1970: 200))
        XCTAssertEqual(chatDone.first?.title, "Current response")
        XCTAssertEqual(chatDone.first?.status, .done)
        XCTAssertEqual(Activity.Snapshot(rows: chatDone).agents.first?.status, "done")

        var resumedFinished = ["Task": Date(timeIntervalSince1970: 300)]
        let resumed = Activity.retainFinishedRows(working, previous: [Activity.Row(status: .done, title: "Task", completionHeld: true)],
                                                   finishedAt: &resumedFinished, now: Date(timeIntervalSince1970: 305))
        XCTAssertEqual(resumed.first?.status, .working)
        XCTAssertFalse(resumed.first?.completionHeld ?? true)
        XCTAssertTrue(resumedFinished.isEmpty)
    }

    func testWorkingToIdleRetainsDoneAndStopDoesNotDuplicateWorkingRow() {
        let start = Date(timeIntervalSince1970: 100)
        let working = [Activity.Row(status: .working, title: "Task")]
        let idle = [Activity.Row(status: .idle, title: "Task")]
        var deadlines: [String: Date] = [:]
        let done = Activity.retainFinishedRows(idle, previous: working, finishedAt: &deadlines, now: start)
        XCTAssertEqual(Activity.Snapshot(rows: done).agents.first?.status, "done")
        let stillDone = Activity.retainFinishedRows(idle, previous: done, finishedAt: &deadlines, now: start.addingTimeInterval(9))
        XCTAssertEqual(Activity.Snapshot(rows: stillDone).agents.first?.status, "done")
        let expired = Activity.retainFinishedRows(idle, previous: stillDone, finishedAt: &deadlines, now: start.addingTimeInterval(10))
        XCTAssertTrue(Activity.Snapshot(rows: expired).agents.isEmpty)
        let root = Activity.Element(role: "AXApplication", children: [
            Activity.Element(role: "AXGroup", subrole: "AXSidePanel", children: [Activity.Element(role: "AXButton", title: "Working Task")]),
            Activity.Element(role: "AXButton", title: "Stop response")
        ])
        let read = Activity.read(root)
        XCTAssertTrue(read.stopButtonVisible)
        XCTAssertEqual(read.agents.count, 1)
        XCTAssertEqual(read.agents.first?.title, "Task")
    }
    func testRowCountLoggingIsBoundedAndWaitsForWindows() {
        var state = Activity.ReadLogState()
        XCTAssertFalse(state.shouldLog(rowCount: 0, hasWindows: false))
        XCTAssertFalse(state.shouldLog(rowCount: 0, hasWindows: false))
        XCTAssertTrue(state.shouldLog(rowCount: 0, hasWindows: true))
        XCTAssertFalse(state.shouldLog(rowCount: 0, hasWindows: true))
        XCTAssertTrue(state.shouldLog(rowCount: 2, hasWindows: true))
        XCTAssertFalse(state.shouldLog(rowCount: 2, hasWindows: true))
        XCTAssertTrue(state.shouldLog(rowCount: 0, hasWindows: true))
        XCTAssertFalse(state.shouldLog(rowCount: 0, hasWindows: true))
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
        hub.updateClaudeDesktop(.init(rows: [.init(status: .done, title: "First", completionHeld: true)]))
        XCTAssertEqual(peeks.count, 1)
        XCTAssertEqual(hub.compose().agents.filter { $0.app == "Claude" }.map(\.status), ["done"])
        hub.updateClaudeDesktop(.init(rows: [.init(status: .done, title: "First", completionHeld: true)]))
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
