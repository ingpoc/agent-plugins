import AppKit
import XCTest
@testable import CUAService

/// Opt-in visual check (skipped unless CUA_ISLAND_CAPTURE_DIR and
/// CUA_ISLAND_CAPTURE_PORT are set): streams a real gateway into the real
/// IslandController/menu-bar panel, publishes each state via
/// /api/island/publish and captures the compact view and the card.
/// Non-activating panel — never takes focus.
@MainActor
final class IslandCaptureHarness: XCTestCase {
    private var port = 0
    private var controller: IslandController?
    /// Gateway-like merge (agents / approval / meters persist unless given; {} clears approval).
    private var state: [String: Any] = ["kind": "idle"]

    /// Feeds the controller's island hub as source=samantha (the voice helper's path), no network.
    private func post(_ path: String, _ body: [String: Any]) async throws {
        if path == "/api/island/voice" {
            state.merge(body) { $1 }
        } else {
            let keep = ["agents", "agent_approval", "voice_side", "voice_level", "voice_levels"]
            var next = body
            for k in keep where next[k] == nil { next[k] = state[k] }
            if let a = next["agent_approval"] as? [String: Any], a.isEmpty { next["agent_approval"] = nil }
            state = next.compactMapValues { $0 }
        }
        let data = try JSONSerialization.data(withJSONObject: state)
        let snap = try JSONDecoder().decode(IslandSnapshot.self, from: data)
        controller?.hub.ingestSamantha(snap)
    }

    private func capture(_ dir: String, _ name: String, tall: Bool) throws {
        let size = NSSize(width: 640, height: tall ? 200 : 40)
        let originX = Int(max(0, ((NSScreen.main?.frame.width ?? size.width) - size.width) / 2))
        let imageURL = URL(fileURLWithPath: dir).appendingPathComponent(name + ".png")
        try? FileManager.default.removeItem(at: imageURL)
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        proc.arguments = ["-x", "-R", "\(originX),0,\(Int(size.width)),\(Int(size.height))", imageURL.path]
        try proc.run()
        proc.waitUntilExit()
        guard proc.terminationStatus == 0, let image = NSImage(contentsOf: imageURL), image.size.width > 0, image.size.height > 0 else {
            throw NSError(domain: "IslandCaptureHarness", code: Int(proc.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: "screen capture failed; check Screen Recording access for the test runner"])
        }
    }

    private func meters(side: String) async throws {
        for i in 0..<14 {
            let lv = 0.25 + 0.6 * abs(sin(Double(i) * 0.9))
            let bars = (0..<8).map { j in 0.2 + 0.75 * abs(sin(Double(i + j) * 0.7)) }
            try await post("/api/island/voice", ["voice_side": side, "voice_level": lv, "voice_levels": bars])
            try await Task.sleep(nanoseconds: 110_000_000)
        }
    }

