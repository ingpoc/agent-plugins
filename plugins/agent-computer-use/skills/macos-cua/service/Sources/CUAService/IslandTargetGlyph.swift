import AppKit
import CoreGraphics
import SwiftUI

private enum IslandGlyphRaster {
    static func centeredSquare(_ pixels: [UInt8], side: Int, bytesPerRow: Int,
                               minX: Int, minY: Int, maxX: Int, maxY: Int,
                               colorSpace: CGColorSpace) -> CGImage? {
        guard minX >= 0, minY >= 0, maxX < side, maxY < side, maxX >= minX, maxY >= minY,
              pixels.count >= side * bytesPerRow else { return nil }
        let width = maxX - minX + 1, height = maxY - minY + 1, edge = max(width, height)
        var square = [UInt8](repeating: 0, count: edge * edge * 4)
        let left = (edge - width) / 2, top = (edge - height) / 2
        for y in 0..<height {
            for x in 0..<width {
                let from = (minY + y) * bytesPerRow + (minX + x) * 4
                let to = ((top + y) * edge + left + x) * 4
                square[to..<(to + 4)] = pixels[from..<(from + 4)]
            }
        }
        return square.withUnsafeMutableBytes { bytes in
            guard let context = CGContext(data: bytes.baseAddress, width: edge, height: edge, bitsPerComponent: 8,
                                          bytesPerRow: edge * 4, space: colorSpace,
                                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
            return context.makeImage()
        }
    }
}

/// The controlled target inside the ring (Guru 2026-11-04 15:27 IST: "have the icon rather than icon inside
/// white square, comet sign is not visible then" / "similarly for apple logo"; 15:29 IST: "that is the design
/// language"). Every controlled target, current or future, draws its BARE logo glyph in the ring on the black
/// pill: never a white tile, never a squircle. One path for all targets:
/// - `.symbol` (ACU → `apple.logo`): the SF Symbol in white.
/// - `.app` (comet-control → Comet): the app's own mark, lifted from its real installed icon by
///   `bareGlyph` (keeps the coloured logo pixels, drops the icon's dark plate and white specks, crops to
///   the logo). If the icon has no coloured mark, or the app is missing, the app's SF Symbol in white.
enum IslandTargetGlyph {
    enum Resolved {
        case image(NSImage)
        case symbol(String)
    }

    /// Glyph box: fits inside the 20 pt ring's ~17 pt inner circle and reads at 1x.
    static let size: CGFloat = 12

    @MainActor private static var cache: [String: Resolved] = [:]

    @MainActor static func resolve(_ target: IslandPluginTarget) -> Resolved {
        switch target {
        case .symbol(let name):
            return .symbol(name)
        case .app(let name):
            if let hit = cache[name] { return hit }
            var result = Resolved.symbol(AppIconResolver.symbols[name] ?? AppIconResolver.fallbackSymbol)
            if let url = AppIconResolver.appURL(for: name) {
                let icon = AppIconResolver.small(NSWorkspace.shared.icon(forFile: url.path))
                var rect = CGRect(origin: .zero, size: icon.size)
                if let cg = icon.cgImage(forProposedRect: &rect, context: nil, hints: nil), let glyph = bareGlyph(cg) {
                    result = .image(NSImage(cgImage: glyph, size: NSSize(width: glyph.width / 2, height: glyph.height / 2)))
                }
            }
            cache[name] = result
            return result
        }
    }

