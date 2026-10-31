import AppKit
import SwiftUI
import XCTest
@testable import CUAService

private struct InitiatorRowFixture: View {
    @ObservedObject var model: IslandModel
    let agent: IslandAgent
    let large: Bool
    var body: some View {
        IslandAgentListBody(model: model).row(agent, depth: 0, parentName: nil, extra: 0, largeOverride: large)
    }
}

/// Offscreen FIXTURE renders (no window, no focus, fake data). Env-gated:
/// CUA_ISLAND_RENDER_DIR=<dir> swift test --filter IslandFixtureRenderTests
@MainActor
final class IslandFixtureRenderTests: XCTestCase {
    private func fixtureArt() -> NSImage {
        let img = NSImage(size: NSSize(width: 300, height: 300))
        img.lockFocus()
        NSGradient(colors: [NSColor(red: 0.95, green: 0.55, blue: 0.2, alpha: 1), NSColor(red: 0.45, green: 0.15, blue: 0.55, alpha: 1)])?
            .draw(in: NSRect(x: 0, y: 0, width: 300, height: 300), angle: 45)
        NSColor.white.withAlphaComponent(0.85).setFill()
        NSBezierPath(ovalIn: NSRect(x: 95, y: 95, width: 110, height: 110)).fill()
        img.unlockFocus()
        return img
    }

