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
        XCTAssertEqual(IslandPluginLabel.display("Agent Computer Use"), "ACU")
        XCTAssertEqual(IslandPluginLabel.display("comet-control"), "Comet Control")
        XCTAssertEqual(IslandPluginLabel.display("context-ledger"), "Context Ledger")
        XCTAssertEqual(IslandPluginLabel.display("Unknown"), "Unknown")
        XCTAssertEqual(IslandPluginLabel.tooltip(name: "comet-control", agents: ["Claude", "Codex"]),
                       "Comet Control · Claude, Codex")
        XCTAssertEqual(IslandPluginLabel.voiceOver(name: "comet-control", agents: ["Claude", "Codex"]),
                       "Comet Control in use by Claude and Codex")
        XCTAssertEqual(IslandPluginLabel.voiceOver(name: "Agent Computer Use", agents: ["A", "B", "C"]),
                       "ACU in use by A, B and C")
        XCTAssertEqual(IslandPluginLabel.tooltip(name: "context-ledger", agents: ["Codex"]),
                       "Context Ledger · Codex")
        XCTAssertEqual(IslandPluginLabel.voiceOver(name: "context-ledger", agents: ["Codex"]),
                       "Context Ledger in use by Codex")
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

    /// CUA_ISLAND_RENDER_DIR=<dir> swift test --filter IslandPluginMarksTests
    func testRenderPluginMarks() async throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else {
            throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render")
        }
        // Plugin logos come from this repo's packages (plugins/<id>/assets/logo.svg), not ~/.agents.
        let plugins = URL(fileURLWithPath: #filePath).deletingLastPathComponent()  // CUAServiceTests
            .appendingPathComponent("../../../../../../").standardized
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)
        var codex = IslandAgent(id: "agent:codex-1", name: "Codex", app: "Codex",
                                title: "Implement plugin marks", status: "running", last_seen_at: 100)
        codex.elapsed_s = 42
        let uses = [
            use("acu", "Agent Computer Use", "Codex"),
            use("comet-control", "Comet", "Claude"),
            use("comet-control", "Comet", "Codex"),
            use("context-ledger", "Context Ledger", "Codex"),
        ]

        let active = IslandModel()
        active.idleActivity = "duo"
        active.apply(IslandSnapshot(kind: "idle", agents: [codex], plugins: uses))
        try await save(active, "plugin-marks-active", dir: dir)

        let cleared = IslandModel()
        cleared.idleActivity = "duo"
        cleared.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        try await save(cleared, "plugin-marks-cleared", dir: dir)

        // One case per plugin: centre (lead slot) = controlled target + ring, right = plugin mark.
        let cases: [(String, IslandPluginUse)] = [
            ("plugin-acu", use("acu", "Agent Computer Use", "Codex")),
            ("plugin-comet-control", use("comet-control", "Comet", "Codex")),
            ("plugin-context-ledger", use("context-ledger", "Context Ledger", "Codex")),
        ]
        for (name, u) in cases {
            let m = IslandModel()
            m.idleActivity = "duo"
            m.apply(IslandSnapshot(kind: "idle", agents: [codex], plugins: [u]))
            try await save(m, name, dir: dir)
        }
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
