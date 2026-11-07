import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Controlled targets use bare glyphs in the pill ring; outside plugin circles use full package art clipped
/// to the circle. Rendering checks protect both treatments from accidental tiles or undersized marks.
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
    private func lightShare<V: View>(_ view: V, side: CGFloat = 20, box boxPt: CGFloat = 12) -> Double {
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

    private func cornerPixels<V: View>(_ view: V, side: CGFloat, box boxPt: CGFloat) -> [[UInt8]] {
        let (px, w, h) = pixels(view, side: side)
        guard w > 0 else { return [] }
        let box = Int((boxPt * scale).rounded()), inset = Int((2 * scale).rounded())
        let x0 = (w - box) / 2, y0 = (h - box) / 2
        let points = [(x0 + inset, y0 + inset), (x0 + box - inset - 1, y0 + inset),
                      (x0 + inset, y0 + box - inset - 1), (x0 + box - inset - 1, y0 + box - inset - 1)]
        return points.map { x, y in
            let i = (y * w + x) * 4
            return Array(px[i..<(i + 4)])
        }
    }

    private func tileCornerShare<V: View>(_ view: V, side: CGFloat = 20, box boxPt: CGFloat = 12) -> Double {
        let points = cornerPixels(view, side: side, box: boxPt)
        return Double(points.filter { $0[0] > 200 && $0[1] > 200 && $0[2] > 200 }.count) / Double(max(1, points.count))
    }

    private func glyphMetrics<V: View>(_ view: V, side: CGFloat, scale: CGFloat) -> (CGRect, CGFloat)? {
        let renderer = ImageRenderer(content: view.frame(width: side, height: side).environment(\.colorScheme, .dark))
        renderer.scale = scale
        guard let cg = renderer.cgImage else { return nil }
        var px = [UInt8](repeating: 0, count: cg.width * cg.height * 4)
        guard let ctx = CGContext(data: &px, width: cg.width, height: cg.height, bitsPerComponent: 8,
                                  bytesPerRow: cg.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
        var minX = cg.width, minY = cg.height, maxX = -1, maxY = -1, peak: UInt8 = 0
        for y in 0..<cg.height {
            for x in 0..<cg.width {
                let i = (y * cg.width + x) * 4
                let alpha = px[i + 3]
                peak = max(peak, alpha)
                if alpha > 96 {
                    minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
                }
            }
        }
        guard maxX >= minX, maxY >= minY else { return nil }
        let rect = CGRect(x: CGFloat(minX) / scale, y: CGFloat(minY) / scale,
                          width: CGFloat(maxX - minX + 1) / scale, height: CGFloat(maxY - minY + 1) / scale)
        return (rect, CGFloat(peak) / 255)
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
        XCTAssertGreaterThan(tileCornerShare(tile), 0.75)
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

    func testPackagedPluginArtUsesItsFull24PointCircularFrame() throws {
        var plugins = URL(fileURLWithPath: #filePath)
        for _ in 0..<7 { plugins.deleteLastPathComponent() }
        let oldRoot = ProcessInfo.processInfo.environment["ACU_PLUGIN_LOGO_ROOT"]
        defer {
            if let oldRoot { setenv("ACU_PLUGIN_LOGO_ROOT", oldRoot, 1) }
            else { unsetenv("ACU_PLUGIN_LOGO_ROOT") }
        }
        setenv("ACU_PLUGIN_LOGO_ROOT", plugins.path, 1)

        for (package, plugin, name) in [
            ("agent-computer-use", "acu", "Agent Computer Use"),
            ("comet-control", "comet-control", "Comet Control"),
            ("context-ledger", "context-ledger", "Context Ledger"),
            ("developer-observability", "developer-observability", "Developer Observability"),
        ] {
            XCTAssertNotNil(AppIconResolver.pluginImage(package), "\(package) package artwork resolves")
            for scale in [CGFloat(1), 2] {
                let renderer = ImageRenderer(content: IslandPluginCircleIcon(plugin: plugin, displayName: name)
                    .frame(width: IslandBarLayout.circleDiameter, height: IslandBarLayout.circleDiameter))
                renderer.scale = scale
                let image = try XCTUnwrap(renderer.cgImage, "\(package) at \(scale)x")
                XCTAssertEqual(image.width, Int(IslandBarLayout.circleDiameter * scale))
                XCTAssertEqual(image.height, Int(IslandBarLayout.circleDiameter * scale))
            }
        }

        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let logo = root.appendingPathComponent("test-circle/assets/logo.svg")
        try FileManager.default.createDirectory(at: logo.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1024\" height=\"1024\" viewBox=\"0 0 1024 1024\"><rect width=\"1024\" height=\"1024\" fill=\"#ff0000\"/></svg>".utf8).write(to: logo)
        setenv("ACU_PLUGIN_LOGO_ROOT", root.path, 1)
        let rendered = pixels(ZStack {
            Circle().fill(Color.black)
            IslandPluginCircleIcon(plugin: "test-circle", displayName: "Test Circle")
        }, side: IslandBarLayout.circleDiameter)
        XCTAssertEqual(rendered.w, Int(IslandBarLayout.circleDiameter * scale))
        let inset = Int((2 * scale).rounded())
        let corner = (inset * rendered.w + inset) * 4
        let center = ((rendered.h / 2) * rendered.w + rendered.w / 2) * 4
        XCTAssertLessThan(rendered.px[corner], 24, "square image corners are clipped by the circle")
        XCTAssertGreaterThan(rendered.px[center], 240, "package artwork fills the full icon frame")
        XCTAssertLessThan(rendered.px[center + 1], 30)
        XCTAssertLessThan(rendered.px[center + 2], 30)
    }

    func testVoiceSlotSitsSixPointsBeforeTheDotInEveryState() {
        let L = IslandBarLayout.self
        XCTAssertEqual(L.voiceDotGap, 6)
        for content in [IslandVoiceSlotContent.listening, .thinking, .speaking, .none] {
            for (controlled, apps) in [(false, 0), (true, 0), (true, 1), (true, 4)] {
                let w = controlled ? L.controlledPillWidth(apps: apps, voice: content) : L.voicePillWidth(content)
                let shape = L.shapeRect(compactWidth: w, card: false)
                let v = L.voiceSlotRect(shape: shape, apps: apps, controlled: controlled, content: content)
                XCTAssertGreaterThanOrEqual(L.compactDotRect(shape: shape).minX - v.maxX, 6, "\(content) controlled=\(controlled) apps=\(apps)")
                if controlled && apps == 0 {
                    XCTAssertEqual(v.minX - L.targetRingOuterRect(shape: shape).maxX, 24, accuracy: 0.5, "ring · 24 · voice group")
                }
            }
        }
    }
}
