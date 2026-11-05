import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// 0.2.87 (Guru, 2026-11-04 13:27 IST "why do i see app icon still on the right side of the island"): ONE layout
/// rule in every compact state, Samantha's voice states included. Outside left: harness circles. Inside: ringed
/// target outer edge · 24 · app icons · 8 · voice slot · 6 · dot · 9. Outside right: plugin marks only.
@MainActor
final class IslandVoiceAppsInsideTests: XCTestCase {
    static func acu(_ app: String, _ t: Double, agent: String = "Samantha") -> IslandPluginUse {
        var u = IslandPluginUse(plugin: "acu", app: "Agent Computer Use", agent: agent, last_seen_at: t)
        u.target_app = app
        return u
    }
    static func plugin(_ id: String, _ t: Double, agent: String = "Samantha") -> IslandPluginUse {
        IslandPluginUse(plugin: id, app: id, agent: agent, last_seen_at: t)
    }
    static let apps3 = ["Calculator", "TextEdit", "Notes"]
    static func uses(_ apps: [String]) -> [IslandPluginUse] { apps.enumerated().map { acu($0.element, 300 + Double($0.offset)) } }
    static let bars: [Double] = [0.3, 0.8, 0.5, 0.9, 0.4]

    static func voice(_ kind: String, apps: [String], plugins extra: [IslandPluginUse] = [], staleApps: Bool = true) -> IslandSnapshot {
        IslandSnapshot(kind: kind, title: kind.capitalized, app: kind == "done" ? (apps.last ?? "") : "",
                       active_apps: staleApps ? apps : [], voice_side: kind == "speaking" ? "assistant" : "user",
                       voice_level: 0.7, voice_levels: bars, plugins: uses(apps) + extra, samantha_on: true)
    }

    private func overlays(_ m: IslandModel) -> [IslandLayoutOverlay] {
        IslandLayoutLogger.overlays(model: m, shape: m.shapeRect, origin: .zero)
    }

    private func settle() async { try? await Task.sleep(nanoseconds: 350_000_000) }

    /// Static zones + exact gaps for one model state.
    private func assertZones(_ m: IslandModel, apps: Int, voice: IslandVoiceSlotContent?, _ label: String, file: StaticString = #filePath, line: UInt = #line) {
        let shape = m.shapeRect
        let ov = overlays(m)
        XCTAssertFalse(ov.contains { $0.kind == "app_circle" }, "\(label): no app circle anywhere", file: file, line: line)
        XCTAssertFalse(m.circles.contains { !$0.isPlugin && { if case .overflow = $0 { return false } else { return true } }($0) },
                       "\(label): outside right holds plugin marks only", file: file, line: line)
        for o in ov where o.expectedInsideIsland {
            let r = CGRect(x: o.rect.x, y: o.rect.y, width: o.rect.w, height: o.rect.h)
            XCTAssertTrue(shape.insetBy(dx: -0.5, dy: -0.5).contains(r), "\(label): \(o.id) inside the pill", file: file, line: line)
        }
        for o in ov where o.kind == "plugin_mark" || o.kind == "overflow_circle" {
            XCTAssertGreaterThanOrEqual(o.rect.x, Double(shape.maxX) + 2, "\(label): \(o.id) outside right", file: file, line: line)
        }
        for o in ov where o.kind == "harness_circle" {
            XCTAssertLessThanOrEqual(o.rect.x + o.rect.w, Double(shape.minX) - 2, "\(label): \(o.id) outside left", file: file, line: line)
        }
        XCTAssertTrue(m.showsTargetMark, "\(label): the ring is present", file: file, line: line)
        XCTAssertTrue(ov.contains { $0.kind == "target_ring" || $0.kind == "comet_circle" }, "\(label): ring event", file: file, line: line)
        XCTAssertEqual(ov.filter { $0.kind == "app_icon" }.count, apps, "\(label): \(apps) app icons inside", file: file, line: line)
        XCTAssertEqual(m.voiceSlotContent, voice, "\(label): voice slot", file: file, line: line)
        XCTAssertEqual(shape.width, IslandBarLayout.controlledPillWidth(apps: apps, voice: voice), "\(label): hug width", file: file, line: line)
        // ring · 24 · apps (4) · 8 · voice · 6 · dot · 9 (no voice: apps · 6 · dot; nothing after the ring: ring · 24 · dot)
        let ringMaxX = IslandBarLayout.targetRingOuterRect(shape: shape).maxX
        let icons = IslandBarLayout.operatedAppRects(shape: shape, count: apps)
        let dot = apps == 0 && voice == nil
            ? IslandBarLayout.targetOnlyDotRect(shape: shape)
            : IslandBarLayout.compactDotRect(shape: shape)
        var cursor = ringMaxX
        if let first = icons.first {
            XCTAssertEqual(first.minX - ringMaxX, 24, accuracy: 0.5, "\(label): 24 pt ring gap", file: file, line: line)
            cursor = icons.last!.maxX
        }
        if let voice {
            let v = IslandBarLayout.voiceSlotRect(shape: shape, apps: apps, controlled: true, content: voice)
            XCTAssertEqual(v.minX - cursor, apps > 0 ? 8 : 24, accuracy: 0.5, "\(label): voice slot after apps", file: file, line: line)
            XCTAssertEqual(v.width, IslandBarLayout.voiceSlotWidth(voice), "\(label): slot width", file: file, line: line)
            XCTAssertTrue(ov.contains { $0.kind == "voice_slot" && $0.expectedInsideIsland }, "\(label): voice slot logged inside", file: file, line: line)
            cursor = v.maxX
        }
        XCTAssertEqual(dot.minX - cursor, apps == 0 && voice == nil ? IslandBarLayout.operatedRingGap : 6, accuracy: 1, "\(label): dot after content", file: file, line: line)
        XCTAssertEqual(shape.maxX - dot.maxX, 9, accuracy: 0.001, file: file, line: line)
    }

