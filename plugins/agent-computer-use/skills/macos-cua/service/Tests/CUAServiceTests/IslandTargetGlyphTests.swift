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
    private func lightShare<V: View>(_ view: V) -> Double {
        let side = IslandPluginTargetMark.ringDiameter
        let (px, w, h) = pixels(view, side: side)
        guard w > 0 else { return 1 }
        let box = Int((IslandPluginTargetMark.glyphSize * scale).rounded())
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
}
