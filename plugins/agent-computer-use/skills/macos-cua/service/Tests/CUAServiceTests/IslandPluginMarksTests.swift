import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Plugin marks: ACU / Comet Control / Context Ledger show as circles on the RIGHT of the compact
/// pill, tied to their agent, and the hub drops them at TTL instead of showing "updates stopped".
@MainActor
final class IslandPluginMarksTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var store: IslandSettingsStore!

    override func setUp() {
        suite = "cua.island.plugins.test.\(UUID().uuidString)"
        defaults = TestDefaults.make(suite)
        store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        // The hub admits unsigned local posts in this suite; no token ceremony needed.
        defaults.set(true, forKey: IslandHub.allowUnsignedKey)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
    }

    // MARK: - Helpers

    private func post(_ hub: IslandHub, _ body: [String: Any], now: Date = Date()) -> IslandHub.Response {
        let request = IslandHub.Request(method: "POST", path: "/api/island/publish", query: [:],
                                        headers: ["host": "127.0.0.1:8777"],
                                        body: try! JSONSerialization.data(withJSONObject: body), localPort: 8777)
        return hub.handle(request, port: 8777, now: now)
    }

    private func clear(_ hub: IslandHub, id: String, now: Date = Date()) -> IslandHub.Response {
        post(hub, ["source": "agent", "id": id, "kind": "clear"], now: now)
    }

    private func pluginBody(plugin: String, id: String, agent: String, app: String, ttl: Double = 10) -> [String: Any] {
        ["source": "agent", "id": id, "kind": "running", "name": agent, "app": app,
         "title": "Using " + plugin, "plugin": plugin, "ttl_s": ttl]
    }

    private func use(_ plugin: String, _ app: String, _ agent: String) -> IslandPluginUse {
        IslandPluginUse(plugin: plugin, app: app, agent: agent, last_seen_at: 100)
    }

    // MARK: - Hub

    func testPluginRowBecomesPluginUseNotAgentRow() {
        let hub = IslandHub(store: store)
        let now = Date()
        let response = post(hub, pluginBody(plugin: "acu", id: "plugin:acu:k", agent: "Codex",
                                            app: "Agent Computer Use"), now: now)
        XCTAssertEqual(response.status, 200)

        let snap = hub.compose(now: now)
        XCTAssertTrue(snap.agents.isEmpty, "a plugin row is never an agent row")
        XCTAssertEqual(snap.plugins.map(\.plugin), ["acu"])
        XCTAssertEqual(snap.plugins.map(\.app), ["Agent Computer Use"])
        XCTAssertEqual(snap.plugins.map(\.agent), ["Codex"])
        XCTAssertEqual(snap.plugins.first?.last_seen_at ?? -1, now.timeIntervalSince1970, accuracy: 0.001)
    }

    func testUnknownPluginStaysANormalAgentRow() {
        let hub = IslandHub(store: store)
        let now = Date()
        _ = post(hub, pluginBody(plugin: "not-a-mark", id: "plugin:other:k", agent: "Codex", app: "Some App"), now: now)

        let snap = hub.compose(now: now)
        XCTAssertTrue(snap.plugins.isEmpty)
        XCTAssertEqual(snap.agents.count, 1)
        XCTAssertNil(snap.agents.first?.plugin)
    }

    func testPluginRowDropsAtTTLAndIsNeverStale() {
        let hub = IslandHub(store: store)
        let now = Date()
        _ = post(hub, pluginBody(plugin: "comet-control", id: "plugin:comet-control:k", agent: "Claude",
                                 app: "Comet", ttl: 10), now: now)

        let expired = now.addingTimeInterval(11)
        // Compose alone already hides it (stale plugin rows are not retained) …
        let beforeExpire = hub.compose(now: expired)
        XCTAssertTrue(beforeExpire.plugins.isEmpty)
        XCTAssertTrue(beforeExpire.agents.isEmpty)
        // … and the 1 s clock removes the entry entirely, never as "updates stopped".
        hub.expire(now: expired)
        let afterExpire = hub.compose(now: expired)
        XCTAssertTrue(afterExpire.plugins.isEmpty)
        XCTAssertFalse(afterExpire.agents.contains { $0.status == "stale" })
    }

    func testClearRemovesThePluginUse() {
        let hub = IslandHub(store: store)
        let now = Date()
        _ = post(hub, pluginBody(plugin: "context-ledger", id: "plugin:context-ledger:k", agent: "Codex",
                                 app: "Context Ledger"), now: now)
        XCTAssertEqual(hub.compose(now: now).plugins.count, 1)

        XCTAssertEqual(clear(hub, id: "plugin:context-ledger:k", now: now.addingTimeInterval(1)).status, 200)
        XCTAssertTrue(hub.compose(now: now.addingTimeInterval(1)).plugins.isEmpty)
    }

    // MARK: - Circles

    func testPluginItemsGroupInFixedOrderThenAlphabetically() {
        let uses = [
            use("context-ledger", "Context Ledger", "Zed"),
            use("comet-control", "Comet", "Claude"),
            use("comet-control", "Comet", "Codex"),
            use("acu", "Agent Computer Use", "Codex"),
            use("other", "Zed App", "Grok"),
            use("alpha", "Alpha", "A"),
        ]
        XCTAssertEqual(IslandCircles.pluginItems(uses), [
            .plugin(name: "Agent Computer Use", agents: ["Codex"]),
            .plugin(name: "comet-control", agents: ["Claude", "Codex"]),
            .plugin(name: "context-ledger", agents: ["Zed"]),
            .plugin(name: "Alpha", agents: ["A"]),
            .plugin(name: "Zed App", agents: ["Grok"]),
        ])
    }

    func testPluginItemsDedupeAgentsAndCapAtMax() {
        let uses = [
            use("acu", "Agent Computer Use", "Codex"),
            use("acu", "Agent Computer Use", "Codex"),
            use("acu", "Agent Computer Use", "Claude"),
        ]
        XCTAssertEqual(IslandCircles.pluginItems(uses),
                       [.plugin(name: "Agent Computer Use", agents: ["Claude", "Codex"])])
        XCTAssertEqual(IslandBarLayout.maxCircles, 3)
    }

    func testMergeKeepsAppsNearestThePillThenPluginsAndCapsOverflow() {
        // 3 shown app circles + "+4" hidden: a consistent app list from IslandCircles.items.
        let apps: [IslandCircleItem] = [
            .app(name: "A", dimmed: false, driving: false),
            .app(name: "B", dimmed: true, driving: false),
            .app(name: "C", dimmed: false, driving: false),
            .overflow(4),
        ]
        let plugin = IslandCircleItem.plugin(name: "comet-control", agents: ["Codex"])
        XCTAssertEqual(IslandCircles.merge(apps: apps, plugins: [plugin]), [
            .app(name: "A", dimmed: false, driving: false),
            .app(name: "B", dimmed: true, driving: false),
            .app(name: "C", dimmed: false, driving: false),
            .overflow(5),
        ])
        // A plain app list comes out unchanged: its own overflow count survives.
        XCTAssertEqual(IslandCircles.merge(apps: apps, plugins: []), apps)
        // Plugins fill the remaining slots nearest-to-far, then "+N".
        XCTAssertEqual(IslandCircles.merge(apps: [apps[0]], plugins: [
            .plugin(name: "Agent Computer Use", agents: ["Codex"]), plugin,
            .plugin(name: "context-ledger", agents: ["Claude"]),
        ]), [
            .app(name: "A", dimmed: false, driving: false),
            .plugin(name: "Agent Computer Use", agents: ["Codex"]),
            plugin,
            .overflow(1),
        ])
        XCTAssertEqual(IslandCircles.merge(apps: [], plugins: []), [])
    }

    func testPluginLabelStringsTieTheCircleToItsAgent() {
        // Title case everywhere; "Using …" only in VoiceOver and the expanded row (UI & UX, 2026-11-03).
        XCTAssertEqual(IslandPluginLabel.display("Agent Computer Use"), "Agent Computer Use")
        XCTAssertEqual(IslandPluginLabel.display("comet-control"), "Comet Control")
        XCTAssertEqual(IslandPluginLabel.display("context-ledger"), "Context Ledger")
        XCTAssertEqual(IslandPluginLabel.display("Unknown"), "Unknown")
        XCTAssertEqual(IslandPluginLabel.tooltip(name: "comet-control", agents: ["Claude", "Codex"]),
                       "Comet Control · Claude, Codex")
        XCTAssertEqual(IslandPluginLabel.voiceOver(name: "comet-control", agents: ["Claude", "Codex"]),
                       "Using Comet Control: Claude and Codex")
        XCTAssertEqual(IslandPluginLabel.voiceOver(name: "Agent Computer Use", agents: ["A", "B", "C"]),
                       "Using Agent Computer Use: A, B and C")
        XCTAssertEqual(IslandPluginLabel.tooltip(name: "context-ledger", agents: ["Codex"]),
                       "Context Ledger · Codex")
        XCTAssertEqual(IslandPluginLabel.voiceOver(name: "context-ledger", agents: []), "Using Context Ledger")
        XCTAssertEqual(IslandPluginLabel.rowTitle(name: "comet-control"), "Using Comet Control")
        for name in ["Agent Computer Use", "comet-control", "context-ledger"] {
            XCTAssertFalse(IslandPluginLabel.tooltip(name: name, agents: ["Codex"]).hasPrefix("Using"))
        }
    }

    func testPluginInUseLightsTheWorkingDotButRedAndAmberWin() {
        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle"))
        XCTAssertNil(model.compactDot)
        model.apply(IslandSnapshot(kind: "idle", plugins: [use("comet-control", "Comet", "Codex")]))
        XCTAssertEqual(model.compactDot, .working)
        XCTAssertTrue(model.compactPillLabel.contains("Using Comet Control: Codex"))
        let ask = IslandAgent(id: "agent:a", name: "Claude", app: "Claude", title: "Ask", status: "needs_you",
                              needs_you: true, last_seen_at: 100)
        model.apply(IslandSnapshot(kind: "idle", agents: [ask], plugins: [use("acu", "Agent Computer Use", "Codex")]))
        XCTAssertEqual(model.compactDot, .needsYou)
        let failed = IslandAgent(id: "agent:f", name: "Codex", app: "Codex", title: "Build", status: "error",
                                 last_seen_at: 100)
        model.apply(IslandSnapshot(kind: "idle", agents: [failed, ask], plugins: [use("acu", "Agent Computer Use", "Codex")]))
        XCTAssertEqual(model.compactDot, .failed)
    }

    func testExpandedListShowsOneUsingRowPerPluginUse() {
        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle", plugins: [
            use("comet-control", "comet-control", "Smallcase Manager"), use("acu", "Agent Computer Use", "Grok Bot"),
        ]))
        let rows = model.listRows.filter { $0.id.hasPrefix("plugin:") }
        XCTAssertEqual(rows.map(\.title), ["Using Agent Computer Use", "Using Comet Control"])
        XCTAssertEqual(rows.map(\.name), ["Grok Bot", "Smallcase Manager"])
        XCTAssertEqual(rows.map(\.app), ["Agent Computer Use", "comet-control"])
        XCTAssertTrue(model.sourceGroups.isEmpty, "plugin rows never become left harness circles")
    }

    func testPluginRowMayHoldForTwoSecondsAndDropsAfter() {
        let hub = IslandHub(store: store)
        let now = Date()
        _ = post(hub, pluginBody(plugin: "acu", id: "plugin:acu:k", agent: "Codex", app: "Agent Computer Use", ttl: 2),
                 now: now)
        XCTAssertEqual(hub.compose(now: now.addingTimeInterval(1.9)).plugins.count, 1, "held 2 s, not raised to 5")
        hub.expire(now: now.addingTimeInterval(2.0))
        XCTAssertTrue(hub.compose(now: now.addingTimeInterval(2.0)).plugins.isEmpty)
        // A normal agent row keeps its 5 s floor.
        _ = post(hub, ["source": "agent", "id": "r", "kind": "running", "name": "Codex", "app": "Codex",
                       "title": "t", "ttl_s": 2], now: now)
        XCTAssertEqual(hub.compose(now: now.addingTimeInterval(4)).agents.filter { $0.status == "running" }.count, 1)
    }

    func testTargetMarkTileReadsAtOneX() {
        XCTAssertGreaterThanOrEqual(IslandPluginTargetMark.tileSize, 12)
        XCTAssertGreaterThan(IslandPluginTargetMark.ringDiameter - 2 * IslandPluginTargetMark.ringLine,
                             IslandPluginTargetMark.tileSize)
        XCTAssertLessThanOrEqual(IslandPluginTargetMark.ringDiameter, IslandBarLayout.compactHeight)
        // The tile's rounded corners clear the ring's inner edge.
        let t = IslandPluginTargetMark.tileSize, r = IslandPluginTargetMark.tileRadius(t)
        let corner = (t / 2 - r) * 2.squareRoot() + r
        XCTAssertLessThan(corner, IslandPluginTargetMark.ringDiameter / 2 - IslandPluginTargetMark.ringLine)
    }

    // MARK: - Model

    func testIdleSnapshotWithPluginsShowsPluginCirclesAndKeepsThePillVisible() {
        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle", plugins: [
            use("acu", "Agent Computer Use", "Codex"),
            use("comet-control", "Comet", "Claude"),
        ]))

        XCTAssertEqual(model.presentation, .idle)
        XCTAssertEqual(model.pluginUses.map(\.plugin), ["acu", "comet-control"])
        XCTAssertEqual(model.circles, [
            .plugin(name: "Agent Computer Use", agents: ["Codex"]),
            .plugin(name: "comet-control", agents: ["Claude"]),
        ])
        XCTAssertFalse(model.idleHidden, "a plugin in use keeps the idle pill visible")
        XCTAssertEqual(model.circleCount, 2)
    }

    func testNoPluginsAtIdleAgentsStillHidesThePill() {
        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle"))
        XCTAssertTrue(model.circles.isEmpty)
        XCTAssertTrue(model.idleHidden)
    }

    func testIdleActivityNoneHidesEvenWithPlugins() {
        let model = IslandModel()
        model.idleActivity = "none"
        model.apply(IslandSnapshot(kind: "idle", plugins: [use("acu", "Agent Computer Use", "Codex")]))
        XCTAssertEqual(model.circles, [.plugin(name: "Agent Computer Use", agents: ["Codex"])])
        XCTAssertTrue(model.idleHidden)
    }

    // MARK: - Render (env-gated)

    private func save(_ model: IslandModel, _ name: String, dir: String) async throws {
        try await Task.sleep(nanoseconds: 800_000_000)  // let transitions settle
        let rect = model.shapeRect.insetBy(dx: model.cardOpen ? -40 : -140, dy: -6).offsetBy(dx: 0, dy: 3)
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

    /// CUA_ISLAND_RENDER_DIR=<dir> swift test --filter IslandPluginMarksTests
    func testRenderPluginMarks() async throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else {
            throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render")
        }
        // Plugin logos come from this repo's packages (plugins/<id>/assets/logo.svg), not ~/.agents.
        let plugins = URL(fileURLWithPath: #filePath).deletingLastPathComponent()  // CUAServiceTests
            .appendingPathComponent("../../../../../../").standardized
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)
        func agent(_ name: String, _ app: String) -> IslandAgent {
            var a = IslandAgent(id: "agent:\(name)", name: name, app: app, title: "Task", status: "running", last_seen_at: 100)
            a.elapsed_s = 42
            return a
        }
        let codex = agent("Codex", "Codex")
        func render(_ name: String, agents: [IslandAgent], uses: [IslandPluginUse], apps: [String] = []) async throws {
            let m = IslandModel()
            m.idleActivity = "duo"
            m.apply(IslandSnapshot(kind: "idle", active_apps: apps, agents: agents, plugins: uses))
            try await save(m, name, dir: dir)
        }
        var acu = use("acu", "Agent Computer Use", "Codex"); acu.last_seen_at = 200
        let comet = use("comet-control", "comet-control", "Codex")
        try await render("plugin-acu", agents: [codex], uses: [acu])
        try await render("plugin-comet-control", agents: [codex], uses: [comet])
        try await render("plugin-context-ledger", agents: [codex], uses: [use("context-ledger", "context-ledger", "Codex")])
        // Both controlling plugins at once: the newer use (ACU) owns the inside mark.
        try await render("plugin-both-active", agents: [codex], uses: [acu, comet])
        // 4+ agents across all three plugins + one Samantha app: left and right both collapse to "+N".
        let four = [agent("Codex", "Codex"), agent("Claude", "Claude"), agent("Cursor", "Cursor"), agent("Grok Bot", "Grok Bot")]
        try await render("plugin-overflow-4-agents", agents: four, uses: [
            acu, use("acu", "Agent Computer Use", "Claude"), comet, use("comet-control", "comet-control", "Cursor"),
            use("context-ledger", "context-ledger", "Grok Bot"),
        ], apps: ["Finder"])
        try await render("plugin-marks-cleared", agents: [codex], uses: [])
        // Samantha, same language: her circle left, the target ring centre, the plugin mark right.
        func samantha(_ name: String, _ snap: IslandSnapshot) async throws {
            let m = IslandModel()
            m.idleActivity = "duo"
            m.apply(snap)
            try await save(m, name, dir: dir)
        }
        try await samantha("samantha-acu-acting", IslandSnapshot(kind: "acting", title: "Opening Finder", app: "Finder",
            active_apps: ["Finder"], plugins: [use("acu", "Agent Computer Use", "Samantha")]))
        try await samantha("samantha-comet-acting", IslandSnapshot(kind: "acting", title: "Reading page",
            plugins: [use("comet-control", "comet-control", "Samantha")]))
        try await samantha("samantha-cleared", IslandSnapshot(kind: "idle"))
        // Operated Mac apps INSIDE the pill, left of the dot; the Apple ring stays inside on the left.
        func op(_ app: String, _ agent: String, _ t: Double) -> IslandPluginUse {
            var u = use("acu", "Agent Computer Use", agent); u.target_app = app; u.last_seen_at = t; return u
        }
        try await render("operated-1-calculator", agents: [codex], uses: [op("Calculator", "Codex", 300)])
        try await render("operated-3-apps", agents: [codex], uses: [
            op("Calculator", "Codex", 300), op("com.apple.finder", "Grok Bot", 310), op("TextEdit", "Codex", 320)])
        try await render("operated-4-apps-plus-n", agents: [codex], uses: [
            op("Calculator", "Codex", 300), op("Finder", "Grok Bot", 310), op("TextEdit", "Codex", 320), op("Notes", "Cursor", 330)])
        try await render("operated-5-apps-plus-2", agents: [codex], uses: [
            op("Calculator", "Codex", 300), op("Finder", "Grok Bot", 310), op("TextEdit", "Codex", 320), op("Notes", "Cursor", 330),
            op("Preview", "Codex", 340)])
        try await samantha("operated-samantha-acting", IslandSnapshot(kind: "acting", title: "Opening Calculator", app: "Calculator",
            active_apps: ["Calculator"], plugins: [op("Calculator", "Samantha", 300)]))
        try await render("operated-cleared", agents: [codex], uses: [])
    }

    /// Replays island posts recorded from real plugin use (one JSON line per post: {"t", "body"}) through
    /// the real hub and renders the island after each post (2x). Env-gated:
    /// ISLAND_RECORDED_POSTS=<jsonl> CUA_ISLAND_RENDER_DIR=<dir> swift test --filter testRenderRecordedPosts
    func testRenderRecordedPosts() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let dir = env["CUA_ISLAND_RENDER_DIR"], let path = env["ISLAND_RECORDED_POSTS"] else {
            throw XCTSkip("set ISLAND_RECORDED_POSTS and CUA_ISLAND_RENDER_DIR to replay")
        }
        let plugins = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../../").standardized
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)
        let hub = IslandHub(store: store)
        let lines = try String(contentsOfFile: path, encoding: .utf8).split(separator: "\n")
        for (i, line) in lines.enumerated() {
            guard let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  let body = obj["body"] as? [String: Any], let t = obj["t"] as? Double else { continue }
            let now = Date(timeIntervalSince1970: t)
            XCTAssertEqual(post(hub, body, now: now).status, 200, "post \(i)")
            let m = IslandModel()
            m.idleActivity = "duo"
            m.apply(hub.compose(now: now))
            let kind = body["kind"] as? String ?? "?"
            let plugin = (body["id"] as? String ?? "").split(separator: ":").dropFirst().first.map(String.init) ?? "row"
            let ttl = (body["ttl_s"] as? Double) ?? 0
            let label = kind == "running" && ttl <= 2 ? "finished" : kind
            try await save(m, String(format: "recorded-%02d-%@-%@", i + 1, plugin, label), dir: dir)
            // After a "finished" post (ttl 2 s), and when no post lands inside that window, render the drop.
            let nextT = lines.dropFirst(i + 1).lazy.compactMap { l -> Double? in
                (try? JSONSerialization.jsonObject(with: Data(l.utf8)) as? [String: Any])?["t"] as? Double
            }.first
            if label == "finished", nextT.map({ $0 >= t + 2.1 }) ?? true {
                let later = Date(timeIntervalSince1970: t + 2.1)
                hub.expire(now: later)
                let gone = IslandModel()
                gone.idleActivity = "duo"
                gone.apply(hub.compose(now: later))
                try await save(gone, String(format: "recorded-%02d-%@-cleared-2s", i + 1, plugin), dir: dir)
            }
        }
    }

    func testRightCirclesCapAtThreeThenCollapseToPlusN() {
        // 4 right-side circles: 3 known plugin marks + one more app in use -> 3 shown + "+1".
        let four = IslandCircles.pluginItems([
            use("acu", "Agent Computer Use", "Codex"), use("comet-control", "comet-control", "Claude"),
            use("context-ledger", "context-ledger", "Cursor"), use("other", "Other Plugin", "Grok Bot"),
        ])
        XCTAssertEqual(four.count, 4)
        XCTAssertEqual(IslandCircles.merge(apps: [], plugins: four), Array(four.prefix(3)) + [.overflow(1)])
        // Samantha's app circle sits nearest the pill and pushes two plugin marks into "+2".
        let finder = IslandCircleItem.app(name: "Finder", dimmed: false, driving: false)
        XCTAssertEqual(IslandCircles.merge(apps: [finder], plugins: four), [finder] + Array(four.prefix(2)) + [.overflow(2)])
        // Exactly 3 never shows "+N".
        XCTAssertEqual(IslandCircles.merge(apps: [], plugins: Array(four.prefix(3))), Array(four.prefix(3)))
        // Through the model: the right side is capped the same way.
        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle", plugins: [
            use("acu", "Agent Computer Use", "Codex"), use("comet-control", "comet-control", "Claude"),
            use("context-ledger", "context-ledger", "Cursor"), use("other", "Other Plugin", "Grok Bot"),
            use("more", "More Plugin", "Codex"),
        ]))
        XCTAssertEqual(model.circles.count, 4)
        XCTAssertEqual(model.circles.last, .overflow(2))
        XCTAssertEqual(model.circleCount, 5)
    }

    func testBothTargetsRingShowsNewestAndVoiceOverNamesBoth() {
        var acu = use("acu", "Agent Computer Use", "Codex"); acu.last_seen_at = 100
        var comet = use("comet-control", "comet-control", "Claude"); comet.last_seen_at = 200
        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle", plugins: [acu, comet]))
        XCTAssertEqual(model.pluginTarget, .app("Comet"))
        XCTAssertEqual(model.pluginTargets, [.app("Comet"), .symbol("apple.logo")])
        XCTAssertEqual(IslandPluginTarget.accessibilityLabel(model.pluginTargets), "Controlling Comet and Mac")
        // ACU becomes the most recent: the ring switches to the Mac, VoiceOver still names both.
        acu.last_seen_at = 300
        model.apply(IslandSnapshot(kind: "idle", plugins: [acu, comet]))
        XCTAssertEqual(model.pluginTarget, .symbol("apple.logo"))
        XCTAssertEqual(IslandPluginTarget.accessibilityLabel(model.pluginTargets), "Controlling Mac and Comet")
        // One target, or a Context Ledger use alongside, names only the controlled target.
        var ledger = use("context-ledger", "context-ledger", "Codex"); ledger.last_seen_at = 400
        XCTAssertEqual(IslandPluginTarget.accessibilityLabel(IslandPluginTarget.all([acu, ledger])), "Controlling Mac")
        XCTAssertEqual(IslandPluginTarget.accessibilityLabel([]), "")
    }

    func testSamanthaActingUsesTheSameThreeZones() {
        // Samantha drives Finder through ACU: left = her circle, centre = Mac ring, right = Finder + ACU mark.
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(IslandSnapshot(kind: "acting", title: "Opening Finder", app: "Finder", active_apps: ["Finder"],
                                   plugins: [use("acu", "Agent Computer Use", "Samantha")]))
        XCTAssertEqual(model.presentation, .acting)
        XCTAssertTrue(model.showsTargetMark)
        XCTAssertEqual(model.pluginTarget, .symbol("apple.logo"))
        XCTAssertEqual(model.sourceGroups.first?.id, "Samantha")
        XCTAssertTrue(model.showAgentCircle)
        XCTAssertEqual(model.circles.last, .plugin(name: "Agent Computer Use", agents: ["Samantha"]))
        XCTAssertTrue(model.circles.contains { if case .app("Finder", _, _) = $0 { return true } else { return false } })
        XCTAssertEqual(model.compactPillLabel.contains("Using Agent Computer Use: Samantha"), true)

        // Comet through Comet Control: Comet is the centre target, never a separate right-side app circle.
        model.apply(IslandSnapshot(kind: "acting", title: "Reading page",
                                   plugins: [use("comet-control", "comet-control", "Samantha")]))
        XCTAssertEqual(model.pluginTarget, .app("Comet"))
        XCTAssertEqual(model.circles, [.plugin(name: "comet-control", agents: ["Samantha"])])

        // 2 s after her last call the hub drops the row: her circle, the ring and the mark all go.
        model.apply(IslandSnapshot(kind: "done", title: "Done"))
        XCTAssertFalse(model.showsTargetMark)
        XCTAssertFalse(model.sourceGroups.contains { $0.id == "Samantha" })
        XCTAssertTrue(model.circles.allSatisfy { !$0.isPlugin })
    }

    func testSamanthaCircleKeepsUrgentFirstAndTheCap() {
        let ask = IslandSourceGroup(id: "Claude", app: "Claude", needsYou: 1)
        let a = IslandSourceGroup(id: "Codex", app: "Codex", running: true)
        let b = IslandSourceGroup(id: "Cursor", app: "Cursor", running: true)
        let out = IslandSourceGroups.withSamantha([ask, a, b], active: true)
        XCTAssertEqual(out.map(\.id), ["Claude", "Samantha", "Codex", "overflow"])
        XCTAssertEqual(out.last?.overflow, 1)
        XCTAssertEqual(IslandSourceGroups.withSamantha([ask, a], active: false).map(\.id), ["Claude", "Codex"])
        let already = IslandSourceGroup(id: "Samantha", app: "Samantha", running: true)
        XCTAssertEqual(IslandSourceGroups.withSamantha([already], active: true).map(\.id), ["Samantha"])
    }

    // MARK: - Operated Mac apps inside the pill (Guru, 2026-11-03 13:27 IST)

    private func op(_ app: String, _ agent: String, _ t: Double, plugin: String = "acu") -> IslandPluginUse {
        var u = use(plugin, plugin == "acu" ? "Agent Computer Use" : plugin, agent)
        u.target_app = app; u.last_seen_at = t
        return u
    }

    func testHubCarriesTheOperatedAppOnAcuRowsOnly() {
        let hub = IslandHub(store: store)
        let now = Date()
        var body = pluginBody(plugin: "acu", id: "plugin:acu:k:app:Calculator", agent: "Grok Bot", app: "Agent Computer Use", ttl: 2)
        body["target_app"] = "  Calculator "
        XCTAssertEqual(post(hub, body, now: now).status, 200)
        var finder = pluginBody(plugin: "acu", id: "plugin:acu:k:app:Finder", agent: "Grok Bot", app: "Agent Computer Use", ttl: 2)
        finder["target_app"] = "Finder"
        XCTAssertEqual(post(hub, finder, now: now.addingTimeInterval(0.5)).status, 200)
        let snap = hub.compose(now: now.addingTimeInterval(1))
        XCTAssertEqual(Set(snap.plugins.compactMap(\.target_app)), ["Calculator", "Finder"])
        XCTAssertEqual(IslandOperatedApps.names(snap.plugins), ["Finder", "Calculator"], "newest first")
        // Each app row clears on its own TTL: 2 s after the Calculator call, only Finder is left.
        XCTAssertEqual(IslandOperatedApps.names(hub.compose(now: now.addingTimeInterval(2.2)).plugins), ["Finder"])
        XCTAssertTrue(hub.compose(now: now.addingTimeInterval(2.7)).plugins.isEmpty)
    }

    func testOperatedAppsNewestFirstUniqueAcuOnlyCappedAtThreePlusN() {
        let uses = [op("Calculator", "Codex", 100), op("Finder", "Grok Bot", 300), op("Calculator", "Cursor", 200),
                    op("Comet", "Codex", 400, plugin: "comet-control"), use("acu", "Agent Computer Use", "Codex")]
        XCTAssertEqual(IslandOperatedApps.names(uses), ["Finder", "Calculator"])
        let five = ["A", "B", "C", "D", "E"].enumerated().map { op($0.element, "Codex", Double(10 - $0.offset)) }
        XCTAssertEqual(IslandOperatedApps.items(five), [.app("A"), .app("B"), .app("C"), .overflow(2)])
        XCTAssertEqual(IslandOperatedApps.accessibilityLabel(["Calculator", "Finder", "TextEdit"]),
                       "Operating Calculator, Finder and TextEdit")
        XCTAssertEqual(IslandOperatedApps.displayName("com.apple.calculator"), "Calculator")
        XCTAssertEqual(IslandOperatedApps.displayName("TextEdit"), "TextEdit")
    }

    func testOperatedAppIconsSitInsideClearOfTheRingAndTheDot() {
        for count in 1...4 {
            let width = max(IslandBarLayout.idleWidth, IslandBarLayout.operatedAppsPillWidth(count: count))
            let shape = IslandBarLayout.shapeRect(compactWidth: width, card: false)
            let dot = IslandBarLayout.compactDotRect(shape: shape)
            let ringMaxX = shape.minX + IslandBarLayout.slotInset + 8 + IslandPluginTargetMark.ringDiameter / 2
            let rects = IslandBarLayout.operatedAppRects(shape: shape, count: count)
            XCTAssertEqual(rects.count, count)
            for r in rects {
                XCTAssertTrue(shape.contains(r), "icon inside the pill (\(count))")
                XCTAssertEqual(r.midY, dot.midY, accuracy: 0.001)
                XCTAssertEqual(r.width, 16)
            }
            XCTAssertEqual(dot.maxX, shape.maxX - 9, "the dot keeps its 9 pt inset")
        }
        XCTAssertEqual(IslandBarLayout.operatedAppsPillWidth(count: 1), 71)  // 6 ring 20 · 8 · 16 · 6 · 6 · 9
        XCTAssertEqual(IslandBarLayout.operatedAppsPillWidth(count: 3), 111)
        XCTAssertEqual(IslandBarLayout.operatedAppsPillWidth(count: 4), 131) // 3 icons + the 16 pt "+N" slot
    }

    /// UI & UX fix 2: ring · 8 · icon · 4 · icon … · 6 · dot · 9, identical for 1…4 slots.
    func testOperatedGapsAreFixedInEveryCount() {
        for count in 1...4 {
            let shape = IslandBarLayout.shapeRect(compactWidth: IslandBarLayout.operatedAppsPillWidth(count: count), card: false)
            let ringMaxX = shape.minX + IslandBarLayout.slotInset + 8 + IslandPluginTargetMark.ringDiameter / 2
            let dot = IslandBarLayout.compactDotRect(shape: shape)
            let rects = IslandBarLayout.operatedAppRects(shape: shape, count: count)
            XCTAssertEqual(rects[0].minX - ringMaxX, 8, accuracy: 0.001, "ring to first icon (\(count))")
            for (a, b) in zip(rects, rects.dropFirst()) { XCTAssertEqual(b.minX - a.maxX, 4, accuracy: 0.001) }
            XCTAssertEqual(dot.minX - rects[count - 1].maxX, 6, accuracy: 0.001, "last icon or +N to dot (\(count))")
            XCTAssertEqual(shape.maxX - dot.maxX, 9, accuracy: 0.001)
        }
    }

    /// UI & UX fix 1: the compact pill hugs its content in Samantha's states too (no 136 pt acting,
    /// no conversation width, no expanded width carried over).
    func testSamanthaCompactPillHugsOperatedApps() async throws {
        let model = IslandModel()
        model.idleActivity = "duo"
        // A presentation family change pinches to 74 then grows (160 ms); wait it out.
        func settle() async throws { try await Task.sleep(nanoseconds: 300_000_000) }
        model.apply(IslandSnapshot(kind: "acting", title: "Reading Finder", app: "Finder", active_apps: ["Finder"],
                                   plugins: [op("Finder", "Samantha", 300)]))
        try await settle()
        XCTAssertEqual(model.presentation, .acting)
        XCTAssertEqual(model.drawnWidth, IslandBarLayout.operatedAppsPillWidth(count: 1))
        XCTAssertEqual(model.shapeRect.width, 71, "no 136 pt acting width")
        model.apply(IslandSnapshot(kind: "idle", plugins: [op("Finder", "Samantha", 300), op("TextEdit", "Samantha", 310),
                                                          op("Calculator", "Samantha", 320)]))
        try await settle()
        XCTAssertEqual(model.shapeRect.width, 111)
        model.apply(IslandSnapshot(kind: "idle"))
        try await settle()
        XCTAssertEqual(model.drawnWidth, IslandBarLayout.idleWidth)
    }

    func testIconChangesUseTheSpringAndNearWhiteIconsUseTheTile() {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(IslandSnapshot(kind: "idle", plugins: [op("Finder", "Codex", 300)]))
        model.apply(IslandSnapshot(kind: "idle", plugins: [op("Finder", "Codex", 300), op("Calculator", "Codex", 310)]))
        XCTAssertTrue(model.widthFromOperatedApps, "adding an icon springs the width")
        XCTAssertEqual(IslandMotion.operatedWidth(reduce: true), IslandMotion.crossfade, "Reduce Motion: no spring")
        XCTAssertTrue(model.accessibilityText.contains("Operating Calculator and Finder"))
        // Generic near-white detection: an all-white image is blank, a coloured one is not.
        let white = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { r in NSColor.white.setFill(); r.fill(); return true }
        let blue = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { r in NSColor.systemBlue.setFill(); r.fill(); return true }
        XCTAssertTrue(AppIconResolver.isBlank(white))
        XCTAssertFalse(AppIconResolver.isBlank(blue))
        if case .tile = AppIconResolver.operatedStyle("No Such App 0279") {} else { XCTFail("a missing icon gets the tile") }
    }

    func testModelShowsOperatedAppsInsideAndGrowsThePill() {
        let model = IslandModel()
        model.idleActivity = "duo"
        let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task", status: "running", last_seen_at: 100)
        model.apply(IslandSnapshot(kind: "idle", agents: [codex], plugins: [
            op("Calculator", "Grok Bot", 300), op("Finder", "Grok Bot", 310), op("TextEdit", "Grok Bot", 320)]))
        XCTAssertTrue(model.showsOperatedApps)
        XCTAssertTrue(model.showsTargetMark, "the Apple ring stays inside on the left")
        XCTAssertEqual(model.operatedApps, [.app("TextEdit"), .app("Finder"), .app("Calculator")])
        XCTAssertEqual(model.drawnWidth, IslandBarLayout.operatedAppsPillWidth(count: 3))
        XCTAssertEqual(model.circles, [.plugin(name: "Agent Computer Use", agents: ["Grok Bot"])], "plugin mark stays outside")
        XCTAssertEqual(model.listRows.filter { $0.id.hasPrefix("plugin:") }.count, 1, "one expanded row per plugin and agent")
        XCTAssertTrue(model.compactPillLabel.contains("Operating TextEdit, Finder and Calculator"))
        // Dropped 2 s after the last call: icons go and the pill returns to its idle width.
        model.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        XCTAssertFalse(model.showsOperatedApps)
        XCTAssertEqual(model.drawnWidth, IslandBarLayout.idleWidth)
    }

    func testSamanthaOperatedAppMovesInsideNotOutside() {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(IslandSnapshot(kind: "acting", title: "Opening Calculator", app: "Calculator", active_apps: ["Calculator"],
                                   plugins: [op("Calculator", "Samantha", 300)]))
        XCTAssertEqual(model.operatedApps, [.app("Calculator")])
        XCTAssertFalse(model.circles.contains { if case .app("Calculator", _, _) = $0 { return true } else { return false } },
                       "an app shown inside is never drawn again outside")
    }

    func testTargetMarkPerPluginAndMostRecentWins() {
        XCTAssertEqual(IslandPluginTarget.of(plugin: "acu"), .symbol("apple.logo"))
        XCTAssertEqual(IslandPluginTarget.of(plugin: "comet-control"), .app("Comet"))
        XCTAssertNil(IslandPluginTarget.of(plugin: "context-ledger"))
        var acu = use("acu", "Agent Computer Use", "Codex"); acu.last_seen_at = 100
        var comet = use("comet-control", "Comet", "Claude"); comet.last_seen_at = 200
        var ledger = use("context-ledger", "Context Ledger", "Codex"); ledger.last_seen_at = 300
        XCTAssertEqual(IslandPluginTarget.current([acu, comet, ledger]), .app("Comet"))
        XCTAssertNil(IslandPluginTarget.current([ledger]))
        XCTAssertNil(IslandPluginTarget.current([]))
    }
}
