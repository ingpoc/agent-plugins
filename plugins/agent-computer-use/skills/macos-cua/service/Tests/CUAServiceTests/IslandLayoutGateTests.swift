import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Deterministic layout gate over every island view and state, light + dark, normal + worst-case content.
/// Frames come from the views' own geometry probes (`layoutProbe`), not pixels. Fails on sibling overlap,
/// text cut off by its frame or clipped by the pill/card, and content inside the 8 pt minimum inset.
/// Optional: CUA_ISLAND_GATE_REPORT=<file> writes every finding (and the state list) for review.
@MainActor
final class IslandLayoutGateTests: XCTestCase {
    override func setUp() { IslandLayoutRecorder.enabled = true }
    override func tearDown() { IslandLayoutRecorder.enabled = false; IslandLayoutRecorder.shared.reset() }

    private func measure<V: View>(_ view: V, scheme: ColorScheme) -> ([IslandLayoutItem], [String: IslandLayoutContainer]) {
        IslandLayoutRecorder.shared.reset()
        let r = ImageRenderer(content: view.environment(\.colorScheme, scheme))
        r.scale = 2
        _ = r.cgImage
        return IslandLayoutRecorder.shared.snapshot()
    }

    private func gate(_ states: [(String, IslandModel)]) -> (issues: [String], checked: [String]) {
        var issues: [String] = [], checked: [String] = []
        for (name, m) in states {
            for (tag, scheme) in [("light", ColorScheme.light), ("dark", ColorScheme.dark)] {
                let (items, containers) = measure(SamanthaIslandBar(model: m), scheme: scheme)
                checked.append("\(name)-\(tag): \(items.count) probes")
                if items.isEmpty { issues.append("\(name)-\(tag): no probes recorded") }
                issues += IslandLayoutGate.issues(state: "\(name)-\(tag)", items: items, containers: containers)
            }
        }
        return (issues, checked)
    }