    // MARK: - Static regression: the 13:27 IST screenshot

    func testVoiceStatesWithOneOrThreeAppsKeepRingAndAppsInside() async {
        for apps in [["Calculator"], Self.apps3] {
            for kind in ["listening", "thinking", "speaking"] {
                let m = IslandModel()
                m.idleActivity = "duo"
                m.apply(Self.voice(kind, apps: apps))
                await settle()
                assertZones(m, apps: apps.count, voice: IslandVoiceSlotContent(rawValue: kind), "\(kind) + \(apps.count) app(s)")
                XCTAssertEqual(m.circles, [.plugin(name: "Agent Computer Use", agents: ["Samantha"])], "\(kind): ACU mark only")
                XCTAssertTrue(m.sourceGroups.contains { $0.id == IslandSourceGroups.samantha }, "Samantha outside left")
            }
            // Acting and idle: same rule, no voice slot.
            for kind in ["acting", "idle"] {
                let m = IslandModel()
                m.idleActivity = "duo"
                m.apply(IslandSnapshot(kind: kind, title: "Using", app: apps.last!, active_apps: apps, plugins: Self.uses(apps), samantha_on: true))
                await settle()
                assertZones(m, apps: apps.count, voice: nil, "\(kind) + \(apps.count) app(s)")
            }
        }
    }

    func testMultiStepActingKeepsOperatedAppsAndVoiceSlotInsideThePill() {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        model.apply(Self.voice("listening", apps: ["Calculator"]), now: now)
        model.apply(IslandSnapshot(kind: "acting", title: "Using Calculator", app: "Calculator", step_total: 2,
                                   plugins: Self.uses(["Calculator"]), samantha_on: true),
                    now: now.addingTimeInterval(0.2))

        XCTAssertEqual(model.presentation, .actingSteps)
        XCTAssertTrue(model.showsOperatedApps)
        assertZones(model, apps: 1, voice: .thinking, "multi-step acting")
    }

    func testVoiceStatesStayAtSeventyFourAndKeepTheVoiceSlotReserved() async {
        let model = IslandModel()
        model.idleActivity = "duo"
        var expected: (CGFloat, CGFloat, CGFloat)?
        for kind in ["listening", "thinking", "speaking", "done", "idle"] {
            model.apply(Self.voice(kind, apps: []))
            let shape = model.shapeRect
            let slot = IslandBarLayout.voiceSlotRect(shape: shape, apps: 0, controlled: false,
                                                     content: model.voiceSlotContent ?? .none)
            let dot = IslandBarLayout.compactDotRect(shape: shape)
            XCTAssertEqual(shape.width, 74, kind)
            XCTAssertNotNil(model.voiceSlotContent, "voice slot remains reserved through \(kind)")
            let current = (shape.width, dot.minX, slot.minX)
            if let expected {
                XCTAssertEqual(current.0, expected.0)
                XCTAssertEqual(current.1, expected.1)
                XCTAssertEqual(current.2, expected.2)
            } else { expected = current }
        }
    }

