import AppKit
import XCTest
@testable import CUAService

/// P7: HUD, Quick Peek, Calendar, Duo/gestures, File Tray, agent-row controls.
@MainActor
final class IslandActivitiesTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var mem: MemoryTokenStore!
    private var store: IslandSettingsStore!

    override func setUp() {
        suite = "cua.island.p7.test.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
        mem = MemoryTokenStore()
        store = IslandSettingsStore(defaults: defaults, tokens: mem)
    }
    override func tearDown() { defaults.removePersistentDomain(forName: suite) }

    // MARK: A2 HUD
    func testMediaKeyDecodeAndStep() {
        let down = MediaKeyLogic.decode(data1: (0 << 16) | 0x0A00)
        XCTAssertEqual(down, .init(code: MediaKeyLogic.soundUp, down: true, isRepeat: false))
        let up = MediaKeyLogic.decode(data1: (7 << 16) | 0x0B01)
        XCTAssertEqual(up, .init(code: MediaKeyLogic.mute, down: false, isRepeat: true))
        XCTAssertEqual(MediaKeyLogic.next(0.5, up: true, fine: false), 0.5625, accuracy: 1e-9)
        XCTAssertEqual(MediaKeyLogic.next(0.65, up: true, fine: false), 0.6875, accuracy: 1e-9, "off-grid snaps up")
        XCTAssertEqual(MediaKeyLogic.next(0.65, up: false, fine: false), 0.625, accuracy: 1e-9, "off-grid snaps down")
        XCTAssertEqual(MediaKeyLogic.next(0.5, up: true, fine: true), 0.515625, accuracy: 1e-9)
        XCTAssertEqual(MediaKeyLogic.next(1, up: true, fine: false), 1)
        XCTAssertEqual(MediaKeyLogic.next(0, up: false, fine: false), 0)
        XCTAssertEqual(IslandHUDState(kind: .volume, value: 0.5, muted: true).symbol, "speaker.slash.fill")
        XCTAssertEqual(IslandHUDState(kind: .brightness, value: 0.5).symbol, "sun.max.fill")
    }

    func testHUDPrecedenceAndDwell() async throws {
        let t0 = Date()
        func p(_ k: String, hud: Bool = true, peek: Bool = false, approval: IslandAgentApproval? = nil) -> IslandPresentation {
            IslandModel.presentation(kind: k, stepTotal: 0, approval: approval, since: t0, now: t0.addingTimeInterval(100),
                                     media: true, hud: hud, peek: peek, live: [.media])
        }
        XCTAssertEqual(p("idle"), .hud)
        XCTAssertEqual(p("confirm"), .needsYou, "needs-you beats HUD")
        XCTAssertEqual(p("idle", approval: IslandAgentApproval(id: "a", title: "Ok?")), .agentApproval)
        XCTAssertEqual(p("idle", hud: false, peek: true), .peek)
        XCTAssertEqual(p("idle", hud: true, peek: true), .hud, "HUD beats peek")
        XCTAssertEqual(IslandPresentation.hud.compactWidth, 200)
        IslandModel.hudDwell = 0.1
        defer { IslandModel.hudDwell = 1.2 }
        let m = IslandModel()
        m.showHUD(.init(kind: .volume, value: 0.5))
        XCTAssertEqual(m.presentation, .hud)
        m.toggleCard()
        XCTAssertFalse(m.cardOpen, "HUD never opens a card")
        m.showHUD(.init(kind: .volume, value: 0.5625))
        XCTAssertEqual(m.hud?.value, 0.5625)
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertNil(m.hud)
        XCTAssertNotEqual(m.presentation, .hud)
    }

    func testHUDScrollAdjusts() {
        let m = IslandModel()
        var ups: [Bool] = []
        m.onHUDScroll = { _, up in ups.append(up) }
        m.showHUD(.init(kind: .volume, value: 0.5))
        m.handleScroll(dx: 0, dy: 25, precise: true, began: true, ended: false)
        XCTAssertEqual(ups, [true, true], "one step per 12 pt")
        m.clearHUD()
    }

    // MARK: A3 Peek
    func testPeekMergeAgeAndClick() {
        let t0 = Date()
        let a = IslandPeekItem(id: "1", source: "Grok Bot", title: "Build done", at: t0)
        var b = a; b.id = "2"; b.title = "Tests done"
        let (m1, anim) = IslandPeekRules.merge(current: a, incoming: b, now: t0.addingTimeInterval(1), window: 3)
        XCTAssertEqual(m1.count, 2); XCTAssertEqual(m1.id, "1"); XCTAssertFalse(anim)
        XCTAssertTrue(IslandPeekRules.merge(current: a, incoming: b, now: t0.addingTimeInterval(4), window: 3).animate)
        XCTAssertEqual(IslandPeekRules.age(t0, now: t0.addingTimeInterval(130)), "2m")
        let m = IslandModel()
        var opened: [String] = []
        m.onPeekOpen = { opened.append($0.title) }
        m.showPeek(a)
        XCTAssertEqual(m.presentation, .peek)
        XCTAssertEqual(IslandPresentation.peek.compactWidth, 244)
        m.toggleCard()
        XCTAssertEqual(opened, ["Build done"]); XCTAssertNil(m.peek)
    }

    func testHubPeekNeedsInfoCap() throws {
        let hub = IslandHub(store: store)
        var got: [IslandPeekItem] = []
        hub.onPeek = { got.append($0) }
        defaults.set(true, forKey: IslandSettingsSchema.agentPeeks)
        try store.rotateToken(.grokBot)
        var r = IslandHub.Request(method: "POST", path: "/api/island/publish",
                                  body: try JSONSerialization.data(withJSONObject: ["source": "grok-bot", "kind": "peek", "title": "Hi", "symbol": "bell"]))
        r.peerVerified = true
        XCTAssertEqual(hub.handle(r, port: 0).status, 200)
        XCTAssertEqual(got.map(\.symbol), ["bell"])
        XCTAssertTrue(hub.compose().agents.isEmpty, "peeks are never rows")
        defaults.set(false, forKey: IslandSettingsSchema.agentPeeks)
        XCTAssertEqual(hub.handle(r, port: 0).status, 403, "agent peeks off → refused, not silently dropped")
    }

    // MARK: A4 Calendar
    func testCalendarLogic() {
        let now = Date()
        let e = IslandCalendarEvent(id: "e", title: "Standup", start: now.addingTimeInterval(240), end: now.addingTimeInterval(1800))
        XCTAssertTrue(CalendarLogic.isActive(e, now: now, lead: 600))
        XCTAssertFalse(CalendarLogic.isActive(e, now: now.addingTimeInterval(-900), lead: 600))
        XCTAssertTrue(CalendarLogic.isUrgent(e, now: now))
        XCTAssertEqual(CalendarLogic.shortCountdown(e, now: now), "4m")
        XCTAssertEqual(CalendarLogic.shortCountdown(e, now: now.addingTimeInterval(300)), "now")
        XCTAssertEqual(CalendarLogic.ringFraction(e, now: now, lead: 600), 0.4, accuracy: 0.01)
        XCTAssertEqual(CalendarLogic.meetingURL(in: ["notes", "join https://meet.google.com/abc-defg-hij now"])?.host, "meet.google.com")
        XCTAssertNil(CalendarLogic.meetingURL(in: ["https://example.com/x"]))
        XCTAssertNotNil(CalendarLogic.color(hex: "#FF8800"))
        let m = IslandModel()
        m.setCalendar([e], now: now)
        XCTAssertEqual(m.presentation, .calendar)
        m.calendarEnabled = false
        m.setCalendar([e], now: now)
        XCTAssertEqual(m.presentation, .idle)
    }

    // MARK: A5/A6 Duo + gestures
    func testDuoOrderSwapAndSecondary() {
        XCTAssertEqual(IslandDuo.order(media: true, calendar: true, tray: true, swapped: false), [.media, .calendar, .tray])
        XCTAssertEqual(IslandDuo.order(media: true, calendar: true, tray: false, swapped: true), [.calendar, .media])
        let now = Date()
        let m = IslandModel()
        m.setNowPlaying(NowPlayingTests.track(), visible: true)
        m.setCalendar([IslandCalendarEvent(id: "e", title: "1:1", start: now.addingTimeInterval(120), end: now.addingTimeInterval(1800))], now: now)
        XCTAssertEqual(m.presentation, .nowPlaying)
        XCTAssertEqual(m.secondary, .calendar)
        XCTAssertNotNil(m.secondaryRect)
        XCTAssertTrue(m.hitRects.contains { $0.contains(m.secondaryRect!) })
        m.tapSecondary()
        XCTAssertEqual(m.presentation, .calendar)
        XCTAssertEqual(m.secondary, .media)
        m.idleActivity = "samantha"
        XCTAssertNil(m.secondary, "Duo only in duo mode")
    }

    func testSwipeTracker() {
        var t = IslandSwipeTracker()
        let t0 = Date()
        XCTAssertNil(t.feed(dx: -50, dy: 0, precise: false, began: true, ended: false, now: t0), "mouse wheel ignored")
        XCTAssertNil(t.feed(dx: -25, dy: 0, precise: true, began: true, ended: false, now: t0))
        XCTAssertEqual(t.feed(dx: -20, dy: 0, precise: true, began: false, ended: false, now: t0), .swapNext)
        XCTAssertNil(t.feed(dx: -40, dy: 0, precise: true, began: false, ended: false, now: t0), "one action per gesture")
        XCTAssertEqual(t.feed(dx: 0, dy: -35, precise: true, began: true, ended: false, now: t0), .open)
        XCTAssertEqual(t.feed(dx: 0, dy: 35, precise: true, began: false, ended: false, now: t0.addingTimeInterval(1)), .close, "gap resets")
        t.naturalMovement = true
        XCTAssertEqual(t.feed(dx: -45, dy: 0, precise: true, began: true, ended: false, now: t0), .swapPrevious)
        XCTAssertNil(t.feed(dx: -30, dy: -30, precise: true, began: true, ended: false, now: t0), "diagonal is ambiguous")
    }

    // MARK: A9 Tray
    func testTrayRulesAndDropTarget() throws {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/macos-cua/test-tray-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.trashItem(at: dir, resultingItemURL: nil) }
        let urls = try (0..<6).map { i -> URL in
            let u = dir.appendingPathComponent("f\(i).txt"); try "x".write(to: u, atomically: true, encoding: .utf8); return u
        }
        var items = IslandTrayRules.adding(urls, to: [])
        items = IslandTrayRules.adding([urls[0]], to: items)
        XCTAssertEqual(items.count, 6, "dedupe by path")
        XCTAssertEqual(items.first?.name, "f0.txt", "re-add moves to front")
        XCTAssertEqual(IslandTrayRules.adding(Array(urls.prefix(2)), to: []).map(\.name), ["f0.txt", "f1.txt"], "drop keeps order")
        XCTAssertEqual(IslandTrayRules.visible(items).extra, 2)
        XCTAssertNotNil(items.first?.bookmark)
        XCTAssertTrue(IslandTrayRules.nearPill(CGPoint(x: 300, y: 70), shape: CGRect(x: 283, y: 0, width: 74, height: 24)))
        XCTAssertFalse(IslandTrayRules.nearPill(CGPoint(x: 10, y: 200), shape: CGRect(x: 283, y: 0, width: 74, height: 24)))
        let m = IslandModel()
        var saved = 0
        m.onTrayChange = { _ in saved += 1 }
        m.setTrayTargeted(true)
        XCTAssertEqual(m.shapeRect.size, IslandBarLayout.dropTargetSize)
        m.addToTray([urls[1]])
        m.setTrayTargeted(false)
        XCTAssertEqual(m.tray.count, 1); XCTAssertEqual(saved, 1)
        XCTAssertEqual(m.presentation, .tray)
        m.clearTray()
        XCTAssertTrue(m.tray.isEmpty)
        let data = try JSONEncoder().encode(items)
        XCTAssertEqual(try JSONDecoder().decode([IslandTrayItem].self, from: data), items)
    }

    // MARK: 7 Agent rows
    func testRowControlRulesAndFlow() async throws {
        let run = IslandAgent(id: "codex:j1", name: "Codex", title: "Migrating", status: "running", controls: ["stop"])
        let err = IslandAgent(id: "codex:j2", name: "Codex", title: "Build", status: "error", controls: ["retry"])
        XCTAssertEqual(IslandAgentRows.control(for: run), .stop)
        XCTAssertEqual(IslandAgentRows.control(for: err), .retry)
        XCTAssertEqual(IslandAgentRows.control(for: IslandAgent(id: "x", status: "running")), .stop, "every running row shows Stop")
        XCTAssertEqual(IslandAgentRows.control(for: IslandAgent(id: "pending:a", status: "needs_you")), .approve)
        XCTAssertEqual(IslandAgentRows.cardHeight(rows: 1, footer: false), 96)
        XCTAssertEqual(IslandAgentRows.cardHeight(rows: 2, footer: false), 120)
        XCTAssertEqual(IslandAgentRows.cardHeight(rows: 3, footer: false), 164)
        XCTAssertEqual(IslandAgentRows.cardHeight(rows: 9, footer: true), 226, "4 rows + footer cap")
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "idle", agents: [run, err]))
        var sent: [String] = []
        m.onRowControl = { a, act in sent.append("\(a.id)=\(act.rawValue)") }
        let t0 = Date()
        m.tapRowControl(run, now: t0)
        XCTAssertTrue(m.isStopArmed(run.id, now: t0)); XCTAssertTrue(sent.isEmpty, "first click arms")
        m.tapRowControl(run, now: t0.addingTimeInterval(1))
        XCTAssertEqual(sent, ["codex:j1=stop"])
        m.tapRowControl(run, now: t0.addingTimeInterval(1.5))
        XCTAssertEqual(sent.count, 1, "in-flight ignores repeats")
        var stopped = run; stopped.status = "done"
        m.apply(IslandSnapshot(kind: "idle", agents: [stopped, err]))
        m.reconcileRowControls()
        XCTAssertNil(m.rowControls[run.id], "status change = ack")
        m.tapRowControl(err)
        XCTAssertEqual(sent.last, "codex:j2=retry")
        m.failRowControl(err.id)
        XCTAssertNil(m.rowControls[err.id]); XCTAssertTrue(m.rowFlash.contains(err.id))
    }

    func testHubControlQueueAndDeliverOnce() async throws {
        let hub = IslandHub(store: store)
        try store.rotateToken(.grokBot)
        func call(_ method: String, _ path: String, _ json: [String: Any] = [:], query: [String: String] = [:]) -> IslandHub.Response {
            var r = IslandHub.Request(method: method, path: path, query: query,
                                      body: method == "POST" ? try! JSONSerialization.data(withJSONObject: json) : Data())
            r.peerVerified = true
            if method == "GET" { r.headers["x-island-source"] = "grok-bot" }
            return hub.handle(r, port: 0)
        }
        XCTAssertEqual(call("POST", "/api/island/publish", ["source": "grok-bot", "id": "j1", "kind": "running", "title": "M", "controls": ["stop", "bogus"]]).status, 200)
        XCTAssertEqual(hub.compose().agents.first?.controls, ["stop"])
        let id = IslandHub.key(.grokBot, "j1")
        XCTAssertFalse(hub.requestControl(id: id, action: "retry"), "not advertised")
        let waiter = Task { await hub.waitForControl(id, timeout: 2) }
        XCTAssertTrue(hub.requestControl(id: id, action: "stop"))
        await waiter.value
        XCTAssertEqual(hub.pendingControls[id], "stop")
        XCTAssertEqual(call("GET", "/api/agent/control", query: ["id": "j1", "source": "grok-bot"]).status, 200)
        XCTAssertNil(hub.pendingControls[id], "delivered once")
        XCTAssertTrue(hub.requestControl(id: id, action: "stop"))
        hub.expire(now: Date().addingTimeInterval(4000))
        XCTAssertNil(hub.pendingControls[id], "expired row drops its queued control")
        XCTAssertFalse(hub.requestControl(id: id, action: "stop"), "row gone")
    }
}
