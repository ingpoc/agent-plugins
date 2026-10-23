import XCTest
@testable import CUAService

@MainActor
final class IslandTests: XCTestCase {
    // MARK: - Pill display choice (multi-display)

    func testPillPinsToPrimaryDisplayEvenWhenFocusIsElsewhere() {
        XCTAssertEqual(MenuBarWindowAnchor.pickScreenIndex(count: 2, mainIndex: 1, hasMenubar: { _ in true }), 0)
    }

    func testPillFallsBackToFocusedThenAnyMenubarDisplay() {
        XCTAssertEqual(MenuBarWindowAnchor.pickScreenIndex(count: 3, mainIndex: 2, hasMenubar: { $0 != 0 }), 2)
        XCTAssertEqual(MenuBarWindowAnchor.pickScreenIndex(count: 3, mainIndex: nil, hasMenubar: { $0 == 1 }), 1)
        XCTAssertEqual(MenuBarWindowAnchor.pickScreenIndex(count: 2, mainIndex: 1, hasMenubar: { _ in false }), 0)
    }

    // MARK: - Geometry (§1): fixed window, three compact widths, one card

    func testCompactWidthsAreOnly74_136_200() {
        let all: [IslandPresentation] = [.idle, .listening, .thinking, .speaking, .acting, .actingSteps,
                                         .needsYou, .error, .done, .doneSettled, .agentApproval, .reconnecting]
        XCTAssertEqual(Set(all.map(\.compactWidth)), [74, 136, 200])
        XCTAssertEqual(IslandPresentation.idle.compactWidth, 74)
        XCTAssertEqual(IslandPresentation.actingSteps.compactWidth, 200)
        XCTAssertEqual(IslandPresentation.listening.compactWidth, 136)
        XCTAssertEqual(IslandBarLayout.cardSize, CGSize(width: 340, height: 176))
        XCTAssertEqual(IslandBarLayout.cardRadius, 36)
        XCTAssertEqual(IslandBarLayout.compactRadius, 12)
    }

    func testNothingClippedInsideFixedWindow() {
        let bounds = CGRect(origin: .zero, size: IslandBarLayout.windowSize)
        for card in [false, true] {
            for w in [CGFloat(74), 136, 200] {
                let shape = IslandBarLayout.shapeRect(compactWidth: w, card: card)
                XCTAssertEqual(shape.minY, 3, "3 pt top gap")
                XCTAssertEqual(shape.midX, bounds.midX)
                for count in [0, 1, 3, 4, 9] {
                    for r in IslandBarLayout.hitRects(shape: shape, circleCount: count, hasAgents: true) {
                        XCTAssertTrue(bounds.contains(r), "card=\(card) w=\(w) n=\(count) \(r)")
                    }
                    for r in IslandBarLayout.circleRects(shape: shape, count: count) {
                        XCTAssertEqual(r.minY, shape.minY, "circles top-aligned at y=3")
                    }
                }
            }
        }
        let shape = IslandBarLayout.shapeRect(compactWidth: 136, card: false)
        XCTAssertEqual(IslandBarLayout.circleRects(shape: shape, count: 2)[0].minX, shape.maxX + 6)
        XCTAssertEqual(IslandBarLayout.agentRect(shape: shape).maxX, shape.minX - 6)
    }

    func testWindowFrameTopCentredAndInsideScreen() {
        let screen = CGRect(x: 0, y: 0, width: 2560, height: 1440)
        let bar = CGRect(x: 0, y: 1410, width: 2560, height: 30)
        let f = IslandBarLayout.windowFrame(bar: bar, screen: screen)
        XCTAssertEqual(f.size, IslandBarLayout.windowSize)
        XCTAssertEqual(f.maxY, screen.maxY)
        XCTAssertEqual(f.midX, bar.midX, accuracy: 1)
        let right = CGRect(x: 2560, y: 287, width: 1920, height: 1080)
        let g = IslandBarLayout.windowFrame(bar: CGRect(x: 2560, y: 1337, width: 1920, height: 30), screen: right)
        XCTAssertGreaterThanOrEqual(g.minX, right.minX)
        XCTAssertLessThanOrEqual(g.maxX, right.maxX)
        let small = CGRect(x: 0, y: 0, width: 500, height: 400)
        let h = IslandBarLayout.windowFrame(bar: CGRect(x: -100, y: 370, width: 500, height: 30), screen: small)
        XCTAssertGreaterThanOrEqual(h.minX, 0)
    }