    /// The logo inside an app icon: pixels with clear colour (chroma) keep their alpha, the dark plate,
    /// grey/white specks and the transparent margin go; the result is cropped to the logo, centred in a
    /// square. nil when under 1 % of the icon is coloured (no mark to lift).
    nonisolated static func bareGlyph(_ image: CGImage, side: Int = 128) -> CGImage? {
        let bpr = side * 4
        var px = [UInt8](repeating: 0, count: side * bpr)
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let info = CGImageAlphaInfo.premultipliedLast.rawValue
        guard let ctx = CGContext(data: &px, width: side, height: side, bitsPerComponent: 8, bytesPerRow: bpr,
                                  space: space, bitmapInfo: info) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
        var minX = side, minY = side, maxX = -1, maxY = -1, kept = 0
        for y in 0..<side {
            for x in 0..<side {
                let i = y * bpr + x * 4
                let a = Double(px[i + 3])
                guard a > 0 else { continue }
                let r = Double(px[i]) * 255 / a, g = Double(px[i + 1]) * 255 / a, b = Double(px[i + 2]) * 255 / a
                let hi = max(r, g, b), chroma = hi - min(r, g, b)
                let k = hi < 70 ? 0 : min(1, max(0, (chroma - 40) / 60))
                let na = a * k
                px[i + 3] = UInt8(na.rounded())
                px[i] = UInt8((r * na / 255).rounded()); px[i + 1] = UInt8((g * na / 255).rounded())
                px[i + 2] = UInt8((b * na / 255).rounded())
                if na > 96 {
                    kept += 1
                    minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
                }
            }
        }
        guard kept * 100 >= side * side else { return nil }
        return IslandGlyphRaster.centeredSquare(px, side: side, bytesPerRow: bpr, minX: minX, minY: minY,
                                                 maxX: maxX, maxY: maxY, colorSpace: space)
    }
}

/// Outside-right plugin marks follow the same design language (Guru 2026-11-04 16:06 IST: "the plugin icon is
/// coming in the white square bar, rather have the icon, since currently is not visible properly"): the bare
/// glyph on the black circle, never a white tile. One path for every plugin mark (ACU, Comet Control, Context
/// Ledger, developer-observability and any future plugin): the plugin's designed logo (or app icon) with its
/// plate lifted off by `bareMark`; its dark neutral strokes turn white for contrast on black and its colours
/// stay. developer-observability keeps its vector trace-waterfall mark, also bare.
enum IslandPluginMarkGlyph {
    /// Glyph box inside the 24 pt circle.
    static let size: CGFloat = IslandBarLayout.circleGlyph

    @MainActor private static var cache: [String: IslandTargetGlyph.Resolved] = [:]

    @MainActor static func resolve(_ name: String) -> IslandTargetGlyph.Resolved {
        if let hit = cache[name] { return hit }
        var source: NSImage?
        if name == "acu" || name == "Agent Computer Use",
           let url = AppIconResolver.pluginLogoURL("agent-computer-use") { source = NSImage(contentsOf: url) }
        if source == nil, AppIconResolver.ownIconNames.contains(name) || name == "Agent Computer Use" {
            source = AppIconResolver.ownIcon()
        }
        if source == nil, let url = AppIconResolver.pluginLogoURL(name) { source = NSImage(contentsOf: url) }
        if source == nil, let url = AppIconResolver.appURL(for: name) { source = NSWorkspace.shared.icon(forFile: url.path) }
        var result = IslandTargetGlyph.Resolved.symbol(AppIconResolver.symbols[name] ?? AppIconResolver.fallbackSymbol)
        if let source {
            let small = AppIconResolver.small(source)
            var rect = CGRect(origin: .zero, size: small.size)
            if let cg = small.cgImage(forProposedRect: &rect, context: nil, hints: nil), let g = bareMark(cg) {
                result = .image(NSImage(cgImage: g, size: NSSize(width: g.width / 2, height: g.height / 2)))
            }
        }
        cache[name] = result
        return result
    }

