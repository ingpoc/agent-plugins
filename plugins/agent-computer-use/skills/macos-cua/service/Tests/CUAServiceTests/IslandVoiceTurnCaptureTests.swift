import AppKit
import QuartzCore
import SwiftUI
import XCTest
@testable import CUAService

/// Real frames of one Samantha conversation turn from the live view, hosted in an offscreen borderless
/// window of the test process (never on screen, no input, no URLs, no clipboard). Fixture data only, in the
/// order voice_cua posts it (realtime_session.py + island_facade.py + plugin_island.py, the 13:27 IST turn).
/// CUA_ISLAND_TURN_DIR=<dir> swift test --filter IslandVoiceTurnCaptureTests
@MainActor
final class IslandVoiceTurnCaptureTests: XCTestCase {
    static func acu(_ app: String, _ t: Double) -> IslandPluginUse {
        var u = IslandPluginUse(plugin: "acu", app: "Agent Computer Use", agent: "Samantha", last_seen_at: t)
        u.target_app = app
        return u
    }

    static let bars: [Double] = [0.35, 0.8, 0.55, 0.95, 0.4]

    /// (label, snapshot) in posting order.
    static func turn(apps: [String]) -> [(String, IslandSnapshot)] {
        let uses = apps.enumerated().map { acu($0.element, 300 + Double($0.offset)) }
        let last = apps.last ?? ""
        return [
            ("listening-user", IslandSnapshot(kind: "listening", title: "Listening", voice_side: "user", voice_level: 0.7,
                                              voice_levels: bars, samantha_on: true)),
            ("thinking", IslandSnapshot(kind: "thinking", title: "Thinking", samantha_on: true)),
            ("acting-posted", IslandSnapshot(kind: "acting", title: "Using \(last)", app: last, active_apps: apps,
                                             samantha_on: true)),
            ("acting-plugin", IslandSnapshot(kind: "acting", title: "Using \(last)", app: last, active_apps: apps,
                                             plugins: uses, samantha_on: true)),
            ("done", IslandSnapshot(kind: "done", title: "Done", app: last, active_apps: apps, plugins: uses, samantha_on: true)),
            ("speaking", IslandSnapshot(kind: "speaking", title: "Speaking", active_apps: apps, voice_side: "assistant",
                                        voice_level: 0.8, voice_levels: bars, plugins: uses, samantha_on: true)),
            ("speaking-plugin-dropped", IslandSnapshot(kind: "speaking", title: "Speaking", active_apps: apps,
                                                       voice_side: "assistant", voice_level: 0.8, voice_levels: bars,
                                                       samantha_on: true)),
            ("barge-in-listening", IslandSnapshot(kind: "listening", title: "Listening", active_apps: apps, voice_side: "user",
                                                  voice_level: 0.6, voice_levels: bars, samantha_on: true)),
            ("idle", IslandSnapshot(kind: "idle", title: "Resting", samantha_on: true)),
        ]
    }

    static func plugin(_ id: String, _ t: Double) -> IslandPluginUse {
        IslandPluginUse(plugin: id, app: id, agent: "Samantha", last_seen_at: t)
    }

    static func voice(_ kind: String, apps: [String], extra: [IslandPluginUse] = []) -> IslandSnapshot {
        IslandSnapshot(kind: kind, title: kind.capitalized, active_apps: apps, voice_side: kind == "speaking" ? "assistant" : "user",
                       voice_level: 0.7, voice_levels: bars,
                       plugins: apps.enumerated().map { acu($0.element, 300 + Double($0.offset)) } + extra, samantha_on: true)
    }

    /// Transition strips (Guru 13:31 IST): each starts from the first snapshot.
    static func transitions() -> [(String, [String], [(String, IslandSnapshot)])] {
        let on = IslandSnapshot(kind: "listening", title: "Listening", voice_side: "user", voice_level: 0.7, voice_levels: bars, samantha_on: true)
        let thinking = IslandSnapshot(kind: "thinking", title: "Thinking", samantha_on: true)
        let speaking = IslandSnapshot(kind: "speaking", title: "Speaking", voice_side: "assistant", voice_level: 0.8, voice_levels: bars, samantha_on: true)
        let idleOn = IslandSnapshot(kind: "idle", title: "Resting", samantha_on: true)
        let calc = ["Calculator"], three = ["Calculator", "TextEdit", "Notes"]
        let acting = IslandSnapshot(kind: "acting", title: "Using Calculator", app: "Calculator", active_apps: calc,
                                    plugins: [acu("Calculator", 300)], samantha_on: true)
        let ledger = plugin("context-ledger", 400)
        return [
            ("samantha-on-off", [], [("off", IslandSnapshot(kind: "idle")), ("on-listening", on), ("off", IslandSnapshot(kind: "idle"))]),
            ("voice-cycle", [], [("listening", on), ("thinking", thinking), ("speaking", speaking), ("idle", idleOn)]),
            ("speaking-acting", calc, [("speaking", speaking), ("acting", acting), ("speaking-controlled", voice("speaking", apps: calc)),
                                        ("speaking-row-dropped", speaking)]),
            ("app-added-removed", three, [("speaking-1", voice("speaking", apps: calc)), ("speaking-3", voice("speaking", apps: three)),
                                           ("speaking-1", voice("speaking", apps: calc))]),
            ("plugin-marks-in-voice", calc, [("speaking-acu", voice("speaking", apps: calc)),
                                              ("ledger-joins", voice("speaking", apps: calc, extra: [ledger])),
                                              ("acu-leaves", IslandSnapshot(kind: "speaking", title: "Speaking", voice_side: "assistant", voice_level: 0.8,
                                                                            voice_levels: bars, plugins: [ledger], samantha_on: true)),
                                              ("ledger-leaves", speaking)]),
            ("barge-in", calc, [("speaking-controlled", voice("speaking", apps: calc)), ("barge-in-listening", voice("listening", apps: calc)),
                                ("speaking", speaking), ("barge-in-listening", on)]),
            // Guru 15:26 / 15:27 IST: a ringed target with no apps (ring · 20 · dot) and the bare logo in the ring.
            ("targets", ["Finder"], [
                ("comet-only", IslandSnapshot(kind: "acting", title: "Reading page", plugins: [plugin("comet-control", 400)], samantha_on: true)),
                ("apple-only", IslandSnapshot(kind: "acting", title: "Using the Mac", plugins: [acu("", 300)], samantha_on: true)),
                ("apple-finder", IslandSnapshot(kind: "acting", title: "Using Finder", app: "Finder", active_apps: ["Finder"],
                                                plugins: [acu("Finder", 300)], samantha_on: true)),
                ("comet-speaking", voice("speaking", apps: [], extra: [plugin("comet-control", 400)])),
            ]),
        ]
    }