    func testPointerMapping() {
        let frame = CGRect(x: 960, y: 1120, width: 640, height: 320)
        XCTAssertEqual(SamanthaMenuBarIslandPanel.windowPoint(screen: CGPoint(x: 1280, y: 1440 - 15), frame: frame),
                       CGPoint(x: 320, y: 15))
    }

    // MARK: - IslandSnapshot decoding

    func testDecodeMissingFieldsFallBackToDefaultsAndExtraKeysIgnored() throws {
        let json = #"{"kind":"acting","title":"Acting","updated_at":1.5}"#
        let snap = try JSONDecoder().decode(
            IslandSnapshot.self,
            from: Data(json.utf8)
        )
        XCTAssertEqual(snap.kind, "acting")
        XCTAssertEqual(snap.title, "Acting")
        XCTAssertEqual(snap.detail, "")
        XCTAssertEqual(snap.voice_levels, [])
        XCTAssertEqual(snap.active_apps, [])
        XCTAssertEqual(snap.voice_side, "idle")
        XCTAssertEqual(snap.voice_level, 0)
    }

    // MARK: - sameVisual

    func testSameVisualIgnoresVoiceMeters() {
        let base = IslandSnapshot(
            kind: "listening",
            title: "Listening",
            voice_side: "left",
            voice_level: 0.4,
            voice_levels: [0.1, 0.2]
        )
        let metered = IslandSnapshot(
            kind: "listening",
            title: "Listening",
            voice_side: "right",
            voice_level: 0.9,
            voice_levels: [0.8, 0.9]
        )
        XCTAssertTrue(base.sameVisual(as: metered))

        let differentDetail = IslandSnapshot(kind: "listening", title: "Listening", detail: "ready")
        XCTAssertFalse(base.sameVisual(as: differentDetail))
    }

    // MARK: - IslandSSEParser

    func testParserDrainsCompleteEventsAndKeepsPartial() {
        var buffer = ": ping\n\n"
            + "data: {\"kind\":\"listening\",\"title\":\"Listening\"}\n\n"
            + "data: {\"kind\":\"thinking\"}\n\n"
            + "data: {\"kind\":"
        let snaps = IslandSSEParser.drain(&buffer)
        XCTAssertEqual(snaps.count, 2)
        XCTAssertEqual(snaps[0].kind, "listening")
        XCTAssertEqual(snaps[1].kind, "thinking")
        XCTAssertEqual(buffer, "data: {\"kind\":")
    }

    func testParserSkipsInvalidJSON() {
        var buffer = "data: {not json}\n\ndata: {\"kind\":\"done\"}\n\n"
        let snaps = IslandSSEParser.drain(&buffer)
        XCTAssertEqual(snaps.count, 1)
        XCTAssertEqual(snaps[0].kind, "done")
        XCTAssertTrue(buffer.isEmpty)
    }

    func testParserHandlesCRLFEvents() {
        var buffer = "data: {\"kind\":\"done\"}\r\n\r\n"
        let snaps = IslandSSEParser.drain(&buffer)
        XCTAssertEqual(snaps.count, 1)
        XCTAssertEqual(snaps[0].kind, "done")
    }

    // MARK: - Copy contract

    func testTitleCapMovesOverflowToDetailWithoutEllipsis() {
        let r = IslandCopy.compact(title: "Searching for the best pizza places near me", detail: "Comet")
        XCTAssertLessThanOrEqual(r.title.count, 28)
        XCTAssertEqual(r.title, "Searching for the best pizza")
        XCTAssertEqual(r.detail, "places near me · Comet")
        XCTAssertFalse(r.title.contains("…"))
        XCTAssertEqual(IslandCopy.compact(title: "Opening netflix.com", detail: "Comet").title, "Opening netflix.com")
        let dotted = IslandCopy.compact(title: "Self-check · example.com and more words here", detail: "")
        XCTAssertEqual(dotted.title, "Self-check")
        XCTAssertEqual(dotted.detail, "example.com and more words here")
    }