    func testTargetAndAppStayUntilTwoSecondVoiceHoldEnds() async {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        let calculator = Self.acu("com.apple.calculator", 300)
        model.apply(Self.voice("listening", apps: [], plugins: [calculator]), now: now)
        model.apply(IslandSnapshot(kind: "acting", title: "Using Calculator", plugins: [], samantha_on: true), now: now.addingTimeInterval(0.5))
        XCTAssertTrue(model.showsTargetMark)
        XCTAssertEqual(IslandOperatedApps.names(model.pluginUses), ["Calculator"])
        model.apply(IslandSnapshot(kind: "done", title: "Done", plugins: [], samantha_on: true), now: now.addingTimeInterval(1))
        model.apply(IslandSnapshot(kind: "idle", title: "Ready", plugins: [], samantha_on: true), now: now.addingTimeInterval(2.9))
        XCTAssertTrue(model.showsTargetMark, "the target remains during done + 2 s")
        XCTAssertEqual(IslandOperatedApps.names(model.pluginUses), ["Calculator"])
        model.apply(IslandSnapshot(kind: "idle", title: "Ready", plugins: [], samantha_on: true), now: now.addingTimeInterval(3.1))
        XCTAssertFalse(model.showsTargetMark)
        XCTAssertTrue(model.operatedApps.isEmpty)
        XCTAssertNil(model.voiceSlotContent)
    }

    func testListeningPlaybackCompletionPreservesDoneHoldUntilItExpires() {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        let calculator = Self.acu("com.apple.calculator", 300)
        model.apply(Self.voice("listening", apps: [], plugins: [calculator]), now: now)
        model.apply(IslandSnapshot(kind: "done", title: "Done", samantha_on: true), now: now.addingTimeInterval(0.5))
        model.apply(IslandSnapshot(kind: "listening", title: "Listening", samantha_on: true), now: now.addingTimeInterval(1))
        model.apply(IslandSnapshot(kind: "idle", title: "Ready", samantha_on: true), now: now.addingTimeInterval(2.4))

        XCTAssertTrue(model.showsTargetMark, "playback completion must not cancel the done + 2 s target hold")
        XCTAssertEqual(IslandOperatedApps.names(model.pluginUses), ["Calculator"])
        model.apply(IslandSnapshot(kind: "idle", title: "Ready", samantha_on: true), now: now.addingTimeInterval(2.6))
        XCTAssertFalse(model.showsTargetMark)
        XCTAssertTrue(model.operatedApps.isEmpty)
    }

    func testIdleWithoutDoneClearsTheVoiceTurnAndRetainedTarget() {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        let calculator = Self.acu("com.apple.calculator", 300)
        model.apply(Self.voice("listening", apps: [], plugins: [calculator]), now: now)
        model.apply(IslandSnapshot(kind: "idle", title: "Ready", samantha_on: true), now: now.addingTimeInterval(0.5))

        XCTAssertFalse(model.showsTargetMark)
        XCTAssertTrue(model.operatedApps.isEmpty)
        XCTAssertNil(model.voiceSlotContent)
    }

    func testVoiceTurnKeepsOneGeometryThroughActingDoneAndTheHold() {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        let snapshots: [(String, IslandSnapshot)] = [
            ("listening", Self.voice("listening", apps: ["Calculator"])),
            ("thinking", Self.voice("thinking", apps: ["Calculator"])),
            ("acting", IslandSnapshot(kind: "acting", title: "Using Calculator", app: "Calculator",
                                      active_apps: ["Calculator"], plugins: Self.uses(["Calculator"]), samantha_on: true)),
            ("done", Self.voice("done", apps: ["Calculator"])),
            ("idle", Self.voice("idle", apps: ["Calculator"])),
        ]
        let expectedWidth = IslandBarLayout.controlledPillWidth(apps: 1, voice: .listening)
        var widths: [CGFloat] = []
        for (index, (kind, snapshot)) in snapshots.enumerated() {
            model.apply(snapshot, now: now.addingTimeInterval(Double(index) * 0.25))
            widths.append(model.drawnWidth)
            XCTAssertEqual(model.drawnWidth, expectedWidth, "locked ring/app/voice geometry at \(kind)")
            if kind == "done" || kind == "idle" {
                XCTAssertEqual(model.voiceSlotContent, IslandVoiceSlotContent.none,
                               "done is an empty reserved slot, not a done glyph")
            }
        }
        XCTAssertEqual(Set(widths), [expectedWidth], "no 87 / 111 / other intermediate pill widths")
    }