    static func states() -> [(String, IslandModel)] {
        IslandModel.hudDwell = 600; IslandModel.peekDwell = 600
        let now = Date()
        var out: [(String, IslandModel)] = []
        func add(_ n: String, _ f: (IslandModel) -> Void) { let m = IslandModel(); m.trayEnabled = true; f(m); out.append((n, m)) }
        // Voice states, including small cards and warnings with activity present.
        for kind in ["listening", "thinking", "speaking", "acting", "confirm", "error", "done", "reconnecting"] {
            add("voice-\(kind)") { m in
                m.forceCompact = true
                m.apply(IslandSnapshot(kind: kind, title: kind == "error" ? "Action not confirmed" : "Checking the calendar", detail: "Review the result before continuing", app: "Calendar"))
            }
            add("voice-\(kind)-card") { m in
                m.apply(IslandSnapshot(kind: kind, title: kind == "error" ? "Action not confirmed" : "Checking the calendar", detail: "Review the result before continuing", app: "Calendar"))
                if !m.cardOpen { m.toggleCard() }
            }
        }
        add("consent-long") { m in
            m.apply(IslandSnapshot(kind: "confirm", title: "Allow saved sign-in?", detail: String(repeating: "W", count: 160), app: "Comet"))
            m.toggleCard()
        }
        // HUD
        add("hud-volume") { $0.showHUD(.init(kind: .volume, value: 0.6875)) }
        add("hud-volume-max") { $0.showHUD(.init(kind: .volume, value: 1)) }
        add("hud-muted") { $0.showHUD(.init(kind: .volume, value: 0.5, muted: true)) }
        add("hud-brightness") { $0.showHUD(.init(kind: .brightness, value: 0.4)) }
        // Peek
        add("peek") { $0.showPeek(IslandPeekItem(id: "p", source: "Grok Bot", symbol: "checkmark.seal.fill", title: "Build finished", at: now), now: now) }
        add("peek-worst") { m in
            for i in 0..<99 {
                m.showPeek(IslandPeekItem(id: "w\(i)", source: "Grok Bot", symbol: "checkmark.seal.fill",
                                          title: "Deployment to production finished for all twelve regions", at: now.addingTimeInterval(-7300)), now: now)
            }
        }
        // Calendar
        let ev = IslandCalendarEvent(id: "e1", title: "Design review", start: now.addingTimeInterval(8 * 60), end: now.addingTimeInterval(38 * 60),
                                     location: "Room 4", calendar: "Work", colorHex: "#0A84FF", meetingURL: URL(string: "https://meet.google.com/abc"))
        add("calendar-compact") { $0.setCalendar([ev], now: now) }
        add("calendar-card") { $0.setCalendar([ev, IslandCalendarEvent(id: "e2", title: "Lunch", start: now.addingTimeInterval(3 * 3600), end: now.addingTimeInterval(4 * 3600))], now: now); $0.toggleCard() }
        add("calendar-card-worst") { $0.calendarLead = 2 * 86_400; $0.setCalendar(Self.worstAgenda(now: now), now: now); $0.toggleCard() }
        add("calendar-compact-worst") { $0.calendarLead = 2 * 86_400; $0.setCalendar(Self.worstAgenda(now: now), now: now) }
        // Duo
        add("duo") { $0.setNowPlaying(NowPlayingTests.track(), visible: true); $0.setCalendar([ev], now: now) }
        add("duo-swapped") { $0.setNowPlaying(NowPlayingTests.track(), visible: true); $0.setCalendar([ev], now: now); $0.tapSecondary() }
        add("duo-tray-99") { m in m.setNowPlaying(NowPlayingTests.track(), visible: true); m.addToTray(Self.files(99)) }
        // Tray
        add("tray-droptarget") { $0.setTrayTargeted(true) }
        add("tray-compact-99") { $0.addToTray(Self.files(99)) }
        add("tray-card") { $0.addToTray(Self.files(6)); $0.toggleCard() }
        add("tray-card-worst") { $0.addToTray(Self.files(99, long: true)); $0.toggleCard() }
        // Agents + focus
        let rows = Self.rows(3)
        add("agents-card") { m in m.idleActivity = "agents"; m.apply(IslandSnapshot(kind: "idle", agents: rows)); m.toggleCard() }
        add("agents-card-worst") { m in m.idleActivity = "agents"; m.apply(IslandSnapshot(kind: "idle", agents: Self.rows(9, long: true))); m.toggleCard() }
        add("agents-stop-armed") { m in m.idleActivity = "agents"; m.apply(IslandSnapshot(kind: "idle", agents: rows)); m.toggleCard(); m.tapRowControl(rows[0]) }
        add("focus-agent-row") { m in m.idleActivity = "agents"; m.apply(IslandSnapshot(kind: "idle", agents: rows)); m.hotKeyPressed() }
        // Decision card + focus
        add("decision") { $0.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "g:x", agent: "Grok Bot", app: "Comet", title: "Deploy to staging?"))); $0.toggleCard(byClick: false) }
        add("decision-worst") { $0.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "g:y", agent: "Grok Bot", app: "Comet",
            title: "Deploy the payments service to production in all twelve regions now?", subtitle: "This restarts every worker and drains 4,812 open connections across the fleet"))); $0.toggleCard(byClick: false) }
        add("focus-decision") { $0.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "g:z", agent: "Grok Bot", app: "Comet", title: "Deploy to staging?"))); $0.hotKeyPressed() }
        return out
    }

    static func worstAgenda(now: Date) -> [IslandCalendarEvent] {
        var c = Calendar.current.dateComponents([.year, .month, .day], from: now); c.hour = 12; c.minute = 59
        var start = Calendar.current.date(from: c)!
        if start <= now { start = Calendar.current.date(byAdding: .day, value: 1, to: start)! }
        let e = IslandCalendarEvent(id: "w", title: "Quarterly planning and roadmap alignment with the platform, payments and growth teams",
                                    start: start, end: start.addingTimeInterval(3600), location: "Building 4, Floor 12, Conference Room Atlantic (hybrid)",
                                    calendar: "Work", colorHex: "#FF9F0A", meetingURL: URL(string: "https://meet.google.com/xyz"))
        return [e] + (1...3).map { i in IslandCalendarEvent(id: "n\(i)", title: "Follow-up session number \(i) with a long name", start: start.addingTimeInterval(Double(i) * 3600), end: start.addingTimeInterval(Double(i) * 3600 + 1800)) }
    }

    static func files(_ n: Int, long: Bool = false) -> [URL] {
        (0..<n).map { URL(fileURLWithPath: "/nonexistent/island-gate/\(long ? "Quarterly financial report final version " : "f")\($0).pdf") }
    }

    static func rows(_ n: Int, long: Bool = false) -> [IslandAgent] {
        let base = [
            IslandAgent(id: "codex:j1", name: "Codex", app: "Codex", title: "Migrating database", status: "running", elapsed_s: 245, controls: ["stop"]),
            IslandAgent(id: "cursor:b2", name: "Cursor", app: "Cursor", title: "Build failed: 3 errors", status: "error", controls: ["retry"]),
            IslandAgent(id: "grok-bot:r3", name: "Grok Bot", app: "Comet", title: "Booking table for 4", status: "running", elapsed_s: 30),
        ]
        return (0..<n).map { i in
            var a = base[i % 3]; a.id += "-\(i)"
            if long { a.name = "Background Agent Number \(i)"; a.title = "Refactoring the entire payments pipeline and its integration tests"; a.elapsed_s = 3 * 3600 + 59 }
            return a
        }
    }

    func testLayoutGateEveryIslandState() async throws {
        let states = Self.states()
        try await Task.sleep(nanoseconds: 400_000_000)   // settled states: the 160 ms content-swap pinch has finished
        let (issues, checked) = gate(states)
        IslandModel.hudDwell = 1.2; IslandModel.peekDwell = 3
        if let path = ProcessInfo.processInfo.environment["CUA_ISLAND_GATE_REPORT"] {
            try (["# Island layout gate", "states checked: \(checked.count)", ""] + checked + ["", "issues: \(issues.count)"] + issues)
                .joined(separator: "\n").appending("\n").write(toFile: path, atomically: true, encoding: .utf8)
        }
        XCTAssertEqual(checked.count, states.count * 2, "every state in light and dark")
        XCTAssertGreaterThanOrEqual(states.count, 23)
        XCTAssertTrue(issues.isEmpty, "layout gate:\n" + issues.joined(separator: "\n"))
    }

    /// Calendar card: the time column and the event column (dot, title, room, countdown) never intersect,
    /// keep ≥ 12 pt between them, and in the worst case (12:59 PM + long title) the time shows in full.
    func testCalendarCardTimeAndEventColumnsNeverIntersect() async throws {
        let all = Self.states()
        try await Task.sleep(nanoseconds: 400_000_000)
        for name in ["calendar-card", "calendar-card-worst"] {
            let m = try XCTUnwrap(all.first { $0.0 == name }?.1)
            let (items, _) = measure(SamanthaIslandBar(model: m), scheme: .dark)
            let byId = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            let time = try XCTUnwrap(byId["cal-time-column"], name)
            XCTAssertLessThanOrEqual(time.ideal.width, time.frame.width + IslandLayoutGate.tolerance, "\(name): time never truncated")
            for id in ["cal-dot", "cal-title", "cal-room", "cal-relative"] {
                let ev = try XCTUnwrap(byId[id], "\(name) \(id)")
                XCTAssertFalse(ev.frame.intersects(time.drawn), "\(name): \(id) intersects the time")
                XCTAssertGreaterThanOrEqual(ev.frame.minX - time.frame.maxX, CalendarCardBody.columnGap - IslandLayoutGate.tolerance, "\(name): \(id) gap")
            }
            let dot = byId["cal-dot"]!, title = byId["cal-title"]!, room = byId["cal-room"]!, rel = byId["cal-relative"]!
            XCTAssertEqual(title.frame.minX - dot.frame.maxX, 6, accuracy: 0.5, "\(name): dot sits 6 pt before the title")
            XCTAssertEqual(room.frame.minX, title.frame.minX, accuracy: 0.5, "\(name): room aligns to the title")
            XCTAssertEqual(rel.frame.minX, title.frame.minX, accuracy: 0.5, "\(name): countdown aligns to the title")
        }
    }

    /// The gate must catch the class of bug it was built for: the pre-fix calendar card (fixed 120 pt time
    /// column, event column at x 132) drew "10:29 AM" under the calendar dot.
    func testLayoutGateCatchesPreFixCalendarOverlap() {
        let start = Calendar.current.date(bySettingHour: 10, minute: 29, second: 0, of: Date().addingTimeInterval(86_400))!
        let e = IslandCalendarEvent(id: "l", title: "Design review", start: start, end: start.addingTimeInterval(1800), location: "Room 4", colorHex: "#0A84FF")
        let (items, _) = measure(PreFixCalendarHeader(event: e), scheme: .dark)
        let card = IslandLayoutContainer(name: "shape", rect: CGRect(origin: .zero, size: IslandBarLayout.cardSize), inset: CGSize(width: 8, height: 8))
        let issues = IslandLayoutGate.issues(state: "pre-fix-calendar", items: items, containers: ["shape": card])
        if let path = ProcessInfo.processInfo.environment["CUA_ISLAND_GATE_REPORT"] {
            try? (["", "# Pre-fix calendar card (regression proof)"] + issues).joined(separator: "\n").appending("\n")
                .write(toFile: path + ".prefix.txt", atomically: true, encoding: .utf8)
        }
        XCTAssertTrue(issues.contains { $0.contains("legacy-time") && ($0.contains("overlaps") || $0.contains("cut off")) },
                      "gate must flag the old overlap; got:\n" + issues.joined(separator: "\n"))
    }
}

