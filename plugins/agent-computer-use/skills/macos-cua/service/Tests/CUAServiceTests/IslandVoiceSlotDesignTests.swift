import XCTest
import SwiftUI
@testable import CUAService

/// Samantha's voice slot (Guru 2026-11-04 14:17 IST, "A plus C's shape cue"; UI & UX spec).
@MainActor
final class IslandVoiceSlotDesignTests: XCTestCase {
    typealias L = IslandBarLayout
    typealias D = IslandVoiceSlotDrawing

    private func settle() async { try? await Task.sleep(nanoseconds: 350_000_000) }
    private func voice(_ kind: String, level: Double = 0.7, levels: [Double] = [0.3, 0.8, 0.5, 0.9, 0.4],
                       plugins: [IslandPluginUse] = []) -> IslandSnapshot {
        IslandSnapshot(kind: kind, title: kind.capitalized, voice_side: kind == "speaking" ? "assistant" : "user",
                       voice_level: level, voice_levels: levels, plugins: plugins, samantha_on: true)
    }
    private func acu(_ app: String, _ t: Double) -> IslandPluginUse {
        IslandPluginUse(plugin: "acu", app: app, agent: "Samantha", last_seen_at: t, target_app: app)
    }

    func testSlotAndPillWidthsFollowTheSpec() {
        XCTAssertEqual(L.voiceMeterWidth, 23, "5 bars × 3 pt + 4 × 2 pt")
        XCTAssertEqual(L.voiceSlotWidth(.listening), 23)
        XCTAssertEqual(L.voiceSlotWidth(.speaking), 23)
        XCTAssertEqual(L.voiceSlotWidth(.thinking), 14)
        // Nothing controlled: 9 · slot · 6 · dot · 9.
        XCTAssertEqual(L.voicePillWidth(.listening), 9 + 23 + 6 + 6 + 9)
        XCTAssertEqual(L.voicePillWidth(.thinking), 9 + 14 + 6 + 6 + 9)
        // ACU: ring (26) · 20 · apps · 8 · slot · 6 · dot · 9.
        XCTAssertEqual(L.controlledPillWidth(apps: 1, voice: .speaking), 26 + 20 + 16 + 8 + 23 + 6 + 6 + 9)
        XCTAssertEqual(L.controlledPillWidth(apps: 3, voice: .speaking), 26 + 20 + 56 + 8 + 23 + 6 + 6 + 9)
        XCTAssertEqual(L.controlledPillWidth(apps: 4, voice: .thinking), 26 + 20 + 76 + 8 + 14 + 6 + 6 + 9, "3 icons + the +N slot")
        XCTAssertEqual(L.controlledPillWidth(apps: 0, voice: .listening), 26 + 20 + 23 + 6 + 6 + 9, "Comet: ring · 20 · slot")
        XCTAssertEqual(L.controlledPillWidth(apps: 1, voice: nil), 26 + 20 + 16 + 6 + 6 + 9, "no voice: unchanged")
    }

    func testBarsAreThreePointsTwoApartAndThreeToFourteenTall() {
        let lo = D.marks(levels: [0, 0, 0, 0, 0], morph: 0, speaker: 0, dots: [1, 1, 1])
        let hi = D.marks(levels: [1, 1, 1, 1, 1], morph: 0, speaker: 0, dots: [1, 1, 1])
        XCTAssertEqual(lo.count, 5)
        for (i, m) in lo.enumerated() {
            XCTAssertEqual(m.rect.width, 3); XCTAssertEqual(m.rect.minX, CGFloat(i) * 5); XCTAssertEqual(m.rect.height, 3)
        }
        XCTAssertEqual(hi.map(\.rect.height), Array(repeating: 14, count: 5))
        XCTAssertEqual(hi.last!.rect.maxX, 23)
    }