    private func save(_ model: IslandModel, _ name: String, dir: String) async throws {
        try await Task.sleep(nanoseconds: 800_000_000)  // let transitions settle
        let rect = model.shapeRect.insetBy(dx: model.cardOpen ? -40 : -100, dy: -6).offsetBy(dx: 0, dy: 3)
        let view = SamanthaIslandBar(model: model).environment(\.colorScheme, .dark)
            .background(Color(white: 0.18))
        let r = ImageRenderer(content: view)
        r.scale = 2
        guard let full = r.cgImage else { return XCTFail("render \(name)") }
        let crop = CGRect(x: rect.minX * 2, y: max(0, rect.minY * 2 - 12), width: rect.width * 2, height: rect.height * 2 + 24)
        let raw = full.cropping(to: crop) ?? full
        // ImageRenderer may hand back an HDR (PQ, 16-bit) image; flatten to 8-bit sRGB for the PNG.
        let cs = CGColorSpace(name: CGColorSpace.sRGB)!
        guard let ctx = CGContext(data: nil, width: raw.width, height: raw.height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return XCTFail("ctx") }
        ctx.draw(raw, in: CGRect(x: 0, y: 0, width: raw.width, height: raw.height))
        guard let img = ctx.makeImage() else { return XCTFail("flatten") }
        let url = URL(fileURLWithPath: dir).appendingPathComponent(name + ".png")
        try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: url)
    }

    func testRenderInitiatorRows() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        for large in [false, true] {
            for started in [false, true] {
                let model = IslandModel()
                var agent = IslandAgent(id: "fixture", name: "Codex", app: "Codex", title: "A long task title that must fit within the narrow island", status: "done")
                agent.local_detail = "Sol · light · 4m"
                if started { agent.initiator = IslandInitiator.parse(originator: "acu_exec.grokbot") }
                model.idleActivity = "agents"
                model.apply(IslandSnapshot(kind: "idle", agents: [agent]))
                model.openAgentList()
                XCTAssertEqual(model.listRows.count, 1)
                let view = InitiatorRowFixture(model: model, agent: agent, large: large).frame(width: IslandBarLayout.cardSize.width - 32, height: 44)
                    .environment(\.dynamicTypeSize, large ? .accessibility5 : .large)
                    .environment(\.colorScheme, .dark).background(Color.black)
                let renderer = ImageRenderer(content: view)
                renderer.scale = 2
                guard let image = renderer.cgImage else { XCTFail("row render"); continue }
                let name = "initiator-\(large ? "large" : "default")-\(started ? "agent" : "self").png"
                try NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
            }
        }
    }

    func testRenderNowPlayingAndChoiceFixtures() async throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else {
            throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render")
        }
        let suite = "cua.island.render.\(UUID().uuidString)"
        let defaults = TestDefaults.make(suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        store.set("gradient", for: NowPlayingKeys.color)
        for src in [AgentSource.grokBot, .cursor, .codex] {
            try store.rotateToken(src)
            store.set(true, for: src.keyPrefix + "enabled")
            store.set(true, for: src.keyPrefix + "canRaiseApprovals")
            store.set("attention", for: src.keyPrefix + "cap")
        }
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.frontmostAppName = { "Finder" }
        let m = c.debugModel
        m.reduceMotion = true
        m.peekSuppressed = { _ in true }   // stills: no Quick Peek mid-capture
        let fake = FakeNowPlayingSource()
        c.nowPlaying.frontmost = { "com.apple.finder" }
        c.nowPlaying.use(fake)
        var t = NowPlayingTests.track("Vibrations", elapsed: 83, duration: 412)
        t.artwork = fixtureArt(); t.artworkHash = 1
        m.mediaDevice = ("airpodspro", "AirPods Pro")
        func publish(_ j: [String: Any]) {
            var r = IslandHub.Request(method: "POST", path: "/api/island/publish", body: try! JSONSerialization.data(withJSONObject: j))
            r.peerVerified = true
            XCTAssertEqual(c.hub.handle(r, port: 0).status, 200, "\(j["id"] ?? "")")
        }
        func clearAll() {
            for p in c.hub.approvals { c.hub.dismissChoice(id: p.approval.id); c.hub.resolveApproval(id: p.approval.id, approved: false) }
        }
        let two: [String: Any] = ["source": "grok-bot", "id": "acu-choice-3", "kind": "choice", "prompt": "Island test: which do you prefer?",
                                  "options": [["label": "Looks great", "value": "great"], ["label": "Needs work", "value": "needs_work"]],
                                  "allow_text": true, "primary": "great"]

        // Compact 1: a single item.
        publish(two)
        try await Task.sleep(nanoseconds: 50_000_000)
        try await save(m, "r3-compact-single", dir: dir)
        // Compact 2: three pending from different agents (the approval jumped the queue).
        publish(["source": "cursor", "id": "c-ask", "kind": "choice", "prompt": "Run the migration now?",
                 "options": [["label": "Yes", "value": "y"], ["label": "Later", "value": "n"]]])
        publish(["source": "codex", "id": "x-appr", "kind": "approval", "title": "Delete 3 stale branches?"])
        m.forceCompact = true
        try await save(m, "r3-compact-3-pending", dir: dir)
        m.forceCompact = false
        clearAll()
        // Compact 3: Duo (music + choice).
        fake.emit(t)
        publish(two)
        try await Task.sleep(nanoseconds: 50_000_000)
        try await save(m, "r3-compact-duo", dir: dir)
        // Card: Now Playing (choice cleared first).
        clearAll()
        try await Task.sleep(nanoseconds: 50_000_000)
        m.toggleCard()
        try await save(m, "r3-card-nowplaying", dir: dir)
        m.toggleCard()
        fake.emit(nil)
        m.setNowPlaying(nil, visible: false)
        // Card: 2 options + reply.
        publish(two)
        try await Task.sleep(nanoseconds: 50_000_000)
        m.toggleCard()
        m.endTextEntry()
        try await save(m, "r3-card-2-options", dir: dir)
        clearAll()
        // Card: 4-option multi-select with Production selected, then the confirm flip.
        publish(["source": "grok-bot", "id": "q4", "kind": "choice", "prompt": "Which environments should I deploy to?",
                 "detail": "Website v1.4 · Pick any", "multi": true, "danger": "prod",
                 "options": [["label": "Dev", "value": "dev"], ["label": "Staging", "value": "staging"],
                             ["label": "QA", "value": "qa"], ["label": "Production", "value": "prod"]]])
        try await Task.sleep(nanoseconds: 50_000_000)
        m.toggleCard()
        m.endTextEntry()
        m.tapOption("staging"); m.tapOption("prod")
        try await save(m, "r3-card-multi-production", dir: dir)
        m.submitChoice()
        try await save(m, "r3-card-multi-confirm", dir: dir)
        clearAll()
        // Card: expiry notice.
        publish(["source": "grok-bot", "id": "acu-choice-9", "kind": "choice", "prompt": "Ship the island review build?",
                 "options": [["label": "Ship", "value": "ship"], ["label": "Hold", "value": "hold"]], "ttl_s": 30])
        c.hub.expire(now: Date().addingTimeInterval(31))
        try await Task.sleep(nanoseconds: 50_000_000)
        try await save(m, "r3-card-expired", dir: dir)
    }
}
