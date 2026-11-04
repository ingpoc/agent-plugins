import AppKit
import CoreGraphics
import SwiftUI

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
                if na > 24 {
                    kept += 1
                    minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
                }
            }
        }
        guard kept * 100 >= side * side, let full = ctx.makeImage() else { return nil }
        let w = maxX - minX + 1, h = maxY - minY + 1, edge = max(w, h)
        // Bitmap rows run top-down, the same as `cropping(to:)`.
        guard let crop = full.cropping(to: CGRect(x: minX, y: minY, width: w, height: h)),
              let sq = CGContext(data: nil, width: edge, height: edge, bitsPerComponent: 8, bytesPerRow: 0,
                                 space: space, bitmapInfo: info) else { return nil }
        sq.draw(crop, in: CGRect(x: (edge - w) / 2, y: (edge - h) / 2, width: w, height: h))
        return sq.makeImage()
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
