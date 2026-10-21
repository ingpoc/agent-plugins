import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// "Island Cursor" logo wiring: template status glyph, app icon, vector mark in sync with mark.svg.
/// Renders: CUA_ISLAND_RENDER_DIR=<dir> swift test --filter BrandTests/testRenderBrand
@MainActor
final class BrandTests: XCTestCase {
    static let brand = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("assets/brand")

    static let appInfo = NSDictionary(contentsOf: brand.deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("service/Resources/Info.plist")) as? [String: Any]

    /// Renders and gates show the app's own Info.plist version, never the xctest runner's.
    static func injectApp() {
        BrandTile.iconURL = brand.appendingPathComponent("AppIcon.icns")
        AppVersion.info = appInfo
    }

    override func setUp() { Self.injectApp() }

    func testAboutShowsAppVersionNeverTheRunner() throws {
        let plugin = try JSONSerialization.jsonObject(with: Data(contentsOf: Self.brand.deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("plugin.json"))) as? [String: Any]
        XCTAssertEqual(AppVersion.short, plugin?["version"] as? String, "About = plugin.json version")
        AppVersion.info = Bundle.main.infoDictionary   // xctest runner
        XCTAssertEqual(AppVersion.short, "dev")
        XCTAssertNil(AppVersion.build)
        AppVersion.info = ["CFBundleIdentifier": AppVersion.bundleID, "CFBundleShortVersionString": "0.2.26", "CFBundleVersion": "412"]
        XCTAssertEqual(AppVersion.build, "(412)")
        Self.injectApp()
    }

    func testNoOrangeBrandLeftovers() {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        SettingsTab.computerUse.tint.usingColorSpace(.sRGB)?.getRed(&r, green: &g, blue: &b, alpha: &a)
        XCTAssertFalse(r > 0.8 && g > 0.4 && g < 0.75 && b < 0.4, "Computer Use tint is no longer orange")
    }

    func testStatusIconIsTemplatePDF() {
        let img = StatusBarController.loadIcon(resources: Self.brand)
        XCTAssertTrue(img.isTemplate, "renders white like the other menu bar icons")
        XCTAssertEqual(img.size, NSSize(width: 18, height: 18))
        XCTAssertTrue(img.representations.contains { $0 is NSPDFImageRep }, "StatusIcon.pdf preferred")
        let none = StatusBarController.loadIcon(resources: URL(fileURLWithPath: "/nonexistent"))
        XCTAssertTrue(none.isTemplate, "fallback is a template too")
    }

    func testAppIconWired() throws {
        XCTAssertNotNil(BrandTile.icon, "AppIcon.icns loads")
        let res = Self.brand.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("service/Resources/Info.plist")
        let plist = try XCTUnwrap(NSDictionary(contentsOf: res) as? [String: Any])
        XCTAssertEqual(plist["CFBundleIconFile"] as? String, "AppIcon")
    }

    func testMarkPathsMatchSVG() throws {
        let svg = try String(contentsOf: Self.brand.appendingPathComponent("mark.svg"), encoding: .utf8)
        let ds = try NSRegularExpression(pattern: #"<path[^>]*\sd="([^"]*)""#)
            .matches(in: svg, range: NSRange(svg.startIndex..., in: svg))
            .map { String(svg[Range($0.range(at: 1), in: svg)!]) }
        XCTAssertEqual(ds, BrandMark.paths.map(\.d), "BrandMark.swift drifted from mark.svg")
        let p = BrandMark.path(BrandMark.paths[0].d, in: CGRect(x: 0, y: 0, width: 18, height: 18))
        XCTAssertFalse(p.isEmpty)
        XCTAssertTrue(CGRect(x: 0, y: 0, width: 18, height: 18).insetBy(dx: -0.01, dy: -0.01).contains(p.boundingRect))
    }

    /// 2x crop of the status item on a dark menu bar (template tinted white) + the About pane.
    func testRenderBrand() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        func save(_ img: CGImage?, _ name: String) throws {
            let img = try XCTUnwrap(img, name)
            let url = URL(fileURLWithPath: dir).appendingPathComponent(name)
            XCTAssertFalse(FileManager.default.fileExists(atPath: url.path), "never overwrite \(name)")
            try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: url)
        }
        // Menu bar: the real template NSImage, tinted as AppKit does for a dark bar (white), beside SF peers.
        let icon = StatusBarController.loadIcon(resources: Self.brand)
        let bar = HStack(spacing: 14) {
            Image(nsImage: icon).renderingMode(.template).foregroundStyle(.white)
            Image(systemName: "wifi").font(.system(size: 14, weight: .medium)).foregroundStyle(.white)
            Image(systemName: "battery.75percent").font(.system(size: 14)).foregroundStyle(.white)
            Text("Wed 21 Oct  2:30 PM").font(.system(size: 13, weight: .medium)).foregroundStyle(.white)
        }
        .padding(.horizontal, 12).frame(height: 24)
        .background(Color(red: 0.16, green: 0.16, blue: 0.20))
        .environment(\.colorScheme, .dark)
        let r = ImageRenderer(content: bar); r.scale = 2
        try save(r.cgImage, "menubar-status-icon-2x-dark.png")
        let z = ImageRenderer(content: bar); z.scale = 8
        if let big = z.cgImage { try save(big.cropping(to: CGRect(x: 0, y: 0, width: Int(48 * 8), height: big.height)), "menubar-status-icon-zoom-dark.png") }
        let store = IslandSettingsStore(defaults: UserDefaults(suiteName: "cua.brand.render.\(UUID().uuidString)")!, tokens: MemoryTokenStore())
        let about = ImageRenderer(content: IslandSettingsRoot(store: store, tab: .about, rendering: true).environment(\.colorScheme, .dark))
        about.scale = 2
        try save(about.cgImage, "settings-about-brand-dark.png")
    }
}