    func testVoiceWidthShrinkWaitsFourHundredMillisecondsAfterDoneHold() async {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.reduceMotion = true
        let now = Date()
        let heldWidth = IslandBarLayout.controlledPillWidth(apps: 1, voice: .listening)
        model.apply(Self.voice("listening", apps: ["Calculator"]), now: now)
        model.apply(Self.voice("done", apps: ["Calculator"]), now: now.addingTimeInterval(0.5))
        model.apply(Self.voice("idle", apps: [], staleApps: false), now: now.addingTimeInterval(2.6))

        XCTAssertEqual(IslandModel.widthShrinkDebounce, 0.4)
        XCTAssertNil(model.voiceSlotContent)
        XCTAssertEqual(model.drawnWidth, heldWidth, "the reserved width stays while the shrink is pending")
        try? await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertEqual(model.drawnWidth, heldWidth, "not before 400 ms")
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(model.drawnWidth, IslandBarLayout.idleWidth, "shrinks after stable idle content")
    }

    func testSpeakingWithoutAudioReturnsToIdle() {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        model.apply(IslandSnapshot(kind: "speaking", title: "Speaking", voice_side: "assistant",
                                   voice_level: 0, samantha_on: true), now: now)
        XCTAssertEqual(model.voiceSlotContent, .speaking)
        model.expireSpeakingIfSilent(now: now.addingTimeInterval(IslandModel.speakingSilenceTimeout + 0.1))
        XCTAssertEqual(model.presentation, .idle)
        XCTAssertNil(model.voiceSlotContent)
    }

    func testThreeOperatedAppsRenderInsideThePill() {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(Self.voice("speaking", apps: Self.apps3))
        let rendered = IslandCaptureHarness.render(SamanthaIslandBar(model: model).environment(\.displayScale, 1), scale: 1)
        XCTAssertEqual(rendered?.width, Int(IslandBarLayout.windowSize.width))
        XCTAssertEqual(overlays(model).filter { $0.kind == "app_icon" }.count, 3)
        XCTAssertEqual(model.shapeRect.width, IslandBarLayout.controlledPillWidth(apps: 3, voice: .speaking))
    }