    /// The glyph of a logo drawn on a plate (light or dark): the plate colour is the median of a ring inside the
    /// icon (where logos keep their margin); pixels far from it are the glyph. Neutral glyph pixels become white
    /// (a dark stroke on a light plate would vanish on black); coloured ones keep their colour. Cropped to the
    /// glyph, centred in a square. nil when there is no glyph.
    nonisolated static func bareMark(_ image: CGImage, side: Int = 128) -> CGImage? {
        let bpr = side * 4
        var px = [UInt8](repeating: 0, count: side * bpr)
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let info = CGImageAlphaInfo.premultipliedLast.rawValue
        guard let ctx = CGContext(data: &px, width: side, height: side, bitsPerComponent: 8, bytesPerRow: bpr,
                                  space: space, bitmapInfo: info) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
        func rgb(_ i: Int) -> (Double, Double, Double, Double) {
            let a = Double(px[i + 3]); guard a > 0 else { return (0, 0, 0, 0) }
            return (Double(px[i]) / a, Double(px[i + 1]) / a, Double(px[i + 2]) / a, a / 255)
        }
        // Plate: median colour on a ring at 40 % of the side from the centre.
        var ring: [Int: [(Double, Double, Double)]] = [:]
        let c = Double(side) / 2, r = Double(side) * 0.40
        for k in 0..<72 {
            let t = Double(k) / 72 * 2 * .pi
            let x = Int(c + r * cos(t)), y = Int(c + r * sin(t))
            let v = rgb(y * bpr + x * 4)
            if v.3 > 0.9 { ring[y, default: []].append((v.0, v.1, v.2)) }
        }
        guard ring.values.reduce(0, { $0 + $1.count }) >= 12 else { return nil }
        func median(_ a: [Double]) -> Double { a.sorted()[a.count / 2] }
        let samples = ring.values.flatMap { $0 }
        let base = (median(samples.map(\.0)), median(samples.map(\.1)), median(samples.map(\.2)))
        let rows = ring.mapValues { colors in
            let plate = colors.filter { color in
                max(abs(color.0 - base.0), abs(color.1 - base.1), abs(color.2 - base.2)) < 0.25
            }
            let source = plate.isEmpty ? colors : plate
            return (median(source.map(\.0)), median(source.map(\.1)), median(source.map(\.2)))
        }
        let sampledRows = rows.keys.sorted()
        let plates = (0..<side).map { y -> (Double, Double, Double) in
            let hiIndex = sampledRows.firstIndex(where: { $0 >= y }) ?? sampledRows.count - 1
            let hi = sampledRows[hiIndex], lo = sampledRows[max(0, hiIndex - 1)]
            guard let lower = rows[lo], let upper = rows[hi], hi != lo else { return rows[hi]! }
            let t = Double(y - lo) / Double(hi - lo)
            return (lower.0 + (upper.0 - lower.0) * t,
                    lower.1 + (upper.1 - lower.1) * t,
                    lower.2 + (upper.2 - lower.2) * t)
        }
        var minX = side, minY = side, maxX = -1, maxY = -1, kept = 0
        for y in 0..<side {
            for x in 0..<side {
                let i = y * bpr + x * 4
                let v = rgb(i)
                guard v.3 > 0 else { continue }
                let plate = plates[y]
                let d = max(abs(v.0 - plate.0), abs(v.1 - plate.1), abs(v.2 - plate.2))
                let hi = max(v.0, v.1, v.2), chroma = hi - min(v.0, v.1, v.2)
                let plateLight = (base.0 + base.1 + base.2) / 3 > 0.65 && hi > 0.65 && chroma < 0.18
                let plateDark = (base.0 + base.1 + base.2) / 3 < 0.35 && hi < 0.35 && chroma < 0.18
                let k = plateLight || plateDark ? 0 : min(1, max(0, (d - 0.12) / 0.2))
                // Neutral or low-contrast strokes → white; only readable brand colour remains coloured.
                let colour: (Double, Double, Double)
                if chroma < 0.18 || !contrastOnBlack(red: v.0, green: v.1, blue: v.2) { colour = (1, 1, 1) }
                else { colour = (v.0, v.1, v.2) }
                let na = v.3 * k
                px[i] = UInt8((colour.0 * na * 255).rounded()); px[i + 1] = UInt8((colour.1 * na * 255).rounded())
                px[i + 2] = UInt8((colour.2 * na * 255).rounded()); px[i + 3] = UInt8((na * 255).rounded())
                if na > 0.38 {
                    kept += 1
                    minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
                }
            }
        }
        guard kept * 200 >= side * side else { return nil }
        return IslandGlyphRaster.centeredSquare(px, side: side, bytesPerRow: bpr, minX: minX, minY: minY,
                                                 maxX: maxX, maxY: maxY, colorSpace: space)
    }

    nonisolated private static func contrastOnBlack(red: Double, green: Double, blue: Double) -> Bool {
        func linear(_ c: Double) -> Double { c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        let luminance = 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
        return (luminance + 0.05) / 0.05 >= 4.5
    }
}

/// The one view for an outside-right plugin mark's glyph: bare on the black circle (see `IslandPluginMarkGlyph`).
struct IslandPluginMarkGlyphView: View {
    let name: String
    var size: CGFloat = IslandPluginMarkGlyph.size

    var body: some View {
        if name == IslandDevObsMark.plugin {
            IslandDevObsMark(size: size)
        } else {
            switch IslandPluginMarkGlyph.resolve(name) {
            case .image(let image):
                Image(nsImage: image).resizable().interpolation(.high).scaledToFit()
                    .frame(width: size, height: size)
                    .frame(width: size, height: size)
            case .symbol(let symbol):
                Image(systemName: symbol)
                    .font(.system(size: size, weight: .semibold))
                    .foregroundStyle(Color.white)
                    .frame(width: size, height: size)
            }
        }
    }
}

/// The one view for a controlled target's glyph inside the ring: bare, no tile (see `IslandTargetGlyph`).
struct IslandTargetGlyphView: View {
    let target: IslandPluginTarget
    var size: CGFloat = IslandTargetGlyph.size

    var body: some View {
        switch IslandTargetGlyph.resolve(target) {
        case .image(let image):
            Image(nsImage: image)
                .resizable()
                .interpolation(.high)
                .scaledToFit()
                .frame(width: size, height: size)
        case .symbol(let name):
            Image(systemName: name)
                .font(.system(size: size * 0.92, weight: .semibold))
                .foregroundStyle(Color.white)
                .frame(width: size, height: size)
                .offset(y: -size * 0.03)
        }
    }
}