    func testGuruGrowsFromTheBaselineInWhiteSamanthaIsCentredInHerGradient() {
        let levels = [0.1, 0.9, 0.4, 0.7, 0.2]
        let guru = D.marks(levels: levels, morph: 0, speaker: 0, dots: [1, 1, 1])
        XCTAssertEqual(Set(guru.map(\.rect.maxY)).count, 1, "one baseline")
        XCTAssertEqual(guru[0].rect.maxY, 15)
        XCTAssertTrue(guru.allSatisfy { $0.gradient == 0 }, "white")
        let sam = D.marks(levels: levels, morph: 0, speaker: 1, dots: [1, 1, 1])
        for m in sam { XCTAssertEqual(m.rect.midY, 8, accuracy: 0.001, "centred, symmetric") }
        XCTAssertTrue(sam.allSatisfy { $0.gradient == 1 })
        // Gradient runs #BF5AF2 → #FF6482 left to right.
        let a = NSColor(D.color(sam[0])).usingColorSpace(.sRGB)!, b = NSColor(D.color(sam[4])).usingColorSpace(.sRGB)!
        XCTAssertLessThan(a.redComponent, b.redComponent)
        XCTAssertGreaterThan(a.blueComponent, b.blueComponent)
        XCTAssertEqual(NSColor(D.color(guru[2])).usingColorSpace(.sRGB)!.blueComponent, 1, accuracy: 0.001)
    }

    func testThinkingIsThreeDotsSixApartInFourteenPoints() {
        let ms = D.marks(levels: [1, 1, 1, 1, 1], morph: 1, speaker: 0, dots: [1, 0.6, 0.35])
        let shown = ms.filter { $0.opacity > 0.01 }
        XCTAssertEqual(shown.count, 3)
        XCTAssertEqual(shown.map(\.rect.width), [3.5, 3.5, 3.5])
        XCTAssertEqual(shown[1].rect.midX - shown[0].rect.midX, 6, accuracy: 0.001)
        XCTAssertEqual(shown[2].rect.midX - shown[1].rect.midX, 6, accuracy: 0.001)
        XCTAssertEqual(shown[1].rect.midX, 7, accuracy: 0.001, "centred in the 14 pt slot")
        XCTAssertEqual(shown.map(\.opacity), [1, 0.6, 0.35])
    }

    func testDotPulseSteps100_60_35OnA09sLoopAndIsStaticUnderReduceMotion() {
        XCTAssertEqual(D.dotOpacities(time: 0, reduceMotion: false), [1, 0.6, 0.35])
        let s1 = D.dotOpacities(time: 0.3, reduceMotion: false)
        XCTAssertEqual(s1[1], 1, accuracy: 0.001); XCTAssertEqual(s1[2], 0.6, accuracy: 0.001); XCTAssertEqual(s1[0], 0.35, accuracy: 0.001)
        let loop = D.dotOpacities(time: 0.9, reduceMotion: false)
        for (x, y) in zip(loop, [1, 0.6, 0.35]) { XCTAssertEqual(x, y, accuracy: 0.001) }
        XCTAssertEqual(D.dotOpacities(time: 0.47, reduceMotion: true), [0.62, 0.62, 0.62])
    }

    func testMorphTimings() {
        XCTAssertEqual(D.duration(from: .listening, to: .thinking), 0.2)
        XCTAssertEqual(D.duration(from: .thinking, to: .speaking), 0.2)
        XCTAssertEqual(D.duration(from: .speaking, to: .listening), 0.15)
        // listening → thinking: bars ease into dots over 200 ms.
        XCTAssertEqual(D.pose(from: .listening, to: .thinking, elapsed: 0).morph, 0)
        XCTAssertEqual(D.pose(from: .listening, to: .thinking, elapsed: 0.1).morph, 0.5, accuracy: 0.001)
        XCTAssertEqual(D.pose(from: .listening, to: .thinking, elapsed: 0.2).morph, 1)
        // thinking → speaking: dots grow into bars (and take her colour) over 200 ms.
        let p = D.pose(from: .thinking, to: .speaking, elapsed: 0.2)
        XCTAssertEqual(p.morph, 0); XCTAssertEqual(p.speaker, 1)
        // speaking → listening: a 150 ms colour crossfade, no morph.
        let q = D.pose(from: .speaking, to: .listening, elapsed: 0.075)
        XCTAssertEqual(q.morph, 0); XCTAssertEqual(q.speaker, 0.5, accuracy: 0.001)
        XCTAssertEqual(D.pose(from: .speaking, to: .listening, elapsed: 0.15).speaker, 0)
        // Reduce Motion: crossfade, the old pose gone within 50 ms, the new one in by 200 ms.
        XCTAssertEqual(D.reducedFade(elapsed: 0.05).out, 0)
        XCTAssertEqual(D.reducedFade(elapsed: 0.08).in, 0); XCTAssertEqual(D.reducedFade(elapsed: 0.2).in, 1, accuracy: 1e-9)
    }