/// Verbatim header of the pre-fix CalendarCardBody (HEAD before the fix), with probes added.
private struct PreFixCalendarHeader: View {
    let event: IslandCalendarEvent
    var body: some View {
        let p = IslandBarLayout.cardPadding
        ZStack(alignment: .topLeading) {
            VStack(alignment: .leading, spacing: 2) {
                Text(CalendarCardBody.time.string(from: event.start))
                    .font(IslandType.value).foregroundStyle(Color.white)
                    .contentTransition(.numericText()).lineLimit(1).minimumScaleFactor(0.6)
                    .layoutProbe("legacy-time")
                Text(CalendarCardBody.day.string(from: event.start)).font(IslandType.meta).layoutProbe("legacy-day")
            }
            .frame(width: 120, alignment: .leading)
            .offset(x: p, y: p - 4)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    IslandCalendarDot(event: event).frame(width: 6, height: 6).layoutProbe("legacy-dot", .glyph)
                    Text(event.title).font(IslandType.title).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .layoutProbe("legacy-title", truncates: true)
                }
                Text(event.location).font(IslandType.body).lineLimit(1).layoutProbe("legacy-room", truncates: true)
            }
            .frame(width: IslandBarLayout.cardSize.width - 132 - p, alignment: .topLeading)
            .offset(x: 132, y: p)
        }
        .frame(width: IslandBarLayout.cardSize.width, height: IslandBarLayout.cardSize.height, alignment: .topLeading)
        .coordinateSpace(name: IslandLayoutRecorder.space)
    }
}
