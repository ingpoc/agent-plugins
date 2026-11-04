import AppKit
import QuartzCore
import SwiftUI
import XCTest
@testable import CUAService

/// Samantha's turn-on uses the one harness-circle motion (UI & UX, 2026-11-04 11:57 IST).
@MainActor
final class IslandHarnessMotionTests: XCTestCase {
    private let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task", status: "running", last_seen_at: 100)

    func testHarnessMotionIsTheSharedWidthCurveAndCrossfadeUnderReduceMotion() {
        XCTAssertEqual(IslandMotion.harness(reduce: false), IslandMotion.widthCurve)
        XCTAssertEqual(IslandMotion.harness(reduce: true), IslandMotion.crossfade)
        XCTAssertEqual(IslandMotion.harnessEnterScale, 0.6)
    }

    func testScaleAnchorIsTheDrawnCircleCentre() {
        // Circles are offset inside the window-sized ZStack; their layout frame is 24×24 at the origin.
        let r = CGRect(x: 262, y: 3, width: 24, height: 24)
        let a = IslandMotion.harnessAnchor(r)
        XCTAssertEqual(a.x * r.width, r.midX, accuracy: 0.001)
        XCTAssertEqual(a.y * r.height, r.midY, accuracy: 0.001)
    }

    func testNeighboursKeepIdentityToSlideButCrossfadeUnderReduceMotion() {
        XCTAssertEqual(IslandMotion.harnessSlotID("Codex", slot: 0, reduce: false),
                       IslandMotion.harnessSlotID("Codex", slot: 1, reduce: false), "same view, so it slides")
        XCTAssertNotEqual(IslandMotion.harnessSlotID("Codex", slot: 0, reduce: true),
                          IslandMotion.harnessSlotID("Codex", slot: 1, reduce: true), "new view, so it cross-fades")
    }

    func testTurningSamanthaOnOpensASlotAndMovesTheNeighbourByOneSlot() {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        let before = IslandBarLayout.agentRects(shape: model.shapeRect, count: model.sourceGroups.count)
        model.apply(IslandSnapshot(kind: "listening", title: "Listening", agents: [codex], samantha_on: true))
        let ids = model.sourceGroups.map(\.id)
        let after = IslandBarLayout.agentRects(shape: model.shapeRect, count: model.sourceGroups.count)
        XCTAssertEqual(ids.count, 2)
        let codexBefore = before[0], codexAfter = after[ids.firstIndex { $0 != IslandSourceGroups.samantha }!]
        // Pill edge may move with the pill width; the slot itself is one circle + spacing.
        let slot = IslandBarLayout.circleDiameter + IslandBarLayout.circleSpacing
        XCTAssertEqual((codexAfter.minX - after[0].minX) - (codexBefore.minX - before[0].minX), -slot, accuracy: 0.001)
        model.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        XCTAssertEqual(model.sourceGroups.count, 1, "turning off closes the slot")
    }

    /// Real frames from the live view: the island hosted in an offscreen, borderless window of the test
    /// process (never on the user's screen, no input), Samantha turned on, the live hosting view captured
    /// at 0/25/50/75/100% of the 0.35 s harness curve. CUA_ISLAND_MOTION_DIR=<dir> swift test --filter testCaptureTurnOnFrames
    func testCaptureTurnOnFrames() async throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_MOTION_DIR"] else {
            throw XCTSkip("set CUA_ISLAND_MOTION_DIR to capture")
        }
        let plugins = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../../").standardized
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        for (label, on, reduce) in [("on", true, false), ("off", false, false), ("on-reduce", true, true)] {
            let model = IslandModel()
            model.idleActivity = "duo"
            let offSnap = IslandSnapshot(kind: "idle", agents: [codex])
            let onSnap = IslandSnapshot(kind: "listening", title: "Listening", agents: [codex], samantha_on: true)
            model.apply(on ? offSnap : onSnap)
            model.systemReduceMotion = reduce
            let root = SamanthaIslandBar(model: model)
                .environment(\.colorScheme, .dark)
                .background(Color(white: 0.18))
            let host = NSHostingView(rootView: root)
            let size = IslandBarLayout.windowSize
            host.frame = CGRect(origin: .zero, size: size)
            let win = NSWindow(contentRect: CGRect(x: -8000, y: -8000, width: size.width, height: size.height),
                               styleMask: [.borderless], backing: .buffered, defer: false)
            win.isReleasedWhenClosed = false
            win.contentView = host
            win.orderBack(nil)
            defer { win.orderOut(nil); win.close() }
            func spin(_ s: Double) { RunLoop.main.run(until: Date().addingTimeInterval(s)) }
            spin(1.0)
            let total = reduce ? 0.15 : 0.35
            var frames: [(Double, NSBitmapImageRep)] = []
            let t0 = CACurrentMediaTime()
            model.apply(on ? onSnap : offSnap)
            for p in [0.0, 0.25, 0.5, 0.75, 1.0] {
                while CACurrentMediaTime() - t0 < p * total { spin(0.001) }
                guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return XCTFail("rep") }
                host.cacheDisplay(in: host.bounds, to: rep)
                frames.append(((CACurrentMediaTime() - t0) * 1000, rep))
            }
            spin(0.6)
            guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return XCTFail("rep") }
            host.cacheDisplay(in: host.bounds, to: rep)
            frames.append(((CACurrentMediaTime() - t0) * 1000, rep))
            for (i, (ms, rep)) in frames.enumerated() {
                let name = String(format: "%@-%d-%03dms.png", label, i, Int(ms.rounded()))
                try rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
            }
        }
    }
}
