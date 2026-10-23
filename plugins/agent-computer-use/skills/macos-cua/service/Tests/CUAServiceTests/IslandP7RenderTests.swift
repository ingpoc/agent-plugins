import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// P7 offscreen FIXTURE renders, every new state on light + dark wallpaper (no window, no focus, fake data).
/// CUA_ISLAND_RENDER_DIR=<dir> swift test --filter IslandP7RenderTests
@MainActor
final class IslandP7RenderTests: XCTestCase {
    private var dir = ""

    private func save(_ model: IslandModel, _ name: String) async throws {
        try await Task.sleep(nanoseconds: 800_000_000)
        var rect = model.shapeRect
        if let s = model.secondaryRect { rect = rect.union(s) }
        // Everything drawn (source circles left, app circles right) plus an 8 pt margin: nothing clipped.
        if model.showAgentCircle {
            for c in IslandBarLayout.agentRects(shape: model.shapeRect, count: model.sourceGroups.count) { rect = rect.union(c) }
        }
        for c in IslandBarLayout.circleRects(shape: model.circleAnchor, count: model.circleCount) { rect = rect.union(c) }
        rect = rect.insetBy(dx: model.cardOpen ? -8 : -40, dy: -6).offsetBy(dx: 0, dy: 3)
        for (tag, bg, scheme) in [("light", Color(red: 0.86, green: 0.89, blue: 0.94), ColorScheme.light),
                                  ("dark", Color(white: 0.12), ColorScheme.dark)] {
            let view = SamanthaIslandBar(model: model).environment(\.colorScheme, scheme).background(bg)
            let r = ImageRenderer(content: view)
            r.scale = 2
            guard let full = r.cgImage else { return XCTFail("render \(name)") }
            let crop = CGRect(x: max(0, rect.minX * 2), y: max(0, rect.minY * 2 - 12), width: rect.width * 2, height: rect.height * 2 + 24)
            let raw = full.cropping(to: crop) ?? full
            let cs = CGColorSpace(name: CGColorSpace.sRGB)!
            guard let ctx = CGContext(data: nil, width: raw.width, height: raw.height, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return XCTFail("ctx") }
            ctx.draw(raw, in: CGRect(x: 0, y: 0, width: raw.width, height: raw.height))
            guard let img = ctx.makeImage() else { return XCTFail("flatten") }
            try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?
                .write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name)-\(tag).png"))
        }
    }

    func testRenderVoiceStates() async throws {
        guard let d = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR") }
        dir = d
        let samples: [(String, String, String)] = [
            ("listening", "Listening", "What's on my calendar?"),
            ("thinking", "What's on my calendar?", "Thinking"),
            ("speaking", "Your next meeting", "Design review in eight minutes."),
            ("acting", "Opening Calendar", "Calendar"),
            ("confirm", "Allow saved sign-in?", "Use the Example password for demo@example.test to sign in at example.test."),
            ("error", "Action not confirmed", "Check the app before trying again."),
            ("done", "84", "12 × 7"),
            ("reconnecting", "Reconnecting", "Connection lost. Restoring your conversation.")]
        for (kind, title, detail) in samples {
            let m = IslandModel()
            m.forceCompact = true
            m.apply(IslandSnapshot(kind: kind, title: title, detail: detail, app: kind == "confirm" ? "Comet" : kind == "done" ? "Calculator" : "Calendar",
                voice_side: kind == "speaking" ? "assistant" : "user", voice_level: 0.6, voice_levels: [0.2, 0.5, 0.7, 0.3, 0.6]))
            try await save(m, "voice-\(kind)-compact")
            m.forceCompact = false
            if !m.cardOpen { m.toggleCard() }
            try await save(m, "voice-\(kind)-card")
        }
    }

    func testRenderP7States() async throws {
        guard let d = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        dir = d
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        IslandModel.hudDwell = 60; IslandModel.peekDwell = 60
        defer { IslandModel.hudDwell = 1.2; IslandModel.peekDwell = 3 }
        let now = Date()

        // A2 HUD
        var m = IslandModel()
        m.showHUD(.init(kind: .volume, value: 0.6875)); try await save(m, "hud-volume")
        m.showHUD(.init(kind: .volume, value: 0.6875, muted: true)); try await save(m, "hud-muted")
        m.showHUD(.init(kind: .brightness, value: 0.4)); try await save(m, "hud-brightness")
        m.clearHUD()

        // A3 Peek
        m = IslandModel()
        m.showPeek(IslandPeekItem(id: "p1", source: "Grok Bot", symbol: "checkmark.seal.fill", title: "Build finished", at: now), now: now)
        try await save(m, "peek")
        m.showPeek(IslandPeekItem(id: "p2", source: "Grok Bot", symbol: "checkmark.seal.fill", title: "Tests passed", at: now), now: now)
        try await save(m, "peek-count")
        m.dismissPeek()
        m = IslandModel()
        m.showPeek(IslandPeekItem(id: "p3", source: "Grok Bot", symbol: "checkmark.seal.fill", title: "Deploy finished", at: now.addingTimeInterval(-125)), now: now)
        try await save(m, "peek-aged")
        m.dismissPeek()

        // A4 Calendar
        let ev = IslandCalendarEvent(id: "e1", title: "Design review", start: now.addingTimeInterval(8 * 60), end: now.addingTimeInterval(38 * 60),
                                     location: "Room 4", calendar: "Work", colorHex: "#0A84FF",
                                     meetingURL: URL(string: "https://meet.google.com/abc-defg-hij"))
        let ev2 = IslandCalendarEvent(id: "e2", title: "Lunch", start: now.addingTimeInterval(3 * 3600), end: now.addingTimeInterval(4 * 3600))
        m = IslandModel()
        m.setCalendar([ev, ev2], now: now); try await save(m, "calendar-compact")
        m.toggleCard(); try await save(m, "calendar-card")
        m = IslandModel()
        var urgent = ev; urgent.start = now.addingTimeInterval(120); urgent.meetingURL = nil
        m.setCalendar([urgent], now: now); try await save(m, "calendar-urgent")
        m = IslandModel(); m.calendarLead = 2 * 86_400   // worst case: 12:59 PM + long title, shown as the next event
        m.setCalendar(IslandLayoutGateTests.worstAgenda(now: now), now: now)
        m.toggleCard(); try await save(m, "calendar-card-worst")

        // A5 Duo
        m = IslandModel()
        m.setNowPlaying(NowPlayingTests.track(), visible: true)
        m.setCalendar([ev], now: now); try await save(m, "duo")
        m.tapSecondary(); try await save(m, "duo-swapped")

        // A9 Tray
        let tdir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/macos-cua/render-tray")
        try FileManager.default.createDirectory(at: tdir, withIntermediateDirectories: true)
        defer { try? FileManager.default.trashItem(at: tdir, resultingItemURL: nil) }
        let files = try Self.trayFixtures(in: tdir)
        m = IslandModel()
        m.setTrayTargeted(true); try await save(m, "tray-droptarget")
        m.setTrayTargeted(false)
        m.addToTray(files); try await save(m, "tray-compact")
        for item in m.tray { _ = IslandThumbCache.shared.image(for: item, side: 48) }   // start QuickLook (async)
        for _ in 0..<40 where m.tray.prefix(4).contains(where: { IslandThumbCache.shared.image(for: $0, side: 48) == nil }) {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        m.toggleCard()
        try await save(m, "tray-card")

        // 7 Agent list
        m = IslandModel()
        m.idleActivity = "agents"
        let rows = [
            IslandAgent(id: "codex:j1", name: "Codex", app: "Codex", title: "Migrating database", status: "running", elapsed_s: 245, controls: ["stop"]),
            IslandAgent(id: "cursor:b2", name: "Cursor", app: "Cursor", title: "Build failed: 3 errors", status: "error", controls: ["retry"]),
            IslandAgent(id: "grok-bot:r3", name: "Grok Bot", app: "Comet", title: "Booking table for 4", status: "running", elapsed_s: 30),
        ]
        m.apply(IslandSnapshot(kind: "idle", agents: rows))
        m.toggleCard(); try await save(m, "agents-card")
        m.tapRowControl(rows[0]); try await save(m, "agents-stop-armed")

        // Keyboard focus rings (⌃⌘I): row control (capsule) and decision buttons (capsule).
        m = IslandModel()
        m.idleActivity = "agents"
        m.apply(IslandSnapshot(kind: "idle", agents: rows))
        m.hotKeyPressed(); try await save(m, "focus-agent-row")
        m = IslandModel()
        m.apply(IslandSnapshot(kind: "idle", agent_approval: IslandAgentApproval(id: "grok:x", agent: "Grok Bot", app: "Comet", title: "Deploy to staging?", subtitle: "Website · release v1.4")))
        m.hotKeyPressed(); try await save(m, "focus-decision")
    }

    /// Real files so QuickLook draws real thumbnails (PNG, PDF, text, CSV); the last two are deliberately
    /// unreadable so the system-icon fallback is visible too.
    static func trayFixtures(in dir: URL) throws -> [URL] {
        let png = dir.appendingPathComponent("mock.png")
        let img = NSImage(size: NSSize(width: 256, height: 256), flipped: false) { r in
            NSGradient(colors: [NSColor.systemTeal, NSColor.systemIndigo])?.draw(in: r, angle: 45)
            NSColor.white.withAlphaComponent(0.85).setFill(); NSBezierPath(ovalIn: r.insetBy(dx: 80, dy: 80)).fill()
            return true
        }
        if let t = img.tiffRepresentation, let rep = NSBitmapImageRep(data: t) { try rep.representation(using: .png, properties: [:])?.write(to: png) }
        let pdf = dir.appendingPathComponent("Quarterly report.pdf")
        var box = CGRect(x: 0, y: 0, width: 612, height: 792)
        if let ctx = CGContext(pdf as CFURL, mediaBox: &box, nil) {
            ctx.beginPDFPage(nil)
            NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
            ("Quarterly report\n\nRevenue up 12 %. Margins steady." as NSString).draw(in: CGRect(x: 60, y: 500, width: 480, height: 240),
                withAttributes: [.font: NSFont.systemFont(ofSize: 36, weight: .bold)])
            NSGraphicsContext.restoreGraphicsState()
            ctx.endPDFPage(); ctx.closePDF()
        }
        let txt = dir.appendingPathComponent("notes.txt")
        try "Island polish notes\n- thumbnails 48 pt\n- radius 10\n- no border\n".write(to: txt, atomically: true, encoding: .utf8)
        let csv = dir.appendingPathComponent("data.csv")
        try "name,value\nalpha,1\nbeta,2\ngamma,3\n".write(to: csv, atomically: true, encoding: .utf8)
        let rest = try ["deck.key", "song.m4a"].map { n -> URL in
            let u = dir.appendingPathComponent(n); try "fixture".write(to: u, atomically: true, encoding: .utf8); return u
        }
        return [pdf, txt, png, csv] + rest
    }
}
