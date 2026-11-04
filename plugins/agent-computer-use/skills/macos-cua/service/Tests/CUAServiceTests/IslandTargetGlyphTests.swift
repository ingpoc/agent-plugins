import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Design language (Guru 2026-11-04 15:27 / 15:29 IST): a controlled target's logo sits BARE in its ring on
/// the black pill, never on a white tile, for every target, current or future. These tests render the real
/// ring and fail if anything tile-like (a light plate filling the glyph box) is drawn behind the glyph.
@MainActor
final class IslandTargetGlyphTests: XCTestCase {
    private let scale: CGFloat = 4

    /// RGBA8 pixels of `view` rendered on the pill's black.
    private func pixels<V: View>(_ view: V, side: CGFloat) -> (px: [UInt8], w: Int, h: Int) {
        let r = ImageRenderer(content: view.frame(width: side, height: side).background(Color.black)
            .environment(\.colorScheme, .dark))
        r.scale = scale
        guard let cg = r.cgImage else { XCTFail("render failed"); return ([], 0, 0) }
        let w = cg.width, h = cg.height
        var px = [UInt8](repeating: 0, count: w * h * 4)
        let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
        return (px, w, h)
    }

    /// Share of the centred glyph box that is near-white: a tile fills most of it, a bare glyph does not.
    private func lightShare<V: View>(_ view: V, side: CGFloat = IslandPluginTargetMark.ringDiameter,
                                     box boxPt: CGFloat = IslandPluginTargetMark.glyphSize) -> Double {
        let (px, w, h) = pixels(view, side: side)
        guard w > 0 else { return 1 }
        let box = Int((boxPt * scale).rounded())
        let x0 = (w - box) / 2, y0 = (h - box) / 2
        var light = 0
        for y in y0..<(y0 + box) {
            for x in x0..<(x0 + box) {
                let i = (y * w + x) * 4
                if px[i] > 200 && px[i + 1] > 200 && px[i + 2] > 200 { light += 1 }
            }
        }
        return Double(light) / Double(box * box)
    }

    private static let tileLimit = 0.5
    /// Plugin logos carry dark strokes on their plate, so the old tile reads lighter-than-half less often: 0.3.
    private static let markTileLimit = 0.3

    /// The detector itself: the 0.2.87-and-earlier white tile is flagged, so the bare-glyph checks are real.
    func testDetectorFlagsAWhiteTile() {
        let g = IslandPluginTargetMark.glyphSize
        let tile = ZStack {
            RoundedRectangle(cornerRadius: IslandPluginTargetMark.tileRadius(g), style: .continuous)
                .fill(Color.white).frame(width: g, height: g)
            Image(systemName: "apple.logo").font(.system(size: g * 0.64, weight: .semibold)).foregroundStyle(Color.black)
        }
        XCTAssertGreaterThan(lightShare(tile), Self.tileLimit)
    }

    func testEveryControlledTargetGlyphIsBareInTheRing() {
        // Today's targets, plus a future app target and a future symbol target through the same path.
        var targets = ["acu", "comet-control"].compactMap(IslandPluginTarget.of(plugin:))
        XCTAssertEqual(targets, [.symbol("apple.logo"), .app("Comet")])
        targets += [.app("Calculator"), .app("NoSuchApp-\(UUID().uuidString)"), .symbol("globe")]
        for t in targets {
            let share = lightShare(IslandPluginTargetMark(target: t, reduceMotion: true))
            XCTAssertLessThan(share, Self.tileLimit, "\(t): the glyph must sit bare in the ring, no white tile (\(share))")
        }
    }

