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

    // MARK: 0.2.63 — leave must compact even when AppKit drops the tracking exit

    private let away = CGPoint(x: -500, y: -500)
    private func over(_ m: IslandModel) -> CGPoint { CGPoint(x: m.shapeRect.midX, y: m.shapeRect.midY) }
    private func sleep(_ s: Double) async throws { try await Task.sleep(nanoseconds: UInt64(s * 1_000_000_000)) }
    /// Under 0.5 s: grace (0.3) + one watchdog period (0.1) + scheduling slack.
    private let compactWithin = SamanthaMenuBarIslandPanel.leaveDelay + SamanthaMenuBarIslandPanel.watchInterval + 0.08

    private func openRoster() -> (IslandModel, SamanthaMenuBarIslandPanel) {
        let m = IslandModel()
        let panel = SamanthaMenuBarIslandPanel()
        panel.attach(model: m)
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 1)))
        m.click(at: CGPoint(x: 40, y: 12))
        XCTAssertTrue(m.cardOpen)
        return (m, panel)
    }

    func testLostExitDuringResizeCompactsViaWatchdog() async throws {
        let (m, panel) = openRoster()
        panel.applyPointer(over(m))                     // tracking: entered
        XCTAssertTrue(m.hovering)
        // A row joins: the card grows and tracking geometry is rebuilt under the pointer, which then
        // leaves — and AppKit never delivers mouseExited (the 0.2.62 bug: hover stays stale-true, so
        // apply()'s hover-hold keeps the card open indefinitely).
        var r = rows(age: 2)
        r.append(IslandAgent(id: "x1", name: "Codex", app: "Codex", title: "New", status: "running"))
        m.apply(IslandSnapshot(kind: "idle", agents: r))
        for s in 3...5 { m.apply(IslandSnapshot(kind: "idle", agents: rows(age: s) + [r[2]])) }
        XCTAssertTrue(m.cardOpen, "stale hover alone holds it (what the watchdog must undo)")
        XCTAssertTrue(m.hovering)
        panel.watchStep(away, now: Date())              // next watchdog tick sees the real pointer
        XCTAssertFalse(m.hovering, "watchdog reconciles the lost exit")
        XCTAssertTrue(panel.debugLeavePending, "and starts the normal leave grace")
        XCTAssertTrue(m.cardOpen, "grace still crosses a seam")
        try await sleep(compactWithin)
        XCTAssertFalse(m.cardOpen, "compacts within the grace")
    }

    func testClickWhileHoveringThenLeaveCompacts() async throws {
        let m = IslandModel()
        let panel = SamanthaMenuBarIslandPanel()
        panel.attach(model: m)
        m.apply(IslandSnapshot(kind: "idle", agents: rows(age: 1)))
        panel.applyPointer(over(m))       // hovering the compact pill
        XCTAssertTrue(m.hovering)
        m.click(at: CGPoint(x: 40, y: 12))              // click opens under the pointer
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.openSource, .user)
        panel.watchStep(over(m), now: Date())           // still over the card: held
        XCTAssertTrue(m.cardOpen)
        panel.watchStep(away, now: Date())              // moved away; no exit event
        try await sleep(compactWithin)
        XCTAssertFalse(m.cardOpen)
        // Click-close while hovered, then leave: stays compact.
        panel.applyPointer(over(m))
        m.click(at: CGPoint(x: 40, y: 12))
        XCTAssertTrue(m.cardOpen)
        m.click(at: CGPoint(x: 40, y: 12))
        XCTAssertFalse(m.cardOpen)
        panel.applyPointer(away)
        try await sleep(compactWithin)
        XCTAssertFalse(m.cardOpen)
    }

    func testRefreshDuringLeaveGraceStillCompacts() async throws {
        let (m, panel) = openRoster()
        panel.applyPointer(over(m))
        panel.applyPointer(away)                        // real exit: grace starts
        XCTAssertTrue(panel.debugLeavePending)
        try await sleep(0.1)
        // Row refresh mid-grace: snapshot + geometry reconcile + watchdog tick, pointer still away.
        var r = rows(age: 2)
        r.append(IslandAgent(id: "x1", name: "Codex", app: "Codex", title: "New", status: "running"))
        m.apply(IslandSnapshot(kind: "idle", agents: r))
        panel.applyPointer(away)
        panel.watchStep(away, now: Date())
        XCTAssertTrue(panel.debugLeavePending, "refresh must not cancel the pending leave")
        try await sleep(compactWithin - 0.1)
        XCTAssertFalse(m.cardOpen)
    }

    func testHoverHoldSurvivesWatchdogTicksWhileOver() async throws {
        let (m, panel) = openRoster()
        let t0 = Date()
        for s in 1...20 {                               // 20 s of refreshes + 10 Hz ticks, pointer over
            m.apply(IslandSnapshot(kind: "idle", agents: rows(age: s)), now: t0.addingTimeInterval(Double(s)))
            for k in 0..<10 { panel.watchStep(over(m), now: t0.addingTimeInterval(Double(s) + Double(k) / 10)) }
        }
        try await sleep(compactWithin)
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.openSource, .user)
    }

    func testHoveredAlertCardCompactsOnLeave() async throws {
        let m = IslandModel()
        let panel = SamanthaMenuBarIslandPanel()
        panel.attach(model: m)
        m.apply(IslandSnapshot(kind: "error", title: "label not found", app: "Calculator"))
        XCTAssertEqual(m.openSource, .alert)
        panel.applyPointer(over(m))
        XCTAssertEqual(m.openSource, .user, "hovered: hover-hold")
        panel.watchStep(away, now: Date())              // leave (exit lost or not)
        try await sleep(compactWithin)
        XCTAssertFalse(m.cardOpen, "hovered alert compacts with the short grace")
    }

    func testHoveredPeekDismissesWithShortGraceUnhoveredKeepsTimer() async throws {
        let m = IslandModel()
        m.showPeek(IslandPeekItem(id: "1", source: "Grok Bot", title: "Hi"))
        XCTAssertNotNil(m.peek)
        m.hovering = true
        m.hovering = false
        try await sleep(IslandModel.leaveGrace + 0.15)
        XCTAssertNil(m.peek, "hovered peek leaves with the 0.3 s grace, not 1.5 s")
        m.showPeek(IslandPeekItem(id: "2", source: "Grok Bot", title: "Again"))
        try await sleep(IslandModel.leaveGrace + 0.15)
        XCTAssertNotNil(m.peek, "unhovered peek keeps its own dwell timer")
        m.dismissPeek()
    }

    func testLeaveWatchArmsOnlyAfterHover() {
        var w = IslandLeaveWatch()
        let t = Date()
        XCTAssertNil(w.observe(over: false, active: true, now: t), "keyboard/alert-opened: never hovered")
        XCTAssertNil(w.observe(over: true, active: true, now: t))
        XCTAssertEqual(w.observe(over: false, active: true, now: t.addingTimeInterval(1)), 0)
        XCTAssertEqual(w.observe(over: false, active: true, now: t.addingTimeInterval(1.4)) ?? -1, 0.4, accuracy: 1e-6)
        XCTAssertNil(w.observe(over: false, active: false, now: t.addingTimeInterval(2)), "compact: disarmed")
        XCTAssertFalse(w.armed)
    }
}