    func testCaptureStates() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let dir = env["CUA_ISLAND_CAPTURE_DIR"], let portText = env["CUA_ISLAND_CAPTURE_PORT"],
              let p = Int(portText) else {
            throw XCTSkip("set CUA_ISLAND_CAPTURE_DIR and CUA_ISLAND_CAPTURE_PORT to capture")
        }
        port = p
        let controller = IslandController(gatewayPort: port, hubPort: nil)
        self.controller = controller
        let model = controller.debugModel
        controller.start()
        controller.startStreaming()
        try await Task.sleep(nanoseconds: 1_500_000_000)
        let five = ["Calculator", "TextEdit", "Notes", "Safari", "Comet"]
        let agents: [[String: Any]] = [
            ["id": "a1", "name": "Cursor", "app": "Cursor", "title": "Refactor island layout", "status": "running", "elapsed_s": 340],
            ["id": "a2", "name": "Codex", "app": "Codex", "title": "Migration plan", "status": "needs_you", "needs_you": true, "elapsed_s": 240],
            ["id": "a3", "name": "Grok Bot", "app": "Comet", "title": "Research Alcove", "status": "done", "elapsed_s": 1260],
        ]
        let none: [[String: Any]] = []
        let cases: [(String, [String: Any], String)] = [
            ("00-idle", ["kind": "ready", "title": "Samantha", "active_apps": [], "agents": none, "agent_approval": [:]], ""),
            ("01-listening", ["kind": "listening", "title": "Listening", "detail": "What's 12 times 7", "active_apps": []], "user"),
            ("02-thinking", ["kind": "thinking", "title": "What's 12 times 7?", "detail": "Thinking"], ""),
            ("03-speaking", ["kind": "speaking", "title": "It's 84", "detail": "12 times 7 is 84. Want me to put it in Calculator?"], "assistant"),
            ("04-acting", ["kind": "acting", "title": "Opening netflix.com", "detail": "Comet", "app": "Comet",
                           "active_apps": five.filter { $0 != "Comet" } + ["Comet"]], ""),
            ("04b-acting-steps", ["kind": "acting", "title": "Opening netflix.com", "app": "Comet", "step_index": 2, "step_total": 5,
                                  "active_apps": ["Comet"]], ""),
            ("05-needs-you", ["kind": "confirm", "title": "Allow Keychain access?", "confirm_id": "demo-confirm",
                              "confirm_prompt": "Samantha wants the Netflix password to sign you in.", "active_apps": ["Comet"]], ""),
            ("06-error", ["kind": "error", "title": "label not found: Sign In · RPC -32000 trace", "app": "Calculator",
                          "active_apps": ["Calculator"]], ""),
            ("07-done", ["kind": "done", "title": "84", "detail": "12 × 7", "app": "Calculator", "active_apps": ["Calculator"]], ""),
            ("08-agent-approval", ["kind": "listening", "title": "Listening", "active_apps": [],
                                   "agent_approval": ["id": "m1", "agent": "Codex", "app": "Codex", "title": "Delete 3 stale branches?",
                                                      "subtitle": "Targets: feature/cache, stale/logging, migration/legacy", "destructive": true]], ""),
            ("09-multi-agent", ["kind": "listening", "title": "Listening", "active_apps": ["Comet"], "app": "",
                                "agents": agents, "agent_approval": [:]], "user"),
        ]
        for (name, body, side) in cases {
            model.forceCompact = true
            try await post("/api/island/publish", body)
            try await Task.sleep(nanoseconds: 600_000_000)
            if !side.isEmpty { try await meters(side: side) } else { try await Task.sleep(nanoseconds: 500_000_000) }
            try capture(dir, "\(name)-compact", tall: false)
            model.forceCompact = false
            if name == "09-multi-agent" { model.openAgentList() } else if !model.cardOpen { model.toggleCard() }
            if !side.isEmpty { try await meters(side: side) } else { try await Task.sleep(nanoseconds: 1_100_000_000) }
            try capture(dir, "\(name)-card", tall: true)
            if model.cardOpen && !model.isSticky { model.toggleCard() }
        }
        // Benign Realtime race never shows.
        model.forceCompact = true
        try await post("/api/island/publish", ["kind": "error", "title": "Realtime error",
                                               "detail": "Audio content of 1850ms is already shorter than 8640ms"])
        try await Task.sleep(nanoseconds: 900_000_000)
        try capture(dir, "10-benign-error-suppressed-compact", tall: false)
        controller.stopStreaming()
    }

    /// Opt-in 60 fps film of the acting card opening and closing (CUA_ISLAND_FILM=<dir>).
    /// Leave is emulated exactly as the panel does it (leaveDelay, then pointerLeft) so the
    /// real pointer is never moved. Writes film.mp4 (wall-clock pts) and marks.json.
    func testFilmActingCard() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let dir = env["CUA_ISLAND_FILM"], let portText = env["CUA_ISLAND_CAPTURE_PORT"], let p = Int(portText) else {
            throw XCTSkip("set CUA_ISLAND_FILM and CUA_ISLAND_CAPTURE_PORT to film")
        }
        port = p
        let controller = IslandController(gatewayPort: port, hubPort: nil)
        self.controller = controller
        let model = controller.debugModel
        controller.start()
        controller.startStreaming()
        try await Task.sleep(nanoseconds: 1_500_000_000)
        try await post("/api/island/publish", ["kind": "acting", "title": "Opening netflix.com", "detail": "Comet", "app": "Comet",
                                               "active_apps": ["Comet"], "agents": [] as [Any], "agent_approval": [:] as [String: Any]])
        try await Task.sleep(nanoseconds: 1_200_000_000)
        let ff = Process()
        ff.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        ff.arguments = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "avfoundation", "-framerate", "60", "-capture_cursor", "0",
                        "-pixel_format", "nv12", "-use_wallclock_as_timestamps", "1", "-i", "0:none",
                        "-vf", "crop=640:220:960:0", "-copyts", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                        "-t", "4", "\(dir)/film.mp4"]
        ff.standardInput = FileHandle.nullDevice
        FileManager.default.createFile(atPath: "\(dir)/ffmpeg.log", contents: nil)
        ff.standardError = FileHandle(forWritingAtPath: "\(dir)/ffmpeg.log") ?? FileHandle.nullDevice
        // XCTest may lack screen-recording rights for AVFoundation; the caller can record externally.
        let external = env["CUA_ISLAND_FILM_EXTERNAL"] != nil
        if !external { try ff.run() }
        try await Task.sleep(nanoseconds: 1_000_000_000)
        let tOpen = Date().timeIntervalSince1970
        model.toggleCard()
        try await Task.sleep(nanoseconds: 1_200_000_000)
        let tLeave = Date().timeIntervalSince1970
        try await Task.sleep(nanoseconds: UInt64(SamanthaMenuBarIslandPanel.leaveDelay * 1_000_000_000))
        let tClose = Date().timeIntervalSince1970
        model.pointerLeft()
        if external { try await Task.sleep(nanoseconds: 1_500_000_000) } else { ff.waitUntilExit() }
        let marks = ["open": tOpen, "leave": tLeave, "close_call": tClose]
        try JSONSerialization.data(withJSONObject: marks).write(to: URL(fileURLWithPath: "\(dir)/marks.json"))
        controller.stopStreaming()
    }
}