    func testMeterSmoothsOverAbout80msAndRunsAt10HzUnderReduceMotion() {
        XCTAssertEqual(IslandModel.meterInterval(reduceMotion: true), 0.1)
        XCTAssertLessThan(IslandModel.meterInterval(reduceMotion: false), 0.05)
        let s = IslandModel.smoothBars([0, 0, 0, 0, 0], [1, 1, 1, 1, 1], dt: 0.08)
        XCTAssertEqual(s[0], 1 - exp(-1), accuracy: 0.0001, "one time constant = 80 ms")
    }

    func testLoudnessNeverChangesTheWidthAndTheMeterNeverLightsTheDot() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(voice("speaking", level: 0.05, levels: [0, 0, 0, 0, 0]))
        await settle()
        let w = m.shapeRect.width
        XCTAssertEqual(w, L.voicePillWidth(.speaking))
        for level in [0.2, 0.6, 1.0] {
            m.applyMeter(voice("speaking", level: level, levels: Array(repeating: level, count: 5)), force: true)
            m.apply(voice("speaking", level: level, levels: Array(repeating: level, count: 5)))
            XCTAssertEqual(m.shapeRect.width, w, "loudness \(level)")
            XCTAssertNil(m.compactDot, "the meter never lights the dot")
        }
    }

    func testWidthChangesOnlyWithTheSlotContentOnTheSpring() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(voice("listening"))
        await settle()
        XCTAssertEqual(m.shapeRect.width, L.voicePillWidth(.listening))
        m.apply(voice("thinking"))
        XCTAssertEqual(m.drawnWidth, L.voicePillWidth(.thinking), "no pinch: straight to the new hug width")
        XCTAssertTrue(m.widthFromOperatedApps, "on the 250 ms spring")
        await settle()
        m.apply(voice("speaking"))
        XCTAssertEqual(m.drawnWidth, L.voicePillWidth(.speaking))
        XCTAssertTrue(m.widthFromOperatedApps)
        await settle()
        m.apply(voice("listening"))
        XCTAssertEqual(m.drawnWidth, L.voicePillWidth(.listening), "same slot width: no width change")
    }

    func testWithAcuTheSlotFollowsRingGapAppsAndEight() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        let apps = ["Calculator", "TextEdit", "Notes", "Finder", "Mail"]
        m.apply(voice("speaking", plugins: apps.enumerated().map { acu($0.element, 300 + Double($0.offset)) }))
        await settle()
        XCTAssertEqual(m.operatedApps.count, 4, "newest 3, then +N")
        let shape = m.shapeRect
        let icons = L.operatedAppRects(shape: shape, count: 4)
        let v = L.voiceSlotRect(shape: shape, apps: 4, controlled: true, content: .speaking)
        XCTAssertEqual(icons.first!.minX - (shape.minX + L.ringRight), 20, accuracy: 0.001)
        XCTAssertEqual(v.minX - icons.last!.maxX, 8, accuracy: 0.001)
        XCTAssertEqual(L.compactDotRect(shape: shape).minX - v.maxX, 6, accuracy: 0.001)
        XCTAssertEqual(shape.width, L.controlledPillWidth(apps: 4, voice: .speaking))
    }
}
