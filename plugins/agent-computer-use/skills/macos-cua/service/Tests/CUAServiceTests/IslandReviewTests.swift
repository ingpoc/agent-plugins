import AppKit
import XCTest
@testable import CUAService

/// island-cards-review r3 (P0–P2): events, Quick Peek, sound, TTL, choice card, Now Playing, queue,
/// expiry, keys, Duo. MemoryTokenStore + temp defaults only; fake now-playing data; no sound played.
@MainActor
final class IslandReviewTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var store: IslandSettingsStore!
    private var events: [[String: Any]] = []

    override func setUp() {
        suite = "cua.island.review.test.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
        store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        try? store.rotateToken(.grokBot); try? store.rotateToken(.cursor); try? store.rotateToken(.codex)
        for src in [AgentSource.grokBot, .cursor, .codex] {
            store.set(true, for: src.keyPrefix + "enabled")
            store.set(true, for: src.keyPrefix + "canRaiseApprovals")
            store.set("attention", for: src.keyPrefix + "cap")
        }
        events = []
        IslandEventLog.sink = { [weak self] e in MainActor.assumeIsolated { self?.events.append(e) } }
        IslandSound.played = 0
    }

    override func tearDown() {
        IslandEventLog.sink = nil
        defaults.removePersistentDomain(forName: suite)
    }

    private func post(_ hub: IslandHub, _ body: [String: Any], now: Date = Date()) -> IslandHub.Response {
        var r = IslandHub.Request(method: "POST", path: "/api/island/publish",
                                  body: try! JSONSerialization.data(withJSONObject: body))
        r.peerVerified = true
        return hub.handle(r, port: 0, now: now)
    }

    private func choice(_ id: String = "q1", source: String = "grok-bot", n: Int = 2, extra: [String: Any] = [:]) -> [String: Any] {
        ["source": source, "id": id, "kind": "choice", "prompt": "SECRET prompt \(id)",
         "options": (1...n).map { ["label": "Opt \($0)", "value": "v\($0)"] }].merging(extra) { $1 }
    }

    private func approval(_ id: String, source: String = "codex") -> [String: Any] {
        ["source": source, "id": id, "kind": "approval", "title": "SECRET approve \(id)"]
    }

    private func names(_ e: String) -> [[String: Any]] { events.filter { $0["event"] as? String == e } }

    // MARK: P0 — events

    func testEventLogCoversLifecycleWithoutContent() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        XCTAssertEqual(post(c.hub, choice(extra: ["allow_text": true])).status, 200)
        XCTAssertEqual(names("shown").count, 1)
        XCTAssertEqual(names("opened").first?["via"] as? String, "peek")
        m.hovering = true
        XCTAssertEqual(names("hovered").count, 1)
        m.hovering = false; m.hovering = true
        XCTAssertEqual(names("hovered").count, 1, "once per card")
        m.click(at: CGPoint(x: 12, y: 30))
        let click = names("clicked").last
        XCTAssertEqual(click?["x"] as? Double, 12); XCTAssertEqual(click?["y"] as? Double, 30)
        m.choiceText = "SECRET reply"
        m.tapOption("v1")
        try await Task.sleep(nanoseconds: 50_000_000)
        let ans = names("answered").last
        XCTAssertEqual(ans?["via"] as? String, "island-click")
        XCTAssertEqual(ans?["has_text"] as? Bool, true)
        // Expired path.
        _ = post(c.hub, choice("q2", extra: ["ttl_s": 30]))
        c.hub.expire(now: Date().addingTimeInterval(31))
        XCTAssertEqual(names("expired").count, 1)
        // Clear path.
        _ = post(c.hub, choice("q3"))
        _ = post(c.hub, ["source": "grok-bot", "id": "q3", "kind": "clear"])
        XCTAssertEqual(names("answered").last?["via"] as? String, "chat-clear")
        // Nothing content-bearing anywhere.
        let blob = events.map { String(describing: $0) }.joined()
        XCTAssertFalse(blob.contains("SECRET"), "no titles/prompts/text in events")
        XCTAssertFalse(blob.contains("Opt 1") || blob.contains("v1"), "no option labels/values")
    }

    func testEventLogDropsUnknownKeysAndWritesNothingUnderTests() {
        XCTAssertNil(IslandEventLog.url, "tests never write the real ~/.cache log")
        IslandEventLog.log("clicked", id: "x", ["x": 1.0, "title": "SECRET", "text": "SECRET"])
        XCTAssertNil(events.last?["title"]); XCTAssertNil(events.last?["text"])
        XCTAssertEqual(events.last?["x"] as? Double, 1)
    }

    // MARK: P0 — whole pill click target

    func testWholePillIncludingTopEdgeIsHittable() {
        let shape = IslandBarLayout.shapeRect(compactWidth: 136, card: false)
        let target = IslandBarLayout.pillTarget(shape: shape)
        XCTAssertEqual(target.minY, 0, "a throw to the screen edge lands")
        XCTAssertTrue(target.contains(CGPoint(x: shape.midX, y: 0.5)))
        XCTAssertTrue(target.contains(CGPoint(x: shape.minX - 3, y: shape.midY)))
        let rects = IslandBarLayout.hitRects(shape: shape, circleCount: 0, agentCount: 0)
        XCTAssertTrue(rects.contains { $0.contains(CGPoint(x: shape.maxX - 1, y: 1)) })
    }

    // MARK: P0 — Quick Peek

    func testPeekSchedulerSlotsAndCap() {
        let t0 = Date(timeIntervalSince1970: 1000)
        var s = IslandPeekScheduler()
        s.reset(id: "a", arrived: t0, expires: t0.addingTimeInterval(900))
        XCTAssertEqual(s.slots, [t0, t0.addingTimeInterval(120), t0.addingTimeInterval(840)])
        XCTAssertTrue(s.due(now: t0, suppressed: false))
        XCTAssertFalse(s.due(now: t0.addingTimeInterval(60), suppressed: false))
        XCTAssertFalse(s.due(now: t0.addingTimeInterval(121), suppressed: true), "typing in the chat app holds it")
        XCTAssertTrue(s.due(now: t0.addingTimeInterval(125), suppressed: false), "…until he stops")
        XCTAssertTrue(s.due(now: t0.addingTimeInterval(841), suppressed: false), "T-60")
        XCTAssertFalse(s.due(now: t0.addingTimeInterval(899), suppressed: false), "max 3")
        XCTAssertEqual(s.count, 3)
        // Missed slots collapse into one peek.
        var m = IslandPeekScheduler()
        m.reset(id: "b", arrived: t0, expires: t0.addingTimeInterval(900))
        XCTAssertTrue(m.due(now: t0.addingTimeInterval(850), suppressed: false))
        XCTAssertFalse(m.due(now: t0.addingTimeInterval(851), suppressed: false))
        XCTAssertEqual(m.count, 1)
        // Short TTL: no back-to-back T-60.
        var q = IslandPeekScheduler()
        q.reset(id: "c", arrived: t0, expires: t0.addingTimeInterval(90))
        XCTAssertEqual(q.slots, [t0, t0.addingTimeInterval(30)], "arrival + T-60 only")
    }

    func testPeekOpensNonKeyFor4sAndHoverPins() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        c.debugPanel.debugEnsurePanel()
        let m = c.debugModel
        _ = post(c.hub, choice())
        XCTAssertTrue(m.cardOpen); XCTAssertTrue(m.isPeeking)
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "peek is never key")
        m.tick(now: Date().addingTimeInterval(4.5))
        XCTAssertFalse(m.cardOpen, "peek closes after 4 s")
        // Next peek slot (+2 min); hover pins it.
        m.tick(now: Date().addingTimeInterval(121))
        XCTAssertTrue(m.cardOpen)
        m.hovering = true
        m.tick(now: Date().addingTimeInterval(130))
        XCTAssertTrue(m.cardOpen, "hover pinned")
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey)
    }

    func testNoPeekWhileTypingInSourceApp() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        m.peekSuppressed = { _ in true }
        _ = post(c.hub, choice())
        XCTAssertFalse(m.cardOpen)
        m.peekSuppressed = { _ in false }
        m.tick(now: Date().addingTimeInterval(1))
        XCTAssertTrue(m.cardOpen, "peeks once he stops typing")
    }

    func testSourceAppMatching() {
        let a = IslandAgentApproval(id: "x", agent: "Grok Bot", app: "Comet", kind: "choice")
        XCTAssertTrue(IslandInput.isSourceApp("Comet", approval: a))
        XCTAssertFalse(IslandInput.isSourceApp("Finder", approval: a))
        XCTAssertFalse(IslandInput.isSourceApp(nil, approval: a))
    }

    // MARK: P0 — sound + TTL

    func testTinkOnArrivalUnlessMuted() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.frontmostAppName = { "Finder" }
        c.start(); defer { c.shutdown() }
        _ = post(c.hub, choice("s1"))
        XCTAssertEqual(IslandSound.played, 1)
        _ = post(c.hub, choice("s1"))
        XCTAssertEqual(IslandSound.played, 1, "re-post of the same card is not a new arrival")
        store.set(true, for: IslandSettingsSchema.mutePromptSound)
        _ = post(c.hub, ["source": "grok-bot", "id": "s1", "kind": "clear"])
        _ = post(c.hub, choice("s2"))
        XCTAssertEqual(IslandSound.played, 1, "muted")
        store.set(false, for: IslandSettingsSchema.mutePromptSound)
        c.frontmostAppName = { "Comet" }
        _ = post(c.hub, choice("s3"))
        XCTAssertEqual(IslandSound.played, 1, "source app frontmost → silent")
        XCTAssertNotNil(IslandSettingsSchema.all.first { $0.key == IslandSettingsSchema.mutePromptSound })
    }

    func testChoiceDefaultTimeoutIs15Minutes() {
        let hub = IslandHub(store: store)
        let t0 = Date(timeIntervalSince1970: 10_000)
        _ = post(hub, choice(), now: t0)
        XCTAssertEqual(hub.compose(now: t0).agent_approval?.expires_at, 10_900)
        hub.expire(now: t0.addingTimeInterval(899))
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "q1")), .pending)
        hub.expire(now: t0.addingTimeInterval(901))
        XCTAssertEqual(hub.decision(IslandHub.key(.grokBot, "q1")), .expired)
    }

    // MARK: P1 — choice card

    func testMaxFourSlotsAndNoScroll() {
        var a = IslandAgentApproval(id: "x", kind: "choice", options: (1...4).map { .init(label: "\($0)", value: "\($0)") })
        XCTAssertEqual(IslandChoiceLayout.shownOptions(a).count, 4); XCTAssertFalse(IslandChoiceLayout.hasMore(a))
        a.multi = true
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a), 220)
        a.options.append(.init(label: "5", value: "5"))
        XCTAssertEqual(IslandChoiceLayout.shownOptions(a).count, 3); XCTAssertTrue(IslandChoiceLayout.hasMore(a))
        XCTAssertEqual(IslandChoiceLayout.optionRows(a), 2)
        a.allow_text = true
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a, textLines: 1), 220)
        XCTAssertEqual(IslandChoiceLayout.cardHeight(a, textLines: 3), 220 - 12 + 34, "field grows 1→3 lines")
        XCTAssertEqual(IslandChoiceLayout.textLines("a\nb\nc\nd"), 3)
        XCTAssertEqual(IslandChoiceLayout.footerHeight(lines: 1), 32)
        XCTAssertEqual(IslandChoiceLayout.sendSize, 32); XCTAssertEqual(IslandChoiceLayout.fieldRadius, 16)
        XCTAssertEqual(IslandChoiceLayout.bottomPadding, 16)
    }

    func testMultiSelectDangerConfirmFlipAndCount() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        _ = post(c.hub, ["source": "grok-bot", "id": "d1", "kind": "choice", "prompt": "Deploy where?", "multi": true,
                         "danger": "prod", "options": [["label": "Staging", "value": "stg"], ["label": "Dev", "value": "dev"],
                                                       ["label": "Canary", "value": "can"], ["label": "Production", "value": "prod"]]])
        m.tapOption("stg"); m.tapOption("prod")
        XCTAssertEqual(m.choiceSelected, ["stg", "prod"])
        XCTAssertTrue(m.choiceIncludesDanger)
        XCTAssertFalse(m.submitChoice(), "first press flips to Confirm")
        XCTAssertTrue(m.confirmPending)
        XCTAssertTrue(m.submitChoice())
        try await Task.sleep(nanoseconds: 50_000_000)
        let ans = c.hub.answer(IslandHub.key(.grokBot, "d1"))
        XCTAssertEqual(ans?.values, ["stg", "prod"], "card order")
    }

    func testSingleDangerNeedsSecondClickAndOptionPlusTextSendTogether() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        _ = post(c.hub, choice("t1", extra: ["allow_text": true, "danger": "v2"]))
        m.tapOption("v2")
        XCTAssertNotNil(m.agentApproval, "danger: first click only selects (turns red)")
        m.choiceText = "because"
        m.tapOption("v1")
        try await Task.sleep(nanoseconds: 50_000_000)
        let ans = c.hub.answer(IslandHub.key(.grokBot, "t1"))
        XCTAssertEqual(ans?.values, ["v1"]); XCTAssertEqual(ans?.text, "because")
    }

    // MARK: P2 — keys

    func testClickOpenTakesKeyAndKeysWork() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        c.debugPanel.debugEnsurePanel()
        let m = c.debugModel
        m.peekSuppressed = { _ in true }
        _ = post(c.hub, choice("k1", n: 3, extra: ["allow_text": true]))
        XCTAssertFalse(m.cardOpen)
        m.click(at: CGPoint(x: 40, y: 10))
        XCTAssertTrue(m.cardOpen); XCTAssertTrue(m.textEntryActive)
        XCTAssertTrue(c.debugPanel.debugCanBecomeKey, "click-to-open makes the card key")
        XCTAssertTrue(m.fieldFocusRequested)
        XCTAssertTrue(m.handleKey(keyCode: 19, characters: "2", shift: false))
        XCTAssertEqual(m.choiceSelected, ["v2"])
        XCTAssertFalse(m.handleKey(keyCode: 21, characters: "9", shift: false), "no 9th option")
        m.choiceText = "x"
        XCTAssertFalse(m.handleKey(keyCode: 18, characters: "1", shift: false), "digits type once text started")
        XCTAssertTrue(m.handleKey(keyCode: 36, characters: "\r", shift: true))
        XCTAssertEqual(m.choiceText, "x\n"); XCTAssertEqual(m.choiceTextLines, 2)
        XCTAssertTrue(m.handleKey(keyCode: 53, characters: "\u{1b}", shift: false))
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "Esc gives focus back")
        XCTAssertTrue(m.cardOpen, "card stays")
        m.beginTextEntry()
        XCTAssertTrue(m.handleKey(keyCode: 36, characters: "\r", shift: false))
        try await Task.sleep(nanoseconds: 50_000_000)
        let ans = c.hub.answer(IslandHub.key(.grokBot, "k1"))
        XCTAssertEqual(ans?.values, ["v2"]); XCTAssertEqual(ans?.text, "x")
        XCTAssertEqual(names("answered").last?["via"] as? String, "island-key")
    }

    // MARK: P2 — queue, circles, ghost stack, drafts

    func testApprovalsJumpQueueAndCirclesPerSource() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        _ = post(c.hub, choice("c1", source: "grok-bot"))
        _ = post(c.hub, choice("c2", source: "cursor"))
        XCTAssertEqual(post(c.hub, approval("a1", source: "codex")).body["error"], nil)
        let s = c.hub.compose()
        XCTAssertEqual(s.agent_approval?.id, IslandHub.key(.codex, "a1"), "approval jumps the queue")
        XCTAssertEqual(s.pending_count, 3)
        XCTAssertEqual(m.sourceGroups.map(\.id), ["Grok Bot", "Cursor"], "pill's own card adds no circle")
        XCTAssertEqual(m.sourceGroups.map(\.needsYou), [1, 1])
        XCTAssertEqual(m.hitRects.count, 3, "pill + 2 source circles")
        let g = IslandSourceGroups.groups(agents: ["A", "B", "C", "D", "E"].map { IslandAgent(id: $0, name: $0, app: $0, status: "running") },
                                          expired: [], hidePending: nil)
        XCTAssertEqual(g.count, 4); XCTAssertEqual(g.last?.overflow, 2, "max 3 + \"+N\"")
    }

    func testDraftSurvivesPreemptionByApproval() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        _ = post(c.hub, choice("p1", extra: ["allow_text": true]))
        m.choiceText = "half typed"
        _ = post(c.hub, approval("a1"))
        XCTAssertEqual(m.agentApproval?.kind, "approval")
        XCTAssertEqual(m.choiceText, "")
        c.hub.resolveApproval(id: IslandHub.key(.codex, "a1"), approved: true)
        XCTAssertEqual(m.agentApproval?.id, IslandHub.key(.grokBot, "p1"))
        XCTAssertEqual(m.choiceText, "half typed")
    }

    func testNextCardSlidesInWhenGuruWasInTheCard() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        m.peekSuppressed = { _ in true }
        _ = post(c.hub, choice("n1")); _ = post(c.hub, choice("n2"))
        m.click(at: .zero)
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.pendingCount, 2, "ghost stack shows 1 layer")
        m.tapOption("v1")
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(m.agentApproval?.id, IslandHub.key(.grokBot, "n2"))
        XCTAssertTrue(m.cardOpen, "next card slides in")
        XCTAssertFalse(c.debugPanel.debugCanBecomeKey, "but key went back")
    }

    // MARK: P2 — expiry

    func testFinalMinuteCountdownExpiryNoticeAndHollowRing() {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start(); defer { c.shutdown() }
        let m = c.debugModel
        let t0 = Date()
        _ = post(c.hub, choice("e1", extra: ["ttl_s": 90]), now: t0)
        m.tick(now: t0.addingTimeInterval(10))
        XCTAssertNil(m.cardRemaining)
        m.tick(now: t0.addingTimeInterval(45))
        XCTAssertTrue((44...46).contains(m.cardRemaining ?? 0), "final 60 s ring")
        c.hub.expire(now: t0.addingTimeInterval(91))
        XCTAssertNil(m.agentApproval)
        XCTAssertEqual(m.presentation, .expired)
        XCTAssertTrue(m.cardOpen, "4 s notice")
        XCTAssertEqual(m.card.subtitle, "Timed out. Still open in chat")
        m.tick(now: Date().addingTimeInterval(5))
        XCTAssertNotEqual(m.presentation, .expired)
        XCTAssertEqual(m.sourceGroups.first?.expired, true, "hollow ring persists")
        XCTAssertEqual(c.hub.compose().expired.count, 1)
        m.openAgentList()
        XCTAssertTrue(m.listRows.contains { $0.status == "expired" })
        m.dismissCard()
        XCTAssertEqual(c.hub.compose().expired.count, 0, "acknowledged after the list was seen")
        XCTAssertTrue(m.sourceGroups.isEmpty)
    }

    func testClearRemovesNoticeAndNoticesPersist() {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/acu-rv-\(UUID().uuidString.prefix(6))")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("p.json")
        let hub = IslandHub(store: store); hub.persistURL = url
        let t0 = Date()
        _ = post(hub, choice("x1", extra: ["ttl_s": 30]), now: t0)
        _ = post(hub, choice("x2", extra: ["ttl_s": 30]), now: t0)
        hub.expire(now: t0.addingTimeInterval(31))
        XCTAssertEqual(hub.expiredNotices.count, 2)
        _ = post(hub, ["source": "grok-bot", "id": "x1", "kind": "clear"])
        XCTAssertEqual(hub.expiredNotices.map(\.id), [IslandHub.key(.grokBot, "x2")])
        let again = IslandHub(store: store); again.persistURL = url
        XCTAssertEqual(again.expiredNotices.count, 1, "never dropped by a restart")
        // A card that expired while CUAService was down also gets its notice.
        _ = post(again, choice("x3", extra: ["ttl_s": 30]), now: t0)
        let later = IslandHub(store: store)
        later.persistURL = url
        XCTAssertTrue(later.expiredNotices.contains { $0.id == IslandHub.key(.grokBot, "x3") } || later.decision(IslandHub.key(.grokBot, "x3")) == .pending)
    }

    // MARK: P2 — Duo

    func testDuoWithMusicForChoiceApprovalPreempts() {
        let m = IslandModel()
        m.setNowPlaying(NowPlayingTests.track(), visible: true)
        XCTAssertEqual(m.presentation, .nowPlaying)
        m.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "c", agent: "Grok Bot", app: "Comet", title: "Q", kind: "choice",
                                                                                 options: [.init(label: "A", value: "a")])))
        XCTAssertEqual(m.presentation, .agentApproval)
        XCTAssertTrue(m.duoWithMedia, "music lead + choice trail")
        m.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "a", agent: "Codex", app: "Codex", title: "Run?")))
        XCTAssertFalse(m.duoWithMedia, "approval preempts music")
        XCTAssertTrue(m.cardOpen, "approval card is sticky")
    }

    // MARK: P1 — Now Playing

    func testNowPlayingActionSlotAndWaveColour() {
        var t = NowPlayingTests.track()
        XCTAssertEqual(NowPlayingCardBody.actionItem(t, action: "favorite")?.id, "favorite", "Music: Like available")
        XCTAssertEqual(NowPlayingCardBody.actionItem(t, action: "shuffle")?.id, "favorite", "shuffle cut")
        t.bundleID = NowPlayingRules.spotifyBundle
        XCTAssertNil(NowPlayingCardBody.actionItem(t, action: "favorite"), "no Like → no action slot")
        XCTAssertEqual(NowPlayingCardBody.scrubTrack, 4); XCTAssertEqual(NowPlayingCardBody.scrubHit, 20)
        let dark = NSColor(srgbRed: 0.2, green: 0.0, blue: 0.4, alpha: 1)
        let lifted = ArtworkColor.luminanceFloor(dark, 0.6)
        XCTAssertGreaterThanOrEqual(ArtworkColor.luminance(lifted), 0.6 - 0.001)
        XCTAssertLessThan(ArtworkColor.luminance(lifted), 0.62)
        let img = NSImage(size: NSSize(width: 16, height: 16))
        img.lockFocus(); NSColor(srgbRed: 0.9, green: 0.1, blue: 0.1, alpha: 1).setFill(); NSRect(x: 0, y: 0, width: 16, height: 16).fill()
        NSColor.gray.setFill(); NSRect(x: 0, y: 0, width: 16, height: 4).fill(); img.unlockFocus()
        let d = ArtworkColor.dominant(img)?.usingColorSpace(.sRGB)
        XCTAssertNotNil(d); XCTAssertGreaterThan(d?.redComponent ?? 0, 0.7, "dominant = the red, not the grey")
    }
}
