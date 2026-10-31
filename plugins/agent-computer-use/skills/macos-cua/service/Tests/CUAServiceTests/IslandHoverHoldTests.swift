import XCTest
@testable import CUAService

/// Hover-hold contract (0.2.62): a card the user opened stays while the pointer is over it, data
/// refreshes never reset it, and it compacts only when the pointer leaves. Alert-opened cards keep
/// their timers unless the pointer enters, which switches them to hover-hold.
@MainActor
final class IslandHoverHoldTests: XCTestCase {
    private func rows(age: Int) -> [IslandAgent] {
        var a = IslandAgent(id: "c1", name: "Cursor", app: "Cursor", title: "Fix island", status: "running")
        a.local_detail = "Grok 4.7 · high · \(age)s"
        var b = IslandAgent(id: "g1", name: "Bot", app: "Grok Bot", title: "Working", status: "running")
        b.local_detail = "\(age / 60)m"
        return [a, b]
    }

    func testUserOpenedRosterHoldsThroughRefreshesWhileHoveredAndCompactsOnLeave() {
        let m = IslandModel()
        let t0 = Date()
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 1)), now: t0)
        m.click(at: CGPoint(x: 40, y: 12))          // user opens the island
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.openSource, .user)
        m.hovering = true
        // 20 s of per-second roster refreshes (ages tick, a row joins) and clock ticks.
        for s in 1...20 {
            var r = rows(age: s)
            if s == 10 { r.append(IslandAgent(id: "x1", name: "Codex", app: "Codex", title: "New", status: "running")) }
            XCTAssertTrue(m.apply(IslandSnapshot(kind: "idle", agents: r), now: t0.addingTimeInterval(Double(s))))
            m.tick(now: t0.addingTimeInterval(Double(s) + 0.5))
            XCTAssertTrue(m.cardOpen, "refresh \(s) must not collapse a hovered, user-opened island")
            XCTAssertEqual(m.openSource, .user)
        }
        m.hovering = false
        m.pointerLeft()                                // panel calls this after leaveDelay
        XCTAssertFalse(m.cardOpen, "pointer leave compacts it")
        XCTAssertEqual(m.openSource, .closed)
    }

    func testRefreshDoesNotResetRosterEvenBeforeHoverIsReported() {
        // Root cause of the 0.2.61 bug: apply() reset userOpened on every idle snapshot.
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 1)))
        m.openAgentList()
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 2)))
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.cardMode, .agents)
    }

    func testRosterSurvivesAnAnsweredAsk() {
        let m = IslandModel()
        let ask = IslandAgentApproval(id: "a1", agent: "Codex", app: "Codex", title: "Run tests?")
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 1), agent_approval: ask))
        m.openAgentList()
        m.hovering = true
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 2)))   // ask answered elsewhere
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.cardMode, .agents)
    }

    func testAlertPeekAutoCollapsesOnItsTimerWhenNotHovered() {
        let m = IslandModel()
        let t0 = Date()
        m.apply(IslandSnapshot(kind: "error", title: "label not found", app: "Calculator"), now: t0)
        XCTAssertEqual(m.openSource, .alert)
        m.tick(now: t0.addingTimeInterval(IslandModel.peekError + 0.1))
        XCTAssertFalse(m.cardOpen, "alert keeps its timed auto-collapse")
    }

    func testPointerEnteringAnAlertSwitchesToHoverHold() {
        let m = IslandModel()
        let t0 = Date()
        m.apply(IslandSnapshot(kind: "error", title: "label not found", app: "Calculator"), now: t0)
        XCTAssertEqual(m.openSource, .alert)
        m.hovering = true
        XCTAssertEqual(m.openSource, .user, "pointer in: the alert timer no longer applies")
        m.tick(now: t0.addingTimeInterval(IslandModel.peekError + 30))
        XCTAssertTrue(m.cardOpen, "held while hovered, past the alert timer")
        m.hovering = false
        m.pointerLeft()
        XCTAssertFalse(m.cardOpen, "compacts when the pointer leaves")
    }

    func testEmptyRosterWaitsForThePointer() async throws {
        let m = IslandModel()
        m.openEmptyRoster(after: 0.05)
        m.hovering = true
        try await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertTrue(m.cardOpen, "the 3 s empty-card timer never closes it under the pointer")
        m.hovering = false
        m.pointerLeft()
        XCTAssertFalse(m.cardOpen)
    }

    func testLeaveGraceIsShortButCrossesASeam() {
        XCTAssertGreaterThanOrEqual(SamanthaMenuBarIslandPanel.leaveDelay, 0.25)
        XCTAssertLessThanOrEqual(SamanthaMenuBarIslandPanel.leaveDelay, 0.4)
    }
}
