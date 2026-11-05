import AppKit
import SwiftUI
import XCTest
@testable import CUAService

@MainActor
final class IslandGenericPluginMarksTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var store: IslandSettingsStore!

    override func setUp() {
        suite = "cua.island.generic.plugins.test.\(UUID().uuidString)"
        defaults = TestDefaults.make(suite)
        store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        defaults.set(true, forKey: IslandHub.allowUnsignedKey)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
    }

    private func post(_ hub: IslandHub, _ body: [String: Any], now: Date = Date()) -> IslandHub.Response {
        let request = IslandHub.Request(method: "POST", path: "/api/island/publish", query: [:],
                                        headers: ["host": "127.0.0.1:8777"],
                                        body: try! JSONSerialization.data(withJSONObject: body), localPort: 8777)
        return hub.handle(request, port: 8777, now: now)
    }

    private func pluginBody(_ plugin: String, app: String = "Agent Computer Use") -> [String: Any] {
        ["source": "agent", "id": "plugin:\(plugin):test", "kind": "running", "name": "Codex",
         "app": app, "title": "Using \(plugin)", "plugin": plugin, "ttl_s": 60]
    }

    private func settle() async { try? await Task.sleep(nanoseconds: 350_000_000) }

    func testHubPluginMarkIsOutsideAndUnknownTargetAppDoesNotChangePill() async {
        let hub = IslandHub(store: store)
        var body = pluginBody("acme-notes", app: "Calculator")
        body["target_app"] = "Notes"
        body["plugin_label"] = " Acme Notes "
        XCTAssertEqual(post(hub, body).status, 200)
        let snapshot = hub.compose()
        XCTAssertEqual(snapshot.plugins.map(\.plugin), ["acme-notes"])
        XCTAssertNil(snapshot.plugins.first?.target_app)
        XCTAssertEqual(snapshot.plugins.first?.plugin_label, "Acme Notes")
        XCTAssertNil(IslandPluginTarget.current(snapshot.plugins))
        XCTAssertEqual(IslandOperatedApps.names(snapshot.plugins), [])
        XCTAssertEqual(IslandPluginLabel.markName(plugin: "acme-notes", app: "Notes"), "acme-notes")
        XCTAssertEqual(IslandPluginLabel.display("acme-notes", pluginLabel: snapshot.plugins.first?.plugin_label), "Acme Notes")
        XCTAssertEqual(IslandPluginMonogram.letters(for: "Acme Notes"), "AN")
        XCTAssertEqual(IslandPluginMonogram.paletteIndex(for: "acme-notes"), 3)
        XCTAssertEqual(IslandPluginLabel.display("my-plugin"), "My Plugin")

        let model = IslandModel()
        model.idleActivity = "agents"
        model.apply(IslandSnapshot(kind: "idle"))
        await settle()
        let compactWidth = model.shapeRect.width
        model.apply(snapshot)
        await settle()
        XCTAssertEqual(model.pluginListRows.first?.title, "Using Acme Notes")

        let shape = model.shapeRect
        let overlays = IslandLayoutLogger.overlays(model: model, shape: shape, origin: .zero)
        let marks = overlays.filter { $0.kind == "plugin_mark" }
        XCTAssertEqual(marks.count, 1)
        XCTAssertGreaterThanOrEqual(marks[0].rect.x, Double(shape.maxX) + 2)
        XCTAssertFalse(overlays.contains { ["app_circle", "app_icon", "target_ring", "comet_circle"].contains($0.kind) })
        for overlay in overlays where overlay.expectedInsideIsland {
            let rect = CGRect(x: overlay.rect.x, y: overlay.rect.y, width: overlay.rect.w, height: overlay.rect.h)
            XCTAssertTrue(shape.insetBy(dx: -0.5, dy: -0.5).contains(rect), "\(overlay.id) remains inside the pill")
        }
        XCTAssertFalse(model.circles.contains { if case .app = $0 { return true }; return false })
        XCTAssertFalse(model.showsTargetMark)
        XCTAssertEqual(model.shapeRect.width, compactWidth)
    }

    func testInvalidPluginIDsAreRejected() {
        let hub = IslandHub(store: store)
        for plugin in ["../x", "", "UPPER", String(repeating: "x", count: 60)] {
            XCTAssertEqual(post(hub, pluginBody(plugin)).status, 400, plugin)
        }
    }

    func testSamanthaSnapshotValidatesIDsAndIgnoresUnknownTargetApps() {
        let hub = IslandHub(store: store)
        hub.samanthaActive = true
        func postSnapshot(plugin: String, targetApp: String) -> IslandHub.Response {
            let json: [String: Any] = [
                "source": "samantha", "kind": "idle",
                "plugins": [["plugin": plugin, "app": "Some App", "agent": "Samantha",
                             "last_seen_at": 1, "target_app": targetApp, "plugin_label": " \(String(repeating: "x", count: 60)) "]],
            ]
            let request = IslandHub.Request(method: "POST", path: "/api/island/publish", query: [:],
                                            headers: ["host": "127.0.0.1:8777"],
                                            body: try! JSONSerialization.data(withJSONObject: json), localPort: 8777)
            return hub.handle(request, port: 8777)
        }

        XCTAssertEqual(postSnapshot(plugin: "../x", targetApp: "Notes").status, 400)
        XCTAssertEqual(postSnapshot(plugin: "acme-notes", targetApp: "Notes").status, 200)
        XCTAssertNil(hub.samantha?.plugins.first?.target_app)
        XCTAssertEqual(hub.samantha?.plugins.first?.plugin_label?.count, 48)
    }

    func testKnownPluginArtworkAndManifestPathLookup() throws {
        var pluginRoot = URL(fileURLWithPath: #filePath)
        for _ in 0..<7 { pluginRoot.deleteLastPathComponent() }
        for plugin in ["agent-computer-use", "comet-control", "context-ledger", "developer-observability"] {
            XCTAssertNotNil(AppIconResolver.pluginLogoURL(plugin, root: pluginRoot), "\(plugin) package logo")
        }
        XCTAssertEqual(AppIconResolver.pluginLogoURL("comet-control", root: pluginRoot),
                       pluginRoot.appendingPathComponent("comet-control/assets/logo.svg"))
        var comet = IslandPluginUse(plugin: "comet-control", app: "comet-control", agent: "Cursor", last_seen_at: 1)
        comet.target_app = "Finder"
        XCTAssertEqual(IslandPluginTarget.of(plugin: comet.plugin), .app("Comet"))
        XCTAssertEqual(IslandOperatedApps.names([comet]), [], "only ACU exposes operated app icons")

        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let package = root.appendingPathComponent("new-plugin")
        let bundledLogo = package.appendingPathComponent("assets/logo.svg")
        try FileManager.default.createDirectory(at: bundledLogo.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("<svg/>".utf8).write(to: bundledLogo)
        XCTAssertEqual(AppIconResolver.pluginLogoURL("new-plugin", root: root), bundledLogo)
        try FileManager.default.removeItem(at: bundledLogo)

        let mark = package.appendingPathComponent("branding/mark.svg")
        try FileManager.default.createDirectory(at: mark.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("<svg/>".utf8).write(to: mark)
        try Data(#"{"icon":"branding/mark.svg"}"#.utf8).write(to: package.appendingPathComponent("plugin.json"))
        XCTAssertEqual(AppIconResolver.pluginLogoURL("new-plugin", root: root), mark)
        XCTAssertNil(AppIconResolver.pluginLogoURL("../new-plugin", root: root))
    }

    func testPluginOrderKeepsKnownMarksThenSortsGenericIDsAlphabetically() {
        let uses = [
            IslandPluginUse(plugin: "zeta-tools", app: "Zeta App", agent: "Zed", last_seen_at: 1),
            IslandPluginUse(plugin: "developer-observability", app: "ignored", agent: "Codex", last_seen_at: 1),
            IslandPluginUse(plugin: "acu", app: "Agent Computer Use", agent: "Codex", last_seen_at: 1),
            IslandPluginUse(plugin: "acme-notes", app: "Notes", agent: "Claude", last_seen_at: 1, plugin_label: "Acme Notes"),
            IslandPluginUse(plugin: "context-ledger", app: "context-ledger", agent: "Claude", last_seen_at: 1),
            IslandPluginUse(plugin: "comet-control", app: "comet-control", agent: "Cursor", last_seen_at: 1),
        ]
        XCTAssertEqual(IslandCircles.pluginItems(uses), [
            .plugin(name: "Agent Computer Use", agents: ["Codex"]),
            .plugin(name: "comet-control", agents: ["Cursor"]),
            .plugin(name: "context-ledger", agents: ["Claude"]),
            .plugin(name: "developer-observability", agents: ["Codex"]),
            .plugin(name: "acme-notes", agents: ["Claude"], pluginLabel: "Acme Notes"),
            .plugin(name: "zeta-tools", agents: ["Zed"]),
        ])
        XCTAssertEqual(IslandPluginLabel.tooltip(name: "acme-notes", agents: ["Claude"], pluginLabel: "Acme Notes"),
                       "Acme Notes · Claude")
    }
}