    func testCaptureVoiceTurnFrames() async throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_TURN_DIR"] else {
            throw XCTSkip("set CUA_ISLAND_TURN_DIR to capture")
        }
        let plugins = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../../").standardized
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        var manifest: [[String: Any]] = []
        var plan: [(String, Bool, [(String, IslandSnapshot)])] = [
            ("turn-1app", false, Self.turn(apps: ["Calculator"])), ("turn-3apps", false, Self.turn(apps: ["Calculator", "TextEdit", "Notes"])),
            ("turn-1app-reduce", true, Self.turn(apps: ["Calculator"]))]
        for (name, _, steps) in Self.transitions() {
            plan.append(("transitions-\(name)", false, steps))
            plan.append(("transitions-\(name)-reduce", true, steps))
        }
        if let only = ProcessInfo.processInfo.environment["CUA_ISLAND_TURN_ONLY"] { plan = plan.filter { $0.0.hasPrefix(only) } }
        for (scenario, reduce, steps) in plan {
            let model = IslandModel()
            model.idleActivity = "duo"
            model.systemReduceMotion = reduce
            model.apply(scenario.hasPrefix("turn") ? IslandSnapshot(kind: "idle", title: "Resting", samantha_on: true) : steps[0].1)
            let root = SamanthaIslandBar(model: model).environment(\.colorScheme, .dark).background(Color(white: 0.18))
            let host = NSHostingView(rootView: root)
            let size = IslandBarLayout.windowSize
            host.frame = CGRect(origin: .zero, size: size)
            let win = NSWindow(contentRect: CGRect(x: -8000, y: -8000, width: size.width, height: size.height),
                               styleMask: [.borderless], backing: .buffered, defer: false)
            win.isReleasedWhenClosed = false
            win.contentView = host
            win.orderBack(nil)
            defer { win.orderOut(nil); win.close() }
            // Yield the main actor (not a nested run loop) so the model's pinch Task and SwiftUI transactions run.
            func spin(_ s: Double) async { try? await Task.sleep(nanoseconds: UInt64(s * 1_000_000_000)) }
            func grab() -> NSBitmapImageRep? {
                guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return nil }
                host.cacheDisplay(in: host.bounds, to: rep)
                return rep
            }
            await spin(1.0)
            for (si, (label, snap)) in steps.enumerated() {
                let t0 = CACurrentMediaTime()
                model.apply(snap)
                var frames: [String] = []
                for (fi, p) in [0.0, 0.25, 0.5, 0.75, 1.0].enumerated() {
                    while CACurrentMediaTime() - t0 < p * 0.4 { await spin(0.001) }
                    if fi == 4 { await spin(0.5) }
                    guard let rep = grab() else { return XCTFail("rep") }
                    let ms = Int(((CACurrentMediaTime() - t0) * 1000).rounded())
                    let name = String(format: "%@-%02d-%@-%d-%04dms.png", scenario, si, label, fi, ms)
                    try rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
                    frames.append(name)
                }
                let shape = model.shapeRect
                let overlays = IslandLayoutLogger.overlays(model: model, shape: shape, origin: .zero).map { o -> [String: Any] in
                    ["kind": o.kind, "x": o.rect.x, "w": o.rect.w, "inside": o.expectedInsideIsland]
                }
                manifest.append(["scenario": scenario, "step": si, "label": label, "presentation": "\(model.presentation)",
                                 "shape_x": shape.minX, "shape_w": shape.width, "frames": frames, "overlays": overlays])
            }
        }
        let data = try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: URL(fileURLWithPath: dir).appendingPathComponent("manifest.json"))
    }
}
