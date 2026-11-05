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
        XCTAssertEqual(L.idleWidth, L.activeMinimumWidth, "idle keeps the original 74 pt compact width")
        XCTAssertEqual(L.voiceMeterWidth, 23, "5 bars × 3 pt + 4 × 2 pt")
        XCTAssertEqual(L.voiceSlotWidth(.listening), 23)
        XCTAssertEqual(L.voiceSlotWidth(.speaking), 23)
        XCTAssertEqual(L.voiceSlotWidth(.thinking), 23, "thinking reserves the meter width")
        XCTAssertEqual(L.voicePillWidth(.listening), 74)
        XCTAssertEqual(L.voicePillWidth(.thinking), 74)
        XCTAssertEqual(L.controlledPillWidth(apps: 1, voice: .speaking), 126)
        XCTAssertEqual(L.controlledPillWidth(apps: 3, voice: .speaking), 166)
        XCTAssertEqual(L.controlledPillWidth(apps: 4, voice: .thinking), 186, "3 icons + the +N slot")
        XCTAssertEqual(L.controlledPillWidth(apps: 0, voice: .listening), 102, "Comet keeps the voice slot and its 6 pt gap")
        XCTAssertEqual(L.controlledPillWidth(apps: 1, voice: nil), 94)
        XCTAssertEqual(L.targetOnlyPillWidth, 74)
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

    func testVoiceContentChangesKeepTheMinimumWidth() async {
        let m = IslandModel()
        m.idleActivity = "duo"
        m.apply(voice("listening"))
        await settle()
        XCTAssertEqual(m.shapeRect.width, L.voicePillWidth(.listening))
        m.apply(voice("thinking"))
        XCTAssertEqual(m.drawnWidth, L.voicePillWidth(.thinking), "no pinch: straight to the new hug width")
        XCTAssertFalse(m.widthFromOperatedApps)
        await settle()
        m.apply(voice("speaking"))
        XCTAssertEqual(m.drawnWidth, L.voicePillWidth(.speaking))
        XCTAssertFalse(m.widthFromOperatedApps)
        await settle()
        m.apply(voice("listening"))
        XCTAssertEqual(m.drawnWidth, L.voicePillWidth(.listening), "same slot width: no width change")
    }

    func testVoiceStatesKeepWidthDotAndSlotAnchorsAtSeventyFour() async {
        let model = IslandModel()
        model.idleActivity = "duo"
        var expected: (CGFloat, CGFloat, CGFloat)?
        for (kind, content) in [("idle", IslandVoiceSlotContent.none), ("listening", .listening),
                                 ("thinking", .thinking), ("speaking", .speaking)] {
            let shape = L.shapeRect(compactWidth: L.voicePillWidth(content), card: false)
            let slot = L.voiceSlotRect(shape: shape, apps: 0, controlled: false, content: content)
            let dot = L.compactDotRect(shape: shape)
            XCTAssertEqual(shape.width, 74, kind)
            let current = (shape.width, dot.minX, slot.minX)
            if let expected {
                XCTAssertEqual(current.0, expected.0)
                XCTAssertEqual(current.1, expected.1)
                XCTAssertEqual(current.2, expected.2)
            } else { expected = current }
        }
        XCTAssertEqual(expected?.1, L.shapeRect(compactWidth: 74, card: false).minX + 59)
        XCTAssertEqual(expected?.2, L.shapeRect(compactWidth: 74, card: false).minX + 9)
    }

    func testCompactWidthsAreEvenAndWindowOriginsSnapToBackingPixels() {
        for rawWidth in [CGFloat(74), 75, 87, 109, 118, 119, 126, 167] {
            let width = L.snappedCompactWidth(rawWidth)
            XCTAssertEqual(width, width.rounded(.down))
            XCTAssertEqual(Int(width) % 2, 0)
        }
        let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)
        let strip = CGRect(x: 720.5, y: 952, width: 71, height: 30)
        for scale in [CGFloat(1), 2] {
            let frame = L.windowFrame(bar: strip, screen: screen, scale: scale)
            XCTAssertEqual(frame.minX * scale, (frame.minX * scale).rounded(), accuracy: 0.001)
            XCTAssertEqual(frame.minY * scale, (frame.minY * scale).rounded(), accuracy: 0.001)
        }
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
        XCTAssertEqual(icons.first!.minX - L.targetRingOuterRect(shape: shape).maxX, 24, accuracy: 0.5)
        XCTAssertEqual(v.minX - icons.last!.maxX, 8, accuracy: 0.001)
        XCTAssertEqual(L.compactDotRect(shape: shape).minX - v.maxX, 6, accuracy: 1)
        XCTAssertEqual(shape.width, L.controlledPillWidth(apps: 4, voice: .speaking))
    }

    func testOuterRingToDotIsTwentyFourPointsAtOneXAndTwoX() throws {
        for scale in [CGFloat(1), 2] {
            let width = L.targetOnlyPillWidth
            let shape = CGRect(origin: .zero, size: CGSize(width: width, height: L.compactHeight))
            let outer = L.targetRingOuterRect(shape: shape, scale: scale)
            let dot = L.targetOnlyDotRect(shape: shape, scale: scale)
            XCTAssertEqual(dot.minX - outer.maxX, 24, accuracy: 0.5, "scale \(scale)")
            let ring = IslandPluginTargetMark(target: .symbol("apple.logo"), allTargets: [.symbol("apple.logo")], reduceMotion: true)
            let rendered = ZStack(alignment: .topLeading) {
                Capsule().fill(Color.black).frame(width: width, height: L.compactHeight)
                ring.offset(x: L.targetRingFrame(shape: shape, scale: scale).minX,
                            y: L.targetRingFrame(shape: shape, scale: scale).minY)
                Circle().fill(IslandPalette.accent).frame(width: dot.width, height: dot.height)
                    .offset(x: dot.minX, y: dot.minY)
            }.frame(width: width, height: L.compactHeight).environment(\.displayScale, scale)
            let image = try XCTUnwrap(IslandCaptureHarness.render(rendered, scale: scale))
            XCTAssertEqual(image.width, Int(width * scale), "ring and dot capture at scale \(scale)")
            var pixels = [UInt8](repeating: 0, count: image.width * image.height * 4)
            let context = CGContext(data: &pixels, width: image.width, height: image.height, bitsPerComponent: 8,
                                    bytesPerRow: image.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
            let y = min(image.height - 1, Int((shape.midY * scale).rounded()))
            func visibleX(in range: Range<Int>) -> [Int] {
                range.filter { x in
                    let i = (y * image.width + x) * 4
                    return max(pixels[i], max(pixels[i + 1], pixels[i + 2])) > 12
                }
            }
            let ringX = try XCTUnwrap(visibleX(in: Int((outer.maxX - 2) * scale)..<Int((outer.maxX + 1) * scale)).max())
            let dotX = try XCTUnwrap(visibleX(in: Int((dot.minX - 1) * scale)..<Int((dot.maxX + 1) * scale)).min())
            XCTAssertEqual(CGFloat(dotX - ringX - 1) / scale, 24, accuracy: 0.5, "captured ring edge to dot at scale \(scale)")
        }
    }
}
