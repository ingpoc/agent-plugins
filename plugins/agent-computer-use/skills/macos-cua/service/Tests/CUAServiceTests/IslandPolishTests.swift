import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// P7 polish: Reduce Motion, frame timing, keyboard nav + VoiceOver labels, peek privacy, row ticker.
@MainActor
final class IslandPolishTests: XCTestCase {
    func testReduceMotionSwapsEverySpringForCrossfade() {
        for open in [true, false] {
            XCTAssertEqual(IslandMotion.open(cardOpen: open, reduce: true), IslandMotion.crossfade)
            XCTAssertEqual(IslandMotion.target(open, reduce: true), IslandMotion.crossfade)
        }
        XCTAssertEqual(IslandMotion.width(reduce: true), IslandMotion.crossfade)
        XCTAssertEqual(IslandMotion.pop(reduce: true), IslandMotion.crossfade)
        XCTAssertFalse(IslandMotion.usesScale(reduce: true))
        XCTAssertEqual(IslandMotion.open(cardOpen: true, reduce: false), .spring(response: 0.36, dampingFraction: 0.7))
        XCTAssertEqual(IslandMotion.open(cardOpen: false, reduce: false), .spring(response: 0.45, dampingFraction: 1.0))
        XCTAssertEqual(IslandMotion.width(reduce: false), .smooth(duration: 0.35))
        let m = IslandModel()
        XCTAssertEqual(m.systemReduceMotion, NSWorkspace.shared.accessibilityDisplayShouldReduceMotion, "mirrors AppKit at launch")
    }

    func testFrameTimingAnalysis() {
        typealias S = IslandMotionRecorder.Sample
        let f = 1.0 / 60
        var s = (0..<20).map { S(t: Double($0) * f, w: 74 + CGFloat($0) * 7, h: 24) }
        s.append(S(t: 20 * f, w: 210, h: 24)); s.append(S(t: 21 * f, w: 207, h: 24)); s.append(S(t: 22 * f, w: 207.2, h: 24))
        let t = IslandFrameTiming.analyze(s)!
        XCTAssertEqual(t.stalls, 0)
        XCTAssertEqual(t.peak, 210); XCTAssertEqual(t.overshootPt, 2.8, accuracy: 0.01)
        var g = s; g.insert(S(t: 5.5 * f, w: 110, h: 24), at: 6); g.remove(at: 5)   // ~2.5-frame gap
        g = g.enumerated().map { i, x in i == 5 ? S(t: 7 * f, w: x.w, h: 24) : x }
        XCTAssertGreaterThanOrEqual(IslandFrameTiming.analyze(g.sorted { $0.t < $1.t })!.stalls, 1)
        XCTAssertEqual(IslandFrameTiming.segments(s + [S(t: 2, w: 74, h: 24), S(t: 2 + f, w: 80, h: 24)]).count, 2)
        let r = IslandMotionRecorder.shared
        r.enabled = true; r.note(1, 1); r.note(1, 1); r.note(2, 1)
        XCTAssertEqual(r.drain().count, 2, "dedupes identical frames"); r.enabled = false
    }