    func testCompactHasNoTextButAXHasFullWords() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "acting", title: "Opening netflix.com", detail: "Comet", app: "Comet"))
        XCTAssertEqual(m.presentation, .acting)
        XCTAssertTrue(m.accessibilityText.contains("Opening netflix.com"))
        XCTAssertTrue(m.accessibilityText.contains("working"))
        XCTAssertEqual(m.card.title, "Opening netflix.com", "words appear only in the card")
    }

    func testErrorMappingAndBenignSuppression() {
        XCTAssertNil(IslandCopy.humanError("Audio content of 3200ms is already shorter than 4000ms"))
        XCTAssertEqual(IslandCopy.humanError("label not found: Sign In", target: "Calculator"), "Couldn't click Calculator")
        XCTAssertEqual(IslandCopy.humanError("websocket connection closed"), "Connection dropped, reconnecting")
        XCTAssertEqual(IslandCopy.humanError("Traceback (most recent call last): KeyError 'x'"), "Something went wrong")
        XCTAssertEqual(IslandCopy.humanError("Couldn't close Comet"), "Couldn't close Comet")
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "listening", title: "Listening"))
        XCTAssertFalse(m.apply(IslandSnapshot(kind: "error", title: "Realtime error",
                                              detail: "Audio content of 10ms is already shorter than 20ms")))
        XCTAssertEqual(m.kind, "listening", "benign error never shows")
        m.apply(IslandSnapshot(kind: "error", title: "Failed", detail: "RPC -32000 something raw"))
        XCTAssertEqual(m.title, "Something went wrong")
        XCTAssertEqual(m.detail, "", "raw text is logged, not shown")
    }

    // MARK: - States, precedence, auto-peek (§4, §6)

    func testPresentationAndPrecedence() {
        let t0 = Date()
        func p(_ k: String, steps: Int = 0, approval: IslandAgentApproval? = nil, after: TimeInterval = 0) -> IslandPresentation {
            IslandModel.presentation(kind: k, stepTotal: steps, approval: approval, since: t0, now: t0.addingTimeInterval(after))
        }
        XCTAssertEqual(p("listening"), .listening)
        XCTAssertEqual(p("acting", steps: 5), .actingSteps)
        XCTAssertEqual(p("confirm"), .needsYou)
        XCTAssertEqual(p("done"), .done)
        XCTAssertEqual(p("done", after: 4.1), .doneSettled, "done → 74 after 4 s")
        XCTAssertEqual(p("speaking", approval: IslandAgentApproval(id: "a", title: "Approve?")), .agentApproval,
                       "agent approval takes the pill")
    }

    func testReconnectingStaysVisibleAboveActivitiesAndPeeks() {
        let now = Date()
        for activity in [IslandLiveActivity.media, .calendar, .tray] {
            XCTAssertEqual(IslandModel.presentation(kind: "reconnecting", stepTotal: 0, approval: nil,
                since: now, now: now, hud: true, peek: true, live: [activity]), .reconnecting)
        }
        let approval = IslandAgentApproval(id: "a", title: "Approve?")
        XCTAssertEqual(IslandModel.presentation(kind: "reconnecting", stepTotal: 0, approval: approval,
            since: now, now: now), .agentApproval, "approval must retain its controls")
    }

    func testSparseCardsFitContentAndExpiredHasNamedRecovery() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "thinking", title: "Check the calendar"))
        XCTAssertEqual(m.cardHeight, 136)
        XCTAssertEqual(m.cardControlsY + IslandBarLayout.cardControlsHeight + IslandBarLayout.cardPadding, m.cardHeight)
        m.apply(IslandSnapshot(kind: "speaking", title: "Here is your agenda"))
        XCTAssertEqual(m.cardHeight, 176, "meter retains its space")
        let c = IslandModel.card(for: .init(presentation: .expired, title: "", detail: "", app: "Codex", stepIndex: 0, stepTotal: 0,
            approval: nil, notice: IslandExpired(id: "x", app: "Codex")))
        guard case .decision(_, let primary, _) = c.controls else { return XCTFail("named recovery action required") }
        XCTAssertEqual(primary.0, "Open in chat")
        XCTAssertEqual(primary.1, .open("Codex"))
    }

    func testDismissExpiredNoticeKeepsItsSourceReminder() {
        let m = IslandModel()
        let expired = IslandExpired(id: "x", agent: "Codex", app: "Codex", title: "Review changes", at: Date().timeIntervalSince1970)
        var snap = IslandSnapshot(kind: "idle")
        snap.expired = [expired]
        m.apply(snap)
        XCTAssertEqual(m.presentation, .expired)
        m.dismissCard()
        XCTAssertNil(m.notice)
        XCTAssertEqual(m.expired, [expired], "dismiss the card, keep the source reminder")
    }

    func testApprovalRetainsSourceAndProvidedScope() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "a", agent: "Codex", app: "Codex",
            title: "Deploy to staging?", subtitle: "Website · release v1.4")))
        XCTAssertEqual(m.card.subtitle, "Codex · Website · release v1.4")
        XCTAssertEqual(m.cardHeight, 152)
    }

    func testUncertainActionWithDetailNeverOffersRetry() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "error", title: "Action not confirmed", detail: "The action may have happened. Check the app before trying again.", app: "Calendar"))
        XCTAssertEqual(m.card.title, "Action not confirmed")
        guard case .decision(_, let primary, _) = m.card.controls else { return XCTFail("check the app before retrying") }
        XCTAssertEqual(primary.0, "Check app")
        XCTAssertEqual(primary.1, .show("Calendar"))
    }

    func testConsentGrowsForFullScopeAndReconnectHasNamedExit() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "confirm", title: "Allow saved sign-in?", detail: String(repeating: "W", count: 160), app: "Comet"))
        XCTAssertGreaterThan(m.cardHeight, 176)
        XCTAssertLessThan(m.cardHeight, IslandBarLayout.windowSize.height)
        m.apply(IslandSnapshot(kind: "reconnecting"))
        guard case .decision(let secondary, _, _) = m.card.controls else { return XCTFail("named exit required") }
        XCTAssertEqual(secondary.0, "Turn off")
        XCTAssertEqual(secondary.1, .stopListening)
    }

    func testDestructiveControlsHaveReadableContrast() {
        let color = NSColor(IslandPalette.destructiveFill).usingColorSpace(.sRGB)!
        func luminance(_ values: [CGFloat]) -> Double {
            let linear = values.map { Double($0) }.map { $0 <= 0.04045 ? $0 / 12.92 : pow(($0 + 0.055) / 1.055, 2.4) }
            return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
        }
        for opacity in [CGFloat(1), 0.75] {
            let bg = luminance([color.redComponent * opacity, color.greenComponent * opacity, color.blueComponent * opacity])
            let fg = luminance([opacity, opacity, opacity])
            XCTAssertGreaterThanOrEqual((fg + 0.05) / (bg + 0.05), 4.5)
        }
    }

    func testAutoPeekRules() {
        let m = IslandModel()
        let t0 = Date()
        m.apply(IslandSnapshot(kind: "listening", title: "Listening"), now: t0)
        XCTAssertFalse(m.cardOpen, "listening never auto-opens")
        m.apply(IslandSnapshot(kind: "error", title: "label not found", app: "Calculator"), now: t0)
        XCTAssertTrue(m.cardOpen, "error peeks")
        m.tick(now: t0.addingTimeInterval(6.1))
        XCTAssertFalse(m.cardOpen, "error peek lasts 6 s")
        m.apply(IslandSnapshot(kind: "done", title: "Done", detail: "cua act ok"), now: t0.addingTimeInterval(7))
        XCTAssertFalse(m.cardOpen, "done without a result value doesn't peek")
        m.apply(IslandSnapshot(kind: "done", title: "84", detail: "12 × 7", app: "Calculator"), now: t0.addingTimeInterval(8))
        XCTAssertTrue(m.cardOpen)
        m.tick(now: t0.addingTimeInterval(12.1))
        XCTAssertFalse(m.cardOpen, "done peek 4 s")
        m.apply(IslandSnapshot(kind: "confirm", title: "Confirm", confirm_id: "c1", confirm_prompt: "Allow Keychain access?"),
                now: t0.addingTimeInterval(13))
        m.tick(now: t0.addingTimeInterval(600))
        XCTAssertTrue(m.cardOpen, "needs-you is sticky")
        m.pointerLeft()
        XCTAssertTrue(m.cardOpen, "mouse-leave never closes a sticky card")
    }

    func testCardContentPerState() {
        func card(_ p: IslandPresentation, title: String = "", detail: String = "", app: String = "",
                  i: Int = 0, n: Int = 0, approval: IslandAgentApproval? = nil) -> IslandCard {
            IslandModel.card(for: .init(presentation: p, title: title, detail: detail, app: app,
                                        stepIndex: i, stepTotal: n, approval: approval))
        }
        let acting = card(.actingSteps, title: "Opening netflix.com", app: "Comet", i: 2, n: 5)
        XCTAssertEqual(acting.subtitle, "Comet · step 2 of 5")
        XCTAssertEqual(acting.meta, .progress(0.4, "2/5"))
        XCTAssertEqual(card(.needsYou, title: "Allow Keychain access?").controls,
                       .decision(secondary: ("Deny", .deny), primary: ("Allow", .allow), destructive: false))
        let done = card(.done, title: "84", detail: "12 × 7", app: "Calculator")
        XCTAssertEqual(done.title, "84")
        XCTAssertEqual(done.subtitle, "12 × 7 · Calculator")
        XCTAssertEqual(done.art, .appDone("Calculator"))
        let ask = card(.agentApproval, approval: IslandAgentApproval(id: "m1", agent: "Codex", app: "Codex",
                                                                     title: "Approve migration?", subtitle: "Codex · repo · 4m ago",
                                                                     destructive: true))
        XCTAssertEqual(ask.controls, .decision(secondary: ("Deny", .agentDeny("m1")), primary: ("Approve", .agentApprove("m1")),
                                               destructive: true))
        XCTAssertEqual(card(.error, title: "Couldn't click Calculator").art, .tile(symbol: "xmark", tint: "red"))
        XCTAssertLessThanOrEqual(card(.done, title: "Pizza Express is open now near you tonight").title.count, 28,
                                 "copy contract on the card title")
    }

    func testMetersResampleAndThrottle() {
        XCTAssertEqual(IslandModel.resample([0, 0.5, 1, 0.5, 0, 0.2, 0.4, 0.6], to: 5, fallback: 0).count, 5)
        XCTAssertEqual(IslandModel.resample([], to: 5, fallback: 0.3), Array(repeating: 0.3, count: 5))
        let m = IslandModel()
        let t0 = Date()
        m.applyMeter(IslandSnapshot(voice_level: 0.9), now: t0, force: true)
        m.applyMeter(IslandSnapshot(voice_level: 0.1), now: t0.addingTimeInterval(0.05))
        XCTAssertEqual(m.meter24.last, 0.9, "≤ 10 Hz")
        m.applyMeter(IslandSnapshot(voice_level: 0.1), now: t0.addingTimeInterval(0.11))
        XCTAssertEqual(m.meter24.last, 0.1)
    }

    func testSnapshotDecodesAgentsAndApproval() throws {
        let json = #"{"kind":"listening","agents":[{"id":"a","name":"Codex","app":"Codex","needs_you":true}],"agent_approval":{"id":"m1","title":"Approve migration?"},"step_index":2,"step_total":5}"#
        let snap = try JSONDecoder().decode(IslandSnapshot.self, from: Data(json.utf8))
        XCTAssertEqual(snap.agents.first?.needs_you, true)
        XCTAssertEqual(snap.agent_approval?.id, "m1")
        XCTAssertEqual(snap.step_total, 5)
        let empty = try JSONDecoder().decode(IslandSnapshot.self, from: Data(#"{"agent_approval":{}}"#.utf8))
        XCTAssertNil(empty.agent_approval)
    }

    // MARK: - Orphaned voice helper fix

    func testResumeMarkerFreshStaleAndConsumedOnce() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("resume-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("samantha-resume.json")
        let t0 = Date()
        XCTAssertFalse(SamanthaResume.consume(at: url, now: t0), "no marker → don't start Samantha")
        SamanthaResume.write(at: url, now: t0)
        XCTAssertTrue(SamanthaResume.consume(at: url, now: t0.addingTimeInterval(10)))
        XCTAssertFalse(SamanthaResume.consume(at: url, now: t0.addingTimeInterval(11)), "consumed once")
        SamanthaResume.write(at: url, now: t0)
        XCTAssertFalse(SamanthaResume.consume(at: url, now: t0.addingTimeInterval(301)), "stale after 5 min")
    }

    // MARK: - App circles

    func testOverflowShowsThreeNewestFirstPlusN() {
        let t0 = Date()
        let apps = ["A", "B", "C", "D", "E"]
        let last = Dictionary(uniqueKeysWithValues: apps.map { ($0, t0) })
        let items = IslandCircles.items(apps: apps, lastActive: last, driving: "E", now: t0)
        XCTAssertEqual(items, [
            .app(name: "E", dimmed: false, driving: true),
            .app(name: "D", dimmed: false, driving: false),
            .app(name: "C", dimmed: false, driving: false),
            .overflow(2),
        ])
        XCTAssertEqual(IslandBarLayout.circleSlots(count: 5), 4)
        XCTAssertEqual(IslandBarLayout.circleSlots(count: 3), 3)
    }

    func testFinishedAppsDimAfter10sAndDropAfter60s() {
        let t0 = Date()
        let last = ["Old": t0.addingTimeInterval(-61), "Mid": t0.addingTimeInterval(-11), "New": t0]
        let items = IslandCircles.items(apps: ["Old", "Mid", "New"], lastActive: last, driving: "", now: t0)
        XCTAssertEqual(items, [.app(name: "New", dimmed: false, driving: false),
                               .app(name: "Mid", dimmed: true, driving: false)])
    }

    func testIconFallbackIsNeverBlank() {
        // Unknown app → no bogus path → symbol, never the generic white document.
        XCTAssertNil(AppIconResolver.appURL(for: "Definitely Not An App 12345"))
        if case .image = AppIconResolver.resolve("Definitely Not An App 12345") {
            XCTFail("unknown app must fall back to a symbol")
        }
        if case .symbol(let s) = AppIconResolver.resolve("Definitely Not An App 12345") {
            XCTAssertEqual(s, AppIconResolver.fallbackSymbol)
        }
        // The generic document icon (what icon(forFile:) returns for a bad path) is blank.
        let generic = NSWorkspace.shared.icon(forFile: "/nonexistent/Nope.app")
        XCTAssertTrue(AppIconResolver.isBlank(generic))
        // A real, colourful app icon is kept.
        if let calc = AppIconResolver.appURL(for: "Calculator") {
            XCTAssertFalse(AppIconResolver.isBlank(NSWorkspace.shared.icon(forFile: calc.path)))
        }
        // Whatever TextEdit resolves to, it is never a blank image.
        if case .image(let img) = AppIconResolver.resolve("TextEdit") {
            XCTAssertFalse(AppIconResolver.isBlank(img))
        }
    }

    // MARK: - IslandDwell

    func testDwellHoldsDoneForListening() {
        let since = Date()
        let incoming = Date().addingTimeInterval(0.5)
        XCTAssertTrue(IslandDwell.shouldHold(
            current: "done", since: since, incoming: "listening", now: incoming
        ))
    }

    func testDwellReleasesAfterHoldSeconds() {
        let since = Date()
        let later = since.addingTimeInterval(IslandDwell.holdSeconds + 0.5)
        XCTAssertFalse(IslandDwell.shouldHold(
            current: "done", since: since, incoming: "listening", now: later
        ))
    }

    func testDwellNeverHoldsForActingIncoming() {
        let since = Date()
        let incoming = since.addingTimeInterval(0.5)
        XCTAssertFalse(IslandDwell.shouldHold(
            current: "done", since: since, incoming: "acting", now: incoming
        ))
    }

    func testDwellNeverHoldsWhenCurrentIsListening() {
        let since = Date()
        let incoming = since.addingTimeInterval(0.5)
        XCTAssertFalse(IslandDwell.shouldHold(
            current: "listening", since: since, incoming: "idle", now: incoming
        ))
    }

    // MARK: - choosePort

    func testChoosePortKeepsPreferredWhenNotForeignBusy() {
        let port = VoiceSupervisor.choosePort(
            preferred: 8765,
            isForeignBusy: { _ in false },
            isListening: { _ in true }
        )
        XCTAssertEqual(port, 8765)
    }

    func testChoosePortSkipsForeignBusyPort() {
        let port = VoiceSupervisor.choosePort(
            preferred: 8765,
            isForeignBusy: { $0 == 8765 },
            isListening: { $0 == 8766 }
        )
        XCTAssertEqual(port, 8767)
    }

    func testChoosePortFallsBackToPreferredWhenAllBusy() {
        let port = VoiceSupervisor.choosePort(
            preferred: 8765,
            isForeignBusy: { _ in true },
            isListening: { _ in true }
        )
        XCTAssertEqual(port, 8765)
    }

    func testSSEUTF8SurvivesByteStream() {
        let wire = "data: {\"kind\":\"error\",\"title\":\"Stopped — you said no\"}\n\n"
        var raw: [UInt8] = []
        var titles: [String] = []
        for b in Array(wire.utf8) {
            raw.append(b)
            guard IslandSSEParser.isEventBoundary(raw) else { continue }
            var buffer = String(decoding: raw, as: UTF8.self)
            raw.removeAll()
            titles += IslandSSEParser.drain(&buffer).map(\.title)
        }
        XCTAssertEqual(titles, ["Stopped — you said no"])
    }

    @MainActor
    func testIdlePillHasNoCardButOpensAgentList() {
        let m = IslandModel()
        _ = m.apply(IslandSnapshot(kind: "ready", title: "Samantha"))
        m.toggleCard()
        XCTAssertFalse(m.cardOpen, "no idle card")
        _ = m.apply(IslandSnapshot(kind: "ready", title: "Samantha",
                                   agents: [IslandAgent(id: "a", name: "Codex", app: "Codex", status: "running")]))
        m.toggleCard()
        XCTAssertTrue(m.cardOpen, "idle click opens the agent list when agents are live")
    }

    func testDoneValueTitleAndDestructiveFlag() {
        func card(_ p: IslandPresentation, title: String = "", detail: String = "", app: String = "",
                  approval: IslandAgentApproval? = nil) -> IslandCard {
            IslandModel.card(for: .init(presentation: p, title: title, detail: detail, app: app,
                                        stepIndex: 0, stepTotal: 0, approval: approval))
        }
        XCTAssertTrue(card(.done, title: "84", detail: "12 × 7", app: "Calculator").valueTitle)
        XCTAssertFalse(card(.done, title: "Netflix opened", app: "Comet").valueTitle, "text results stay 15 pt")
        XCTAssertTrue(IslandModel.isValueResult("₹1,240"))
        XCTAssertFalse(IslandModel.isValueResult("Pizza Express 2"))
        let safe = card(.agentApproval, approval: IslandAgentApproval(id: "s", agent: "Cursor", app: "Cursor", title: "Run tests?"))
        if case .decision(_, _, let d) = safe.controls { XCTAssertFalse(d, "Approve is white unless destructive") } else { XCTFail() }
        XCTAssertEqual(IslandBarLayout.decisionButton.width * 2 + IslandBarLayout.decisionGap,
                       IslandBarLayout.cardSize.width - 2 * IslandBarLayout.cardPadding, "full-width 2×148 + 12")
    }
}
