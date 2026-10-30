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
        AppVersion.info = ["CFBundleIdentifier": AppVersion.bundleID, "CFBundleShortVersionString": "0.2.27", "CFBundleVersion": "412"]
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

    /// Draws the icon's 16/32 px representation as Finder / System Settings would.
    static func pixels(_ img: NSImage, size: Int) -> [Double]? {
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: size * 4, bitsPerPixel: 32) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSGraphicsContext.current?.imageInterpolation = .high
        img.draw(in: NSRect(x: 0, y: 0, width: size, height: size), from: .zero, operation: .copy, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
        guard let data = rep.bitmapData else { return nil }
        return (0..<(size * size * 4)).map { Double(data[$0]) / 255 }
    }

    /// Mean |difference| between the icns's own `size` px entry (icp4 / icp5, what System Settings
    /// shows at 16 / 32) and the 1024 px master downscaled to `size`. Entries extracted with iconutil.
    static func iconNoise(_ icns: URL, size: Int) throws -> Double {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("cua-icns-\(UUID().uuidString).iconset")
        defer { try? FileManager.default.removeItem(at: dir) }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
        p.arguments = ["-c", "iconset", icns.path, "-o", dir.path]
        try p.run(); p.waitUntilExit()
        guard p.terminationStatus == 0,
              let entry = NSImage(contentsOf: dir.appendingPathComponent("icon_\(size)x\(size).png")) else { return 1 }
        let master = try XCTUnwrap(NSImage(contentsOf: brand.appendingPathComponent("AppIcon.iconset/icon_512x512@2x.png")))
        let a = try XCTUnwrap(pixels(entry, size: size)), b = try XCTUnwrap(pixels(master, size: size))
        return zip(a, b).map { abs($0 - $1) }.reduce(0, +) / Double(a.count)
    }

    /// The 16 and 32 px entries must look like the master, not noise (icnsutil wrote broken icp4/icp5).
    func testSmallIconSizesAreNotNoisy() throws {
        let icns = Self.brand.appendingPathComponent("AppIcon.icns")
        for size in [16, 32] {
            let noise = try Self.iconNoise(icns, size: size)
            XCTAssertLessThan(noise, 0.2, "AppIcon.icns \(size) px differs from the master by \(noise): garbled small icon")
        }
        // Optional negative control: the known-bad icnsutil build must fail the same check.
        if let bad = ProcessInfo.processInfo.environment["CUA_BAD_ICNS"] {
            let noise = try Self.iconNoise(URL(fileURLWithPath: bad), size: 32)
            XCTAssertGreaterThan(noise, 0.2, "negative control should be noisy")
        }
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
