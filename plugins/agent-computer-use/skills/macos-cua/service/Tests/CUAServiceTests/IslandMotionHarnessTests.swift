import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// In-process motion harness: the real island view in a borderless floating window (on a 60 Hz screen so
/// the display link ticks; never key; ignores the mouse; closed at the end). No screen
/// capture API is used: timing comes from the per-frame IslandMotionProbe, clips from cacheDisplay.
/// CUA_ISLAND_MOTION_DIR=<dir> swift test --filter IslandMotionHarnessTests
@MainActor
final class IslandMotionHarnessTests: XCTestCase {
    private var window: NSWindow!
    private var host: NSHostingView<AnyView>!
    private var dir = ""
    private let rosterRows = [IslandAgent(id: "codex:j1", name: "Codex", app: "Codex", title: "Coordinate release", status: "running", elapsed_s: 245, controls: ["stop"]),
                             IslandAgent(id: "cursor:b2", name: "Cursor", app: "Cursor", title: "Needs your decision", status: "needs_you", needs_you: true, parent_id: "codex:j1"),
                             IslandAgent(id: "grok:r3", name: "Grok Bot", app: "Comet", title: "Build failed", status: "error", parent_id: "codex:j1", controls: ["retry"]),
                             IslandAgent(id: "codex:c4", name: "Claude Code", app: "Codex", title: "Run integration tests", status: "running", elapsed_s: 30, parent_id: "codex:j1", controls: ["stop"]),
                             IslandAgent(id: "cursor:p5", name: "Cursor", app: "Cursor", title: "Review API docs", status: "running", elapsed_s: 80, controls: ["stop"]),
                             IslandAgent(id: "cursor:c6", name: "Cursor", app: "Cursor", title: "Check migration edge cases", status: "running", elapsed_s: 15, parent_id: "cursor:p5", controls: ["stop"])]

    private func mount(_ m: IslandModel, reduce: Bool = false) {
        m.systemReduceMotion = reduce
        host.rootView = AnyView(SamanthaIslandBar(model: m).environment(\.colorScheme, .dark).background(Color(white: 0.12)))
    }

    private func sleep(_ s: Double) async { try? await Task.sleep(nanoseconds: UInt64(s * 1e9)) }