    /// The exact 0.2.86 bug: speaking after the ACU row dropped, with voice_cua's stale active_apps.
    func testStaleActiveAppsNeverBecomeAnOutsideCircle() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(IslandSnapshot(kind: "speaking", title: "Speaking", active_apps: ["Calculator"], samantha_on: true))
        await settle()
        XCTAssertTrue(m.circles.isEmpty, "0.2.86 drew Calculator as an outside-right circle here")
        XCTAssertFalse(overlays(m).contains { $0.kind == "app_circle" })
        XCTAssertEqual(m.voiceSlotContent, .speaking, "nothing controlled: the voice slot alone")
        XCTAssertEqual(m.shapeRect.width, IslandBarLayout.voicePillWidth(.speaking))
        // The 13:26:54 ordering flash: an acting post before its plugin row never draws the app outside either.
        m.apply(IslandSnapshot(kind: "acting", title: "Using Calculator", app: "Calculator", active_apps: ["Calculator"], samantha_on: true))
        XCTAssertTrue(m.circles.isEmpty)
    }

    func testCometRingKeepsTheVoiceSlotAndNoApps() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(IslandSnapshot(kind: "speaking", title: "Speaking", plugins: [Self.plugin("comet-control", 300)], samantha_on: true))
        await settle()
        assertZones(m, apps: 0, voice: .speaking, "speaking + Comet")
        XCTAssertEqual(m.pluginTarget, .app("Comet"))
    }

    func testNonControllingPluginsAddNothingInside() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(IslandSnapshot(kind: "speaking", title: "Speaking", plugins: [Self.plugin("context-ledger", 300)], samantha_on: true))
        await settle()
        XCTAssertFalse(m.showsTargetMark)
        XCTAssertEqual(m.voiceSlotContent, .speaking)
        XCTAssertEqual(m.circles, [.plugin(name: "context-ledger", agents: ["Samantha"])])
        XCTAssertEqual(m.shapeRect.width, IslandBarLayout.voicePillWidth(.speaking))
    }

    // MARK: - developer-observability mark (Guru 13:33 IST): outside right, nothing inside

    func testDeveloperObservabilityIsAPluginMarkOutsideRightOnly() async {
        XCTAssertTrue(IslandPluginLabel.isValidID("developer-observability"))
        XCTAssertEqual(IslandPluginLabel.display("developer-observability"), "Developer Observability")
        XCTAssertEqual(IslandPluginLabel.rowTitle(name: "developer-observability"), "Using Developer Observability")
        XCTAssertTrue(IslandSourceGroups.isPluginIdentity("Developer Observability"), "never a left harness circle")
        for kind in ["idle", "speaking", "acting"] {
            let m = IslandModel()
            m.idleActivity = "duo"
            let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task", status: "running", last_seen_at: 100)
            m.apply(IslandSnapshot(kind: kind, title: kind, agents: [codex], plugins: [Self.plugin("developer-observability", 300, agent: "Codex")],
                                   samantha_on: kind != "idle"))
            await settle()
            XCTAssertEqual(m.circles, [.plugin(name: "developer-observability", agents: ["Codex"])], "\(kind): mark outside right")
            XCTAssertFalse(m.showsTargetMark, "\(kind): controls nothing, no ring")
            XCTAssertFalse(m.showsOperatedApps)
            XCTAssertFalse(overlays(m).contains { $0.expectedInsideIsland && $0.kind != "voice_slot" }, "\(kind): nothing added inside")
            XCTAssertEqual(m.shapeRect.width, m.voiceSlotContent.map(IslandBarLayout.voicePillWidth) ?? m.shapeRect.width, "\(kind): no width added")
            let mark = overlays(m).first { $0.kind == "plugin_mark" }
            XCTAssertGreaterThanOrEqual(mark?.rect.x ?? 0, Double(m.shapeRect.maxX) + 2)
        }
        // With ACU in the same pill: the ACU ring + app inside; both marks outside right, fixed order.
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(IslandSnapshot(kind: "idle", plugins: [Self.acu("Calculator", 300, agent: "Codex"), Self.plugin("developer-observability", 310, agent: "Codex")]))
        XCTAssertEqual(m.circles.map(\.id), ["plugin:Agent Computer Use", "plugin:developer-observability"])
    }

    func testDeveloperObservabilityMarkMatchesTheSpec() {
        // UI & UX spec: bars 2 pt tall, 1.5 pt apart (a 9 pt group, centred), x 3–9, 5.5–12, 8–13; #30D158 bare on black since Guru 16:06 IST (was #248A3D on a white tile).
        let r = IslandDevObsMark.barRects()
        XCTAssertEqual(r.map(\.minX), [0, 3, 6])
        XCTAssertEqual(r.map(\.maxX), [9, 12, 15])
        XCTAssertEqual(r.map(\.height), [2.5, 2.5, 2.5])
        XCTAssertEqual(r[1].minY - r[0].maxY, 1.25, accuracy: 1e-9)
        XCTAssertEqual(r[2].maxY - r[0].minY, 10, accuracy: 1e-9)
        XCTAssertEqual((r[0].minY + r[2].maxY) / 2, 7.5, accuracy: 1e-9, "centred in the 15 pt optical box")
        let c = NSColor(IslandDevObsMark.color).usingColorSpace(.sRGB)!
        XCTAssertEqual(Int((c.redComponent * 255).rounded()), 0x30)
        XCTAssertEqual(Int((c.greenComponent * 255).rounded()), 0xD1)
        XCTAssertEqual(Int((c.blueComponent * 255).rounded()), 0x58)
    }

    func testPluginAndHarnessCirclesKeepTheirGapThroughPillShrink() async throws {
        let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task",
                                status: "running", last_seen_at: 100)
        let ledger = Self.plugin("context-ledger", 400)
        let model = IslandModel()
        model.idleActivity = "duo"
        let base = Date().timeIntervalSince1970
        let initialUses = Self.apps3.enumerated().map { Self.acu($0.element, base + Double($0.offset) * 0.01) }
        var initialLedger = ledger; initialLedger.last_seen_at = base
        model.apply(IslandSnapshot(kind: "acting", title: "Using Notes", app: "Notes", active_apps: Self.apps3,
                                   agents: [codex], plugins: initialUses + [initialLedger], samantha_on: true))
        let from = model.drawnWidth
        var currentLedger = ledger; currentLedger.last_seen_at = base + 0.5
        model.apply(IslandSnapshot(kind: "acting", title: "Using Calculator", app: "Calculator", active_apps: ["Calculator"],
                                   agents: [codex], plugins: [Self.acu("Calculator", base + 0.5), currentLedger],
                                   samantha_on: true))
        try await Task.sleep(nanoseconds: 450_000_000)
        let to = model.drawnWidth
        XCTAssertGreaterThan(from, to)
        for step in 0...25 {
            let t = Double(step) / 25 * 0.25
            let progress = CGFloat(Spring(response: 0.25, dampingRatio: 0.9).value(target: 1.0, time: t))
            let width = from + (to - from) * progress
            let shape = IslandBarLayout.shapeRect(compactWidth: width, card: false)
            let marks = IslandBarLayout.circleRects(shape: shape, count: model.circles.count)
            XCTAssertEqual(marks[0].minX - shape.maxX, IslandBarLayout.circleGap, accuracy: 0.001)
            XCTAssertGreaterThan(marks[0].minX, IslandBarLayout.compactDotRect(shape: shape).maxX,
                                 "plugin mark clears the dot at shrink frame \(step)")
            let slots = model.sourceGroups.compactMap { model.sourceGroupSlots[$0.id] }
            let harnesses = IslandBarLayout.agentRects(shape: shape, slots: slots)
            XCTAssertEqual(shape.minX - harnesses[0].maxX, IslandBarLayout.circleGap, accuracy: 0.001)
            for pair in zip(harnesses, harnesses.dropFirst()) {
                XCTAssertGreaterThanOrEqual(pair.0.minX - pair.1.maxX, IslandBarLayout.circleSpacing - 0.001,
                                            "harness circles do not overlap at shrink frame \(step)")
            }
        }
    }

    // MARK: - Transitions: every sampled frame keeps the locked zones

    /// One transition A → B, sampled every 10 ms for 0.6 s with the real curves: pill width on the operated
    /// spring / shared width curve (Reduce Motion: 0.15 s crossfade curve), incl. the 160 ms pinch; plugin marks
    /// entering after `pluginMarkEnter` and leaving on `pluginMarkExit`. Inside elements are clipped to the
    /// animated pill path (SamanthaIslandBar), so only the outside zones can be violated mid-frame.
    private func checkTransition(_ m: IslandModel, to snap: IslandSnapshot, reduce: Bool, _ label: String) async -> [String] {
        var problems: [String] = []
        let shape0 = m.shapeRect, ov0 = overlays(m)
        m.apply(snap)
        let immediate = m.drawnWidth, spring = m.widthFromOperatedApps
        await settle()
        let shape1 = m.shapeRect, ov1 = overlays(m)
        let w0 = shape0.width, w1 = shape1.width
        let pinch = !reduce && immediate == IslandBarLayout.idleWidth && w1 != IslandBarLayout.idleWidth && w0 != IslandBarLayout.idleWidth
        func curve(_ t: Double) -> CGFloat {
            if t <= 0 { return 0 }
            if reduce { return CGFloat(UnitCurve.easeInOut.value(at: min(1, t / 0.15))) }
            if spring { return CGFloat(Spring(response: 0.25, dampingRatio: 0.9).value(target: 1.0, time: t)) }
            return CGFloat(Spring(duration: 0.35, bounce: 0).value(target: 1.0, time: t))
        }
        func width(_ t: Double) -> CGFloat {
            guard pinch else { return w0 + (w1 - w0) * curve(t) }
            let a = w0 + (IslandBarLayout.idleWidth - w0) * curve(min(t, 0.16))
            return t < 0.16 ? a : a + (w1 - a) * curve(t - 0.16)
        }
        func pillMaxX(_ t: Double) -> CGFloat { IslandBarLayout.windowSize.width / 2 + width(t) / 2 }
        let entering = ov1.filter { $0.kind == "plugin_mark" || $0.kind == "overflow_circle" }.count
            > ov0.filter { $0.kind == "plugin_mark" || $0.kind == "overflow_circle" }.count
        let leaving = ov0.filter { $0.kind == "plugin_mark" || $0.kind == "overflow_circle" }.count
            > ov1.filter { $0.kind == "plugin_mark" || $0.kind == "overflow_circle" }.count
        let enterDelay = reduce ? 0.15 : IslandMotion.pluginMarkEnterDelay
        // Voice slot content (meter ↔ dots morph, or the Reduce Motion crossfade) never reaches the dot.
        let v0 = ov0.first { $0.kind == "voice_slot" }, v1 = ov1.first { $0.kind == "voice_slot" }
        let c0 = v0.flatMap { IslandVoiceSlotContent(rawValue: String($0.id.dropFirst("voice_slot_".count))) }
        let c1 = v1.flatMap { IslandVoiceSlotContent(rawValue: String($0.id.dropFirst("voice_slot_".count))) }
        let levels = m.voiceBars.map { _ in 1.0 }
        func contentRight(_ c: IslandVoiceSlotContent, morph: Double, speaker: Double) -> CGFloat {
            c == .none ? 0 : IslandVoiceSlotDrawing.contentMaxX(levels: levels, morph: morph, speaker: speaker)
        }
        for i in 0...60 {
            let t = Double(i) / 100
            if let v0, let v1, let c0, let c1 {
                let x = CGFloat(v0.rect.x) + CGFloat(v1.rect.x - v0.rect.x) * curve(t)
                var rights: [CGFloat] = []
                if c0 == c1 {
                    rights = [contentRight(c1, morph: IslandVoiceSlotDrawing.pose(c1).morph, speaker: IslandVoiceSlotDrawing.pose(c1).speaker)]
                } else if c1 == .none {
                    rights = []
                } else if c0 == .none {
                    rights = [contentRight(c1, morph: IslandVoiceSlotDrawing.pose(c1).morph, speaker: 0)]
                } else if reduce {
                    let f = IslandVoiceSlotDrawing.reducedFade(elapsed: t)
                    if f.out > 0.1 { rights.append(contentRight(c0, morph: IslandVoiceSlotDrawing.pose(c0).morph, speaker: 0)) }
                    if f.in > 0.1 { rights.append(contentRight(c1, morph: IslandVoiceSlotDrawing.pose(c1).morph, speaker: 0)) }
                } else {
                    let p = IslandVoiceSlotDrawing.pose(from: c0, to: c1, elapsed: t)
                    rights.append(contentRight(c1, morph: p.morph, speaker: p.speaker))
                }
                let dotMinX = pillMaxX(t) - 9 - IslandBarLayout.compactDotDiameter
                for r in rights where x + r > dotMinX - 2 {
                    problems.append(String(format: "%@ t=%.2f: voice content right %.1f within 2 pt of the dot %.1f", label, t, x + r, dotMinX))
                }
            }
            // Entering marks sit at their final slot; visible (> 10 %) only after the delay.
            if entering, t >= enterDelay + 0.01 {
                for o in ov1 where o.kind == "plugin_mark" || o.kind == "overflow_circle" {
                    if CGFloat(o.rect.x) < pillMaxX(t) + 2 { problems.append(String(format: "%@ t=%.2f: entering mark x %.1f over pill edge %.1f", label, t, o.rect.x, pillMaxX(t))) }
                }
            }
            // Leaving marks stay at their old slot while opacity > 10 % (easeOut 0.1 s → gone by ~0.09 s).
            if leaving, t < 0.09 {
                // (Persisting marks ride the pill edge on the same transaction; harness circles ride the left
                // edge and IslandHarnessMotionTests owns their enter / exit frames.)
                for o in ov0 where o.kind == "plugin_mark" || o.kind == "overflow_circle" {
                    if CGFloat(o.rect.x) < pillMaxX(t) - 0.5 { problems.append(String(format: "%@ t=%.2f: leaving mark x %.1f under pill edge %.1f", label, t, o.rect.x, pillMaxX(t))) }
                }
            }
        }
        // End frame: static zones.
        if ov1.contains(where: { $0.kind == "app_circle" }) { problems.append("\(label): app circle at end") }
        for o in ov1 where o.expectedInsideIsland {
            let r = CGRect(x: o.rect.x, y: o.rect.y, width: o.rect.w, height: o.rect.h)
            if !shape1.insetBy(dx: -0.5, dy: -0.5).contains(r) { problems.append("\(label): \(o.id) outside the pill at end") }
        }
        // No jump: the ring keeps its slot relative to the pill's left edge across the transition.
        if let r0 = ov0.first(where: { $0.kind == "target_ring" || $0.kind == "comet_circle" }),
           let r1 = ov1.first(where: { $0.kind == "target_ring" || $0.kind == "comet_circle" }),
           abs((r0.rect.x - Double(shape0.minX)) - (r1.rect.x - Double(shape1.minX))) > 0.01 {
            problems.append("\(label): ring jumped inside the pill")
        }
        return problems
    }

    static func transitions(apps: [String]) -> [(String, [IslandSnapshot])] {
        let on = IslandSnapshot(kind: "listening", title: "Listening", samantha_on: true)
        let idleOff = IslandSnapshot(kind: "idle")
        let idleOn = IslandSnapshot(kind: "idle", title: "Resting", samantha_on: true)
        let ledger = plugin("context-ledger", 400)
        return [
            ("samantha-on-off", [idleOff, on, idleOff]),
            ("voice-cycle", [on, IslandSnapshot(kind: "thinking", title: "Thinking", samantha_on: true),
                             IslandSnapshot(kind: "speaking", title: "Speaking", samantha_on: true), idleOn]),
            ("turn-acu", [on, IslandSnapshot(kind: "thinking", title: "Thinking", samantha_on: true),
                          IslandSnapshot(kind: "acting", title: "Using", app: apps.last!, active_apps: apps, plugins: uses(apps), samantha_on: true),
                          voice("done", apps: apps), voice("speaking", apps: apps),
                          IslandSnapshot(kind: "speaking", title: "Speaking", samantha_on: true), idleOn]),
            ("speaking-acting", [voice("speaking", apps: apps),
                                 IslandSnapshot(kind: "acting", title: "Using", app: apps.last!, active_apps: apps, plugins: uses(apps), samantha_on: true),
                                 voice("speaking", apps: apps)]),
            ("app-added-removed", [voice("speaking", apps: [apps[0]]), voice("speaking", apps: apps.count > 1 ? apps : apps + ["TextEdit"]),
                                   voice("speaking", apps: [apps[0]])]),
            ("plugin-marks-in-voice", [voice("speaking", apps: apps), voice("speaking", apps: apps, plugins: [ledger]),
                                       voice("speaking", apps: apps), IslandSnapshot(kind: "speaking", title: "Speaking", plugins: [ledger], samantha_on: true),
                                       IslandSnapshot(kind: "speaking", title: "Speaking", samantha_on: true)]),
            ("barge-in", [voice("speaking", apps: apps), voice("listening", apps: apps),
                          IslandSnapshot(kind: "speaking", title: "Speaking", samantha_on: true),
                          IslandSnapshot(kind: "listening", title: "Listening", samantha_on: true)]),
        ]
    }

    func testEveryVoiceTransitionKeepsTheZonesInEveryFrame() async {
        var all: [String] = []
        var count = 0
        for reduce in [false, true] {
            for apps in [["Calculator"], Self.apps3] {
                for (name, steps) in Self.transitions(apps: apps) {
                    let m = IslandModel()
                    m.idleActivity = "duo"
                    m.systemReduceMotion = reduce
                    m.apply(steps[0])
                    await settle()
                    for (i, s) in steps.dropFirst().enumerated() {
                        count += 1
                        all += await checkTransition(m, to: s, reduce: reduce,
                                                     "\(name)[\(i)→\(i + 1)] \(apps.count) app(s)\(reduce ? " RM" : "")")
                    }
                }
            }
        }
        XCTAssertGreaterThan(count, 40)
        XCTAssertEqual(all, [], all.joined(separator: "\n"))
    }
}