    func testKeyboardNavAgentRowsAndAnnouncements() {
        let m = IslandModel()
        let rows = [IslandAgent(id: "codex:a", name: "Codex", title: "Migrate", status: "running", controls: ["stop"]),
                    IslandAgent(id: "codex:b", name: "Codex", title: "Build", status: "error", controls: ["retry"]),
                    IslandAgent(id: "codex:c", name: "Codex", title: "Lint", status: "done")]
        m.idleActivity = "agents"
        m.apply(IslandSnapshot(kind: "idle", agents: rows))
        var said: [String] = [], sent: [String] = []
        m.onAnnounce = { said.append($0) }
        m.onRowControl = { a, act in sent.append("\(a.id)=\(act.rawValue)") }
        m.hotKeyPressed()
        XCTAssertTrue(m.keyboardActive); XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.navTargets, [.row("codex:a"), .row("codex:b")], "rows without a control are skipped")
        XCTAssertEqual(said.last, "Stop Codex: Migrate")
        XCTAssertTrue(m.handleKey(keyCode: 125, characters: "", shift: false))   // ↓
        XCTAssertEqual(m.navTarget, .row("codex:b")); XCTAssertEqual(said.last, "Retry Codex: Build")
        XCTAssertTrue(m.handleKey(keyCode: 125, characters: "", shift: false))   // wraps
        XCTAssertEqual(m.navTarget, .row("codex:a"))
        XCTAssertTrue(m.handleKey(keyCode: 36, characters: "\r", shift: false))  // Return arms Stop
        XCTAssertTrue(sent.isEmpty); XCTAssertEqual(said.last, "Confirm stop Codex")
        XCTAssertTrue(m.handleKey(keyCode: 36, characters: "\r", shift: false))
        XCTAssertEqual(sent, ["codex:a=stop"])
        XCTAssertFalse(m.handleKey(keyCode: 0, characters: "a", shift: false), "letters pass through")
        XCTAssertTrue(m.handleKey(keyCode: 53, characters: "", shift: false))    // Esc
        XCTAssertFalse(m.keyboardActive); XCTAssertFalse(m.cardOpen)
    }

    func testKeyboardDecisionStartsOnPrimary() {
        let m = IslandModel()
        var acts: [IslandCardAction] = []
        m.onAction = { acts.append($0) }
        m.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "grok:x", agent: "Grok Bot", app: "Comet", title: "Deploy?")))
        m.hotKeyPressed()
        XCTAssertEqual(m.navTargets.count, 2)
        XCTAssertEqual(m.navLabel(m.navTarget!), "Approve")
        XCTAssertTrue(m.handleKey(keyCode: 123, characters: "", shift: false))  // ←
        XCTAssertEqual(m.navLabel(m.navTarget!), "Deny")
        XCTAssertTrue(m.handleKey(keyCode: 124, characters: "", shift: false))  // →
        XCTAssertTrue(m.handleKey(keyCode: 36, characters: "\r", shift: false))
        XCTAssertEqual(acts, [.agentApprove("grok:x")])
        for t in m.navTargets { XCTAssertFalse(m.navLabel(t).isEmpty, "VoiceOver label for \(t)") }
    }

    func testCompactNavOpensAndSwaps() {
        let m = IslandModel()
        m.setNowPlaying(NowPlayingTests.track(), visible: true)
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/macos-cua/test-nav-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.trashItem(at: dir, resultingItemURL: nil) }
        let f = dir.appendingPathComponent("a.txt"); try? "x".write(to: f, atomically: true, encoding: .utf8)
        m.addToTray([f])
        XCTAssertEqual(m.secondary, .tray)
        m.keyboardActive = true; m.navIndex = 1
        XCTAssertEqual(m.navTargets, [.open, .secondary])
        XCTAssertFalse(m.navLabel(.secondary).isEmpty)
        XCTAssertTrue(m.handleKey(keyCode: 36, characters: "\r", shift: false))
        XCTAssertEqual(m.presentation, .tray, "Return on the secondary swaps")
        m.keyboardActive = false
    }

    func testPeekPrivacyGateAndCaptureHook() {
        XCTAssertFalse(IslandPrivacy.peeksAllowed(sharing: true, focused: false, hideWhileSharing: true, respectFocus: true))
        XCTAssertTrue(IslandPrivacy.peeksAllowed(sharing: true, focused: false, hideWhileSharing: false, respectFocus: true))
        XCTAssertFalse(IslandPrivacy.peeksAllowed(sharing: false, focused: true, hideWhileSharing: true, respectFocus: true))
        XCTAssertTrue(IslandPrivacy.peeksAllowed(sharing: false, focused: true, hideWhileSharing: true, respectFocus: false))
        XCTAssertTrue(IslandPrivacy.processRunning([ProcessInfo.processInfo.processName]), "libproc scan sees this process")
        XCTAssertFalse(IslandPrivacy.processRunning(["no-such-proc-p7"]))
        let m = IslandModel()
        var vis: [Bool] = []
        m.onPeekVisible = { vis.append($0) }
        m.peekGate = { false }
        m.showPeek(IslandPeekItem(id: "1", source: "Grok Bot", title: "Secret"))
        XCTAssertNil(m.peek, "gated peek is dropped")
        m.peekGate = { true }
        m.showPeek(IslandPeekItem(id: "1", source: "Grok Bot", title: "Hi"))
        m.showPeek(IslandPeekItem(id: "2", source: "Grok Bot", title: "Again"))
        m.dismissPeek()
        XCTAssertEqual(vis, [true, false], "capture exclusion on while a peek is up, once")
    }

    func testRunningTicker() {
        let a = IslandAgent(id: "x", status: "running", elapsed_s: 58)
        XCTAssertEqual(IslandAgentRows.statusText(a), "running 58s")
        XCTAssertEqual(IslandAgentRows.statusText(a, elapsedExtra: 3), "running 1m")
        XCTAssertEqual(IslandAgentRows.statusText(IslandAgent(id: "y", status: "error"), elapsedExtra: 9), "failed")
        let m = IslandModel()
        let t0 = m.agentsAt
        m.apply(IslandSnapshot(kind: "idle", agents: [a]), now: t0.addingTimeInterval(5))
        XCTAssertEqual(m.agentsAt, t0.addingTimeInterval(5))
        m.apply(IslandSnapshot(kind: "idle", agents: [a]), now: t0.addingTimeInterval(9))
        XCTAssertEqual(m.agentsAt, t0.addingTimeInterval(5), "same rows keep their clock")
    }

    func testUIReviewRound1Rules() {
        // Ticker: per second under a minute, then on each row's minute boundary.
        let t0 = Date(timeIntervalSinceReferenceDate: 1000)
        XCTAssertEqual(IslandAgentTicker.next(after: t0, start: t0, bases: [30]).timeIntervalSince(t0), 1, accuracy: 0.001)
        XCTAssertEqual(IslandAgentTicker.next(after: t0, start: t0, bases: [245]).timeIntervalSince(t0), 55, accuracy: 0.001)
        XCTAssertEqual(IslandAgentTicker.next(after: t0, start: t0, bases: [245, 58]).timeIntervalSince(t0), 1, accuracy: 0.001)
        XCTAssertEqual(IslandAgentTicker.next(after: t0.addingTimeInterval(2.5), start: t0, bases: [58]).timeIntervalSince(t0), 62, accuracy: 0.001)
        XCTAssertEqual(IslandAgentRows.statusText(IslandAgent(id: "r", status: "running", elapsed_s: 245)), "running 4m")
        // Peek: no "now"; time only after a minute; success glyph is green.
        XCTAssertNil(IslandPeekRules.ageLabel(t0, now: t0.addingTimeInterval(59)))
        XCTAssertEqual(IslandPeekRules.ageLabel(t0, now: t0.addingTimeInterval(60)), "1m")
        XCTAssertTrue(IslandPeekRules.isSuccess("checkmark.seal.fill")); XCTAssertFalse(IslandPeekRules.isSuccess("bell.fill"))
        // Focus ring: 2 pt gap + 2 pt ring; tiles follow radius 7.
        XCTAssertEqual(IslandFocusShape.outset, 4); XCTAssertEqual(IslandFocusShape.tile, .rounded(7))
        // Timing columns: overshoot % of travel and time to peak.
        typealias S = IslandMotionRecorder.Sample
        let s = [S(t: 0, w: 100, h: 24), S(t: 0.1, w: 180, h: 24), S(t: 0.25, w: 206, h: 24), S(t: 0.4, w: 200.3, h: 24), S(t: 0.5, w: 200, h: 24)]
        let f = IslandFrameTiming.analyze(s)!
        XCTAssertEqual(f.overshootPct, 6, accuracy: 0.01); XCTAssertEqual(f.peakMs, 250, accuracy: 0.5); XCTAssertEqual(f.settleMs, 400, accuracy: 0.5)
        // Shape path interpolates rect + radius (render-time; content keeps pinned frames).
        var p = IslandShapePath(rect: CGRect(x: 0, y: 0, width: 74, height: 24), radius: 12)
        p.animatableData = IslandShapePath(rect: CGRect(x: 10, y: 6, width: 340, height: 96), radius: 22).animatableData
        XCTAssertEqual(p.rect, CGRect(x: 10, y: 6, width: 340, height: 96)); XCTAssertEqual(p.radius, 22)
        XCTAssertEqual(IslandMotion.contentOut, .easeOut(duration: 0.12))
    }

    func testTrayThumbnailBlankFallbackAndCardHeight() {
        func img(_ fill: NSColor) -> CGImage {
            let ctx = CGContext(data: nil, width: 96, height: 96, bitsPerComponent: 8, bytesPerRow: 0,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            ctx.setFillColor(fill.cgColor); ctx.fill(CGRect(x: 0, y: 0, width: 96, height: 96))
            ctx.setFillColor(NSColor.black.cgColor); ctx.fill(CGRect(x: 10, y: 80, width: 40, height: 4))   // one text line
            return ctx.makeImage()!
        }
        XCTAssertTrue(IslandTrayRules.isMostlyBlank(img(.white)), "white page with a line of text → icon")
        XCTAssertFalse(IslandTrayRules.isMostlyBlank(img(.systemIndigo)), "a real picture keeps its thumbnail")
        XCTAssertEqual(IslandTrayRules.buttonTop - (IslandTrayRules.thumbTop + IslandTrayRules.tile + 4 + IslandTrayRules.nameHeight), 12)
        XCTAssertEqual(IslandTrayRules.cardHeight, 168)
        let m = IslandModel(); m.addToTray([URL(fileURLWithPath: "/tmp/x.txt")]); m.toggleCard()
        XCTAssertEqual(m.cardHeight, 168, "tray card is shorter than the 176 default")
    }
}