    /// Run `step` once with the probe on (timing) and once with frame capture (clip).
    private func scenario(_ name: String, reduce: Bool = false, setup: (IslandModel) -> Void, step: @escaping (IslandModel) async -> Void) async throws -> String {
        // Pass 1 — timing.
        var m = IslandModel(); setup(m); mount(m, reduce: reduce); await sleep(0.6)
        IslandMotionRecorder.shared.enabled = true
        _ = IslandMotionRecorder.shared.drain()
        var ticks: [IslandMotionRecorder.Sample] = []
        // The harness window may not get WindowServer display cycles (test process has no running
        // NSApp event loop), so drive one display pass per 60 Hz tick — the same work a vsync does.
        let tick = Timer(timeInterval: 1.0 / 60, repeats: true) { [host] _ in
            MainActor.assumeIsolated {
                guard let host else { return }
                host.layoutSubtreeIfNeeded()
                if let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) { host.cacheDisplay(in: host.bounds, to: rep) }
                if let l = IslandMotionRecorder.shared.latest { ticks.append(.init(t: CACurrentMediaTime(), w: l.w, h: l.h)) }
            }
        }
        RunLoop.main.add(tick, forMode: .common)
        await step(m)
        tick.invalidate()
        _ = IslandMotionRecorder.shared.drain()
        let samples = ticks
        IslandMotionRecorder.shared.enabled = false
        var lines = ["# \(name)\(reduce ? " (Reduce Motion)" : "") — in-process probe, offscreen ticks at 60 Hz (approximate; live LG panel is 100 Hz, see live-timing.md)",
                     "segment | dim | " + IslandFrameTiming.header, "---|---|---|---|---|---|---|---|---|---|---|---|---"]
        var total = 0
        for (i, seg) in IslandFrameTiming.segments(samples).enumerated() {
            for (dim, kp) in [("w", \IslandMotionRecorder.Sample.w), ("h", \IslandMotionRecorder.Sample.h)] {
                guard let f = seg.first?[keyPath: kp], let l = seg.last?[keyPath: kp], abs(f - l) > 0.5 || seg.contains(where: { abs($0[keyPath: kp] - f) > 0.5 }),
                      let t = IslandFrameTiming.analyze(seg, dimension: kp) else { continue }
                total += t.stalls
                lines.append("\(i + 1) | \(dim) | " + t.row.replacingOccurrences(of: " | ", with: " | "))
            }
        }
        lines.append("total stalls: \(total)")
        try lines.joined(separator: "\n").appending("\n").write(toFile: "\(dir)/\(name)-timing.md", atomically: true, encoding: .utf8)
        // Pass 2 — frames (separate so capture cost can't pollute timing).
        m = IslandModel(); setup(m); mount(m, reduce: reduce); await sleep(0.6)
        let fdir = "\(dir)/frames-\(name)"
        try? FileManager.default.removeItem(atPath: fdir)
        try FileManager.default.createDirectory(atPath: fdir, withIntermediateDirectories: true)
        var n = 0
        let crop = NSRect(x: 120, y: host.isFlipped ? 0 : IslandBarLayout.windowSize.height - 240, width: 400, height: 240)
        let timer = Timer(timeInterval: 1.0 / 60, repeats: true) { [host] _ in
            MainActor.assumeIsolated {
                guard let host, let rep = host.bitmapImageRepForCachingDisplay(in: crop) else { return }
                host.cacheDisplay(in: crop, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: String(format: "%@/%04d.png", fdir, n)))
                n += 1
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        await step(m)
        timer.invalidate()
        return "\(name): \(samples.count) ticks, \(total) stalls, \(n) clip frames"
    }