    /// `bareGlyph` keeps the coloured mark, drops the dark plate and white specks, and crops to the mark.
    func testBareGlyphLiftsTheColouredMarkOffItsPlate() throws {
        let side = 128
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.04, green: 0.04, blue: 0.05, alpha: 1))      // dark icon plate
        ctx.fill(CGRect(x: 8, y: 8, width: 112, height: 112))
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))               // white "stars"
        ctx.fill(CGRect(x: 14, y: 14, width: 3, height: 3)); ctx.fill(CGRect(x: 110, y: 100, width: 3, height: 3))
        ctx.setFillColor(CGColor(red: 0.2, green: 0.8, blue: 0.9, alpha: 1))         // cyan mark
        ctx.fillEllipse(in: CGRect(x: 40, y: 50, width: 48, height: 24))
        let glyph = try XCTUnwrap(IslandTargetGlyph.bareGlyph(try XCTUnwrap(ctx.makeImage()), side: side))
        XCTAssertEqual(glyph.width, glyph.height, "square")
        XCTAssertEqual(Double(glyph.width), 48, accuracy: 2, "cropped to the mark, plate and stars gone")
        // Corners of the square are transparent (the plate is gone); the centre is the opaque cyan mark.
        var px = [UInt8](repeating: 0, count: glyph.width * glyph.height * 4)
        let c = CGContext(data: &px, width: glyph.width, height: glyph.height, bitsPerComponent: 8,
                          bytesPerRow: glyph.width * 4, space: space,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        c.draw(glyph, in: CGRect(x: 0, y: 0, width: glyph.width, height: glyph.height))
        XCTAssertEqual(px[3], 0)
        let mid = ((glyph.height / 2) * glyph.width + glyph.width / 2) * 4
        XCTAssertGreaterThan(px[mid + 3], 240)
        XCTAssertGreaterThan(px[mid + 2], px[mid])  // still cyan, not recoloured
        // A plate with no coloured mark has nothing to lift.
        ctx.setFillColor(CGColor(red: 0.04, green: 0.04, blue: 0.05, alpha: 1)); ctx.fill(CGRect(x: 0, y: 0, width: side, height: side))
        XCTAssertNil(IslandTargetGlyph.bareGlyph(try XCTUnwrap(ctx.makeImage()), side: side))
    }

    /// Outside-right plugin marks (Guru 16:06 IST): bare glyph on the black circle, never a white tile, for every
    /// plugin mark, current or future, through `IslandPluginMarkGlyphView`. Logos load from this checkout.
    func testEveryPluginMarkIsBareOnTheBlackCircle() {
        let plugins = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../../").standardized
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)
        defer { unsetenv("ACU_PLUGIN_LOGO_ROOT") }
        // The pre-16:06 drawing (the plugin logo, white plate and all) is what the detector must catch.
        XCTAssertNotNil(AppIconResolver.pluginLogoURL("comet-control"), "logo found in the checkout")
        let old = lightShare(SamanthaAppIcon(appName: "comet-control"), side: IslandBarLayout.circleDiameter, box: IslandPluginMarkGlyph.size)
        XCTAssertGreaterThan(old, Self.markTileLimit, "the old tiled logo is flagged (\(old))")
        for name in ["Agent Computer Use", "comet-control", "context-ledger", "developer-observability", "future-plugin-\(UUID().uuidString)"] {
            let share = lightShare(IslandPluginMarkGlyphView(name: name), side: IslandBarLayout.circleDiameter, box: IslandPluginMarkGlyph.size)
            XCTAssertLessThan(share, Self.markTileLimit, "\(name): plugin mark must be bare on the black circle (\(share))")
        }
    }

    /// PR #82's fallbacks are bare too (Guru 16:06 IST): a marketplace plugin's manifest icon on a light plate is
    /// lifted off it, and the monogram is letters on the black circle, no colour disc or tile.
    func testFallbackPluginMarksAreBareOnTheBlackCircle() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("acu-marks-\(UUID().uuidString)")
        let id = "acme-" + UUID().uuidString.prefix(8).lowercased()
        let package = root.appendingPathComponent(id)
        try FileManager.default.createDirectory(at: package.appendingPathComponent("assets"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let side = 128
        let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.95, green: 0.95, blue: 0.97, alpha: 1)); ctx.fill(CGRect(x: 0, y: 0, width: side, height: side))
        ctx.setFillColor(CGColor(red: 0.10, green: 0.10, blue: 0.12, alpha: 1))   // a dark "+" mark, stroke-like as logos are
        ctx.fill(CGRect(x: 40, y: 61, width: 48, height: 6)); ctx.fill(CGRect(x: 61, y: 40, width: 6, height: 48))
        let png = try XCTUnwrap(NSBitmapImageRep(cgImage: try XCTUnwrap(ctx.makeImage())).representation(using: .png, properties: [:]))
        try png.write(to: package.appendingPathComponent("assets/mark.png"))
        try Data(#"{"name":"acme","icon":"assets/mark.png"}"#.utf8).write(to: package.appendingPathComponent("plugin.json"))
        setenv("ACU_PLUGIN_LOGO_ROOT", root.path, 1)
        defer { unsetenv("ACU_PLUGIN_LOGO_ROOT") }
        XCTAssertEqual(IslandPluginMarkTier.resolve(plugin: id), .manifestIcon)
        let tiled = try XCTUnwrap(AppIconResolver.pluginImage(id))
        let old = lightShare(Image(nsImage: tiled).resizable().frame(width: IslandBarLayout.circleIcon, height: IslandBarLayout.circleIcon),
                             side: IslandBarLayout.circleDiameter, box: IslandPluginMarkGlyph.size)
        XCTAssertGreaterThan(old, Self.markTileLimit, "the pre-16:06 manifest-icon tile is flagged (\(old))")
        guard case .image = IslandPluginMarkGlyph.resolve(id) else { return XCTFail("manifest icon resolves to a bare glyph") }
        let icon = lightShare(IslandPluginMarkGlyphView(name: id), side: IslandBarLayout.circleDiameter, box: IslandPluginMarkGlyph.size)
        XCTAssertLessThan(icon, Self.markTileLimit, "manifest icon must be bare (\(icon))")
        XCTAssertGreaterThan(icon, 0.02, "the lifted glyph is visible (\(icon))")
        let mono = lightShare(IslandPluginMonogram(plugin: "acme-notes", displayName: "Acme Notes"),
                              side: IslandBarLayout.circleDiameter, box: IslandPluginMarkGlyph.size)
        XCTAssertLessThan(mono, Self.markTileLimit, "monogram must be bare (\(mono))")
        XCTAssertGreaterThan(mono, 0.02, "monogram letters read on black (\(mono))")
    }

    /// `bareMark` lifts a logo off a light plate: plate gone, dark strokes white, the blue accent kept.
    func testBareMarkLiftsALogoOffALightPlate() throws {
        let side = 128
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.93, green: 0.94, blue: 0.96, alpha: 1))   // light plate
        ctx.fill(CGRect(x: 0, y: 0, width: side, height: side))
        ctx.setFillColor(CGColor(red: 0.07, green: 0.07, blue: 0.08, alpha: 1))   // dark stroke
        ctx.fill(CGRect(x: 40, y: 70, width: 48, height: 10))
        ctx.setFillColor(CGColor(red: 0.18, green: 0.48, blue: 0.96, alpha: 1))   // blue accent
        ctx.fill(CGRect(x: 40, y: 50, width: 48, height: 10))
        let g = try XCTUnwrap(IslandPluginMarkGlyph.bareMark(try XCTUnwrap(ctx.makeImage()), side: side))
        XCTAssertEqual(Double(g.width), 48, accuracy: 2, "cropped to the glyph")
        var px = [UInt8](repeating: 0, count: g.width * g.height * 4)
        let c = CGContext(data: &px, width: g.width, height: g.height, bitsPerComponent: 8, bytesPerRow: g.width * 4,
                          space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        c.draw(g, in: CGRect(x: 0, y: 0, width: g.width, height: g.height))
        func at(_ x: Int, _ y: Int) -> [UInt8] { let i = (y * g.width + x) * 4; return Array(px[i..<(i + 4)]) }
        let mid = g.width / 2
        // Rows are top-down: the dark stroke (drawn higher) comes first, then the plate gap, then the blue bar.
        let top = at(mid, g.height / 2 - 12), gap = at(mid, g.height / 2), bottom = at(mid, g.height / 2 + 12)
        XCTAssertGreaterThan(top[0], 240, "dark stroke now white: \(top)"); XCTAssertGreaterThan(top[3], 240)
        XCTAssertLessThan(gap[3], 10, "plate gone: \(gap)")
        XCTAssertGreaterThan(bottom[2], bottom[0], "blue kept: \(bottom)"); XCTAssertGreaterThan(bottom[3], 240)
    }

    /// Guru 16:05 IST approved the live Comet + listening frame as the reference: the voice slot and the dot travel
    /// together at the right end, the slot 6 pt before the dot (measured 6 pt in that frame), and the 24 pt gap sits
    /// between the ring and that group. The same 6 pt holds with nothing controlled and for every voice content,
    /// Samantha's speaking gradient bars included.
    func testVoiceSlotSitsSixPointsBeforeTheDotInEveryState() {
        let L = IslandBarLayout.self
        XCTAssertEqual(L.voiceDotGap, 6)
        for content in [IslandVoiceSlotContent.listening, .thinking, .speaking, .done] {
            for (controlled, apps) in [(false, 0), (true, 0), (true, 1), (true, 4)] {
                if content == .done && !controlled { continue }
                let w = controlled ? L.controlledPillWidth(apps: apps, voice: content) : L.voicePillWidth(content)
                let shape = L.shapeRect(compactWidth: w, card: false)
                let v = L.voiceSlotRect(shape: shape, apps: apps, controlled: controlled, content: content)
                XCTAssertEqual(L.compactDotRect(shape: shape).minX - v.maxX, 6, accuracy: 0.001, "\(content) controlled=\(controlled) apps=\(apps)")
                if controlled && apps == 0 {
                    XCTAssertEqual(v.minX - (shape.minX + L.ringRight), 24, accuracy: 0.001, "ring · 24 · voice group")
                }
            }
        }
    }
}
