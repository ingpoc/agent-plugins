import AppKit
import QuartzCore
import SwiftUI
import XCTest
@testable import CUAService

/// Samantha's turn-on uses the one harness-circle motion (UI & UX, 2026-11-04 11:57 IST).
@MainActor
final class IslandHarnessMotionTests: XCTestCase {
    private let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task", status: "running", last_seen_at: 100)

    func testHarnessMotionIsTheSharedWidthCurveAndSnapsUnderReduceMotion() {
        XCTAssertEqual(IslandMotion.harness(reduce: false), IslandMotion.widthCurve)
        XCTAssertNil(IslandMotion.harness(reduce: true), "Reduce Motion: the layout snaps, nothing slides")
        XCTAssertEqual(IslandMotion.harnessEnterScale, 0.6)
    }

    /// Value of the shared curve (`.smooth(duration: 0.35)`, a critically damped spring) at `fraction` of 0.35 s.
    private func curve(_ fraction: Double) -> CGFloat {
        CGFloat(Spring(duration: 0.35, bounce: 0).value(target: 1.0, time: 0.35 * fraction))
    }

    /// Frame geometry of a turn-on / turn-off, one Codex neighbour (UI & UX, 2026-11-04 12:24 IST): on the
    /// shared curve the circle never sits under the pill, never overlaps Codex from the 25 % frame on, clears
    /// it by ≥ 2 pt from 50 %, lands exactly on its slot, and draws above the neighbour it pushes. (At 25 % the
    /// opened slot is 19.0 pt for an 18.9 pt circle, so ≥ 2 pt there needs a smaller start scale.)
    func testEnteringCircleStaysInItsOpeningSlotAndAboveItsNeighbour() {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(IslandSnapshot(kind: "listening", title: "Listening", agents: [codex], samantha_on: true))
        let final = IslandBarLayout.agentRects(shape: model.shapeRect, count: 2)
        XCTAssertEqual(model.sourceGroups.first?.id, IslandSourceGroups.samantha, "Samantha nearest the pill")
        let sam = final[0], codexFinal = final[1], pillMinX = model.shapeRect.minX
        let gap = IslandMotion.harnessTrailingGap(rects: final, index: 0, shape: model.shapeRect)
        XCTAssertEqual(gap, 6, accuracy: 0.001)
        var clear: [Int: CGFloat] = [:]
        for step in 0...20 {
            let f = Double(step) / 20, v = curve(f)
            let s = IslandMotion.harnessFrame(final: sam, progress: v, trailingGap: gap)
            let codexMaxX = codexFinal.maxX + IslandMotion.harnessSlot * (1 - v)   // Codex slides on the same curve
            XCTAssertEqual(s.width, sam.width * (0.6 + 0.4 * v), accuracy: 0.001)
            XCTAssertEqual(s.midY, sam.midY, accuracy: 0.001)
            XCTAssertLessThanOrEqual(s.maxX, pillMinX + 0.001, "never under the pill (\(f))")
            clear[step * 5] = s.minX - codexMaxX
            if f >= 0.25 { XCTAssertGreaterThanOrEqual(s.minX - codexMaxX, 0, "no overlap with Codex at \(f)") }
            if f >= 0.5 { XCTAssertGreaterThanOrEqual(s.minX - codexMaxX, 2, "≥ 2 pt clear at \(f)") }
        }
        XCTAssertEqual(clear[100]!, IslandBarLayout.circleSpacing, accuracy: 0.2, "spring ~settled at 0.35 s")
        let landed = IslandMotion.harnessFrame(final: sam, progress: 1, trailingGap: gap)
        XCTAssertEqual(landed.minX, sam.minX, accuracy: 0.001)
        XCTAssertEqual(landed.width, sam.width, accuracy: 0.001)
        let mid = IslandMotion.harnessFrame(final: final[1], progress: 1, trailingGap: IslandBarLayout.circleSpacing)
        XCTAssertEqual(mid.minX, final[1].minX, accuracy: 0.001, "lands exactly for a circle after another circle too")
        // Turn-on: Samantha enters at 0, Codex moves 0 → 1.
        let on = model.previousSourceGroupIDs
        let samOn = IslandMotion.harnessDrawOrder(id: IslandSourceGroups.samantha, slot: 0, previous: on)
        let codexOn = IslandMotion.harnessDrawOrder(id: model.sourceGroups[1].id, slot: 1, previous: on)
        XCTAssertGreaterThan(samOn, codexOn, "entering circle above the neighbour it pushes")
        // Turn-off: Samantha leaves, keeping her last rank (slot 0); Codex moves 1 → 0.
        model.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        let off = model.previousSourceGroupIDs
        XCTAssertEqual(off.first, IslandSourceGroups.samantha)
        let codexOff = IslandMotion.harnessDrawOrder(id: model.sourceGroups[0].id, slot: 0, previous: off)
        XCTAssertGreaterThan(samOn, codexOff, "leaving circle stays above the neighbour sliding back")
        print(String(format: "harness clearance to Codex: 25%% %.2f pt, 50%% %.2f pt, 75%% %.2f pt", clear[25]!, clear[50]!, clear[75]!))
    }

    func testScaleAnchorIsTheDrawnCircleCentre() {
        // Circles are offset inside the window-sized ZStack; their layout frame is 24×24 at the origin.
        let r = CGRect(x: 262, y: 3, width: 24, height: 24)
        let a = IslandMotion.harnessAnchor(r)
        XCTAssertEqual(a.x * r.width, r.midX, accuracy: 0.001)
        XCTAssertEqual(a.y * r.height, r.midY, accuracy: 0.001)
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