    func testMotionClips() async throws {
        guard let d = ProcessInfo.processInfo.environment["CUA_ISLAND_MOTION_DIR"] else { throw XCTSkip("set CUA_ISLAND_MOTION_DIR") }
        dir = d
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)   // a WindowServer client that never takes focus or a Dock icon
        NSApp.finishLaunching()
        guard let screen = NSScreen.main else { throw XCTSkip("no screen") }
        let size = IslandBarLayout.windowSize
        window = NSWindow(contentRect: NSRect(x: screen.frame.minX + 40, y: screen.frame.minY + 40, width: size.width, height: size.height),
                          styleMask: [.borderless], backing: .buffered, defer: false)
        // Floating so it is never occluded (occluded windows get no SwiftUI frames).
        window.level = .floating
        // Opaque on purpose: WindowServer marks near-transparent windows occluded and SwiftUI then stops
        // producing frames. Shown ~1 min in the screen's bottom-left corner, never key, then closed.
        window.ignoresMouseEvents = true
        window.isOpaque = false
        window.backgroundColor = .clear
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        host = NSHostingView(rootView: AnyView(EmptyView()))
        host.frame = NSRect(origin: .zero, size: size)
        window.contentView = host
        window.orderFrontRegardless()
        await sleep(1.0)
        let occl = "occlusion visible: \(window.occlusionState.contains(.visible))"
        defer { window.orderOut(nil); window.close() }
        IslandPrewarm.run()   // production does the same 2 s after launch
        IslandModel.hudDwell = 0.9; IslandModel.peekDwell = 1.2
        defer { IslandModel.hudDwell = 1.2; IslandModel.peekDwell = 3 }
        let tmp = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/macos-cua/motion-tray")
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.trashItem(at: tmp, resultingItemURL: nil) }
        let files = try ["Report.pdf", "notes.txt", "mock.png"].map { n -> URL in
            let u = tmp.appendingPathComponent(n); try "x".write(to: u, atomically: true, encoding: .utf8); return u
        }
        let rows = rosterRows
        let approval = IslandAgentApproval(id: "cursor:branches", agent: "Cursor", app: "Cursor",
                                           title: "Delete 3 stale branches?",
                                           subtitle: "Targets: feature/cache, stale/logging, migration/legacy",
                                           destructive: true)
        var out: [String] = [occl]
        // CUA_ISLAND_MOTION_ONLY=peek,agents runs just those scenarios (both motion modes).
        let only = ProcessInfo.processInfo.environment["CUA_ISLAND_MOTION_ONLY"].map { Set($0.split(separator: ",").map(String.init)) }
        func run(_ n: String) -> Bool { only == nil || only!.contains(n) }
        for reduce in [false, true] {
            let sfx = reduce ? "-reduce" : ""
            if run("conversation") {
                out.append(try await scenario("conversation\(sfx)", reduce: reduce, setup: { m in
                    m.setNowPlaying(NowPlayingTests.track(), visible: true)
                }) { m in
                    for kind in ["thinking", "speaking", "listening", "acting", "done", "listening"] {
                        m.apply(IslandSnapshot(kind: kind, title: kind == "done" ? "Calculator is open" : kind))
                        await self.sleep(0.45)
                    }
                })
            }
            if run("hud") {
            out.append(try await scenario("hud\(sfx)", reduce: reduce, setup: { _ in }) { m in
                m.showHUD(.init(kind: .volume, value: 0.6875)); await self.sleep(0.4)
                m.showHUD(.init(kind: .volume, value: 0.75)); await self.sleep(1.6)
                m.showHUD(.init(kind: .volume, value: 0.8125)); await self.sleep(1.6)   // warm second run
            })
            }
            if run("peek") {
            out.append(try await scenario("peek\(sfx)", reduce: reduce, setup: { _ in }) { m in
                m.showPeek(IslandPeekItem(id: "p", source: "Grok Bot", symbol: "checkmark.seal.fill", title: "Build finished")); await self.sleep(2.0)
            })
            }
            if run("duo") {
            out.append(try await scenario("duo\(sfx)", reduce: reduce, setup: { m in m.setNowPlaying(NowPlayingTests.track(), visible: true) }) { m in
                m.addToTray(files); await self.sleep(0.8)
                m.tapSecondary(); await self.sleep(0.8)
                m.tapSecondary(); await self.sleep(0.8)
            })
            }
            if run("tray") {
            out.append(try await scenario("tray\(sfx)", reduce: reduce, setup: { _ in }) { m in
                m.setTrayTargeted(true); await self.sleep(1.2)
                m.addToTray(files); m.setTrayTargeted(false); await self.sleep(1.0)
                m.toggleCard(); await self.sleep(1.0)
                m.toggleCard(); await self.sleep(1.0)
            })
            }
            if run("agents") {
            out.append(try await scenario("agents\(sfx)", reduce: reduce, setup: { m in m.idleActivity = "agents"; m.apply(IslandSnapshot(kind: "idle", agents: rows)) }) { m in
                await self.sleep(0.4)  // capture the stable compact attention summary before opening the roster
                m.toggleCard(); await self.sleep(0.9)
                m.tapRowControl(rows[0]); await self.sleep(0.4)
                m.toggleCard(); await self.sleep(0.9)
            })
            }
            if run("approval") {
            out.append(try await scenario("approval\(sfx)", reduce: reduce, setup: { m in
                m.apply(IslandSnapshot(kind: "idle", agent_approval: approval))
                m.openApprovalFromRoster()
            }) { m in
                m.performCardAction(.agentApprove(approval.id))
                await self.sleep(0.4)
            })
            }
        }
        try out.joined(separator: "\n").appending("\n").write(toFile: "\(dir)/summary\(only == nil ? "" : "-partial").txt", atomically: true, encoding: .utf8)
    }

}
