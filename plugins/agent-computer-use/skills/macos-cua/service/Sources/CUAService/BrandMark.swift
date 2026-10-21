import SwiftUI

/// "Island Cursor" brand mark (18×18 template glyph), drawn as vector paths so it renders the same
/// live and offscreen (ImageRenderer) with no resource bundle. Path data is copied verbatim from
/// assets/brand/mark.svg (M/L/Z only); BrandMarkTests fails if the two drift.
struct BrandMark: View {
    static let viewBox: CGFloat = 18
    static let paths: [(d: String, evenOdd: Bool)] = [
        ("M13.196,7.994 L13.392,7.974 L13.585,7.942 L13.776,7.898 L13.964,7.841 L14.148,7.772 L14.327,7.691 L14.5,7.598 L14.667,7.494 L14.826,7.38 L14.978,7.256 L15.121,7.121 L15.256,6.978 L15.38,6.826 L15.494,6.667 L15.598,6.5 L15.691,6.327 L15.772,6.148 L15.841,5.964 L15.898,5.776 L15.942,5.585 L15.974,5.392 L15.994,5.196 L16,5 L15.994,4.804 L15.974,4.608 L15.942,4.415 L15.898,4.224 L15.841,4.036 L15.772,3.852 L15.691,3.673 L15.598,3.5 L15.494,3.333 L15.38,3.174 L15.256,3.022 L15.121,2.879 L14.978,2.744 L14.826,2.62 L14.667,2.506 L14.5,2.402 L14.327,2.309 L14.148,2.228 L13.964,2.159 L13.776,2.102 L13.585,2.058 L13.392,2.026 L13.196,2.006 L13,2 L5,2 L4.804,2.006 L4.608,2.026 L4.415,2.058 L4.224,2.102 L4.036,2.159 L3.852,2.228 L3.673,2.309 L3.5,2.402 L3.333,2.506 L3.174,2.62 L3.022,2.744 L2.879,2.879 L2.744,3.022 L2.62,3.174 L2.506,3.333 L2.402,3.5 L2.309,3.673 L2.228,3.852 L2.159,4.036 L2.102,4.224 L2.058,4.415 L2.026,4.608 L2.006,4.804 L2,5 L2.006,5.196 L2.026,5.392 L2.058,5.585 L2.102,5.776 L2.159,5.964 L2.228,6.148 L2.309,6.327 L2.402,6.5 L2.506,6.667 L2.62,6.826 L2.744,6.978 L2.879,7.121 L3.022,7.256 L3.174,7.38 L3.333,7.494 L3.5,7.598 L3.673,7.691 L3.852,7.772 L4.036,7.841 L4.224,7.898 L4.415,7.942 L4.608,7.974 L4.804,7.994 L5,8 L6,8 L6,5.5 L6.003,5.401 L6.013,5.303 L6.029,5.206 L6.052,5.11 L6.08,5.016 L6.115,4.924 L6.156,4.834 L6.203,4.747 L6.255,4.663 L6.313,4.583 L6.375,4.507 L6.443,4.436 L6.515,4.368 L6.592,4.306 L6.672,4.249 L6.756,4.197 L6.843,4.151 L6.933,4.111 L7.026,4.077 L7.12,4.049 L7.216,4.027 L7.314,4.012 L7.412,4.003 L7.511,4 L8.218,4 L8.316,4.004 L8.414,4.014 L8.511,4.031 L8.607,4.054 L8.701,4.084 L8.793,4.119 L8.883,4.161 L8.969,4.208 L9.053,4.261 L9.132,4.319 L9.208,4.383 L9.279,4.451 L12.754,8 L13,8 Z", true),
        ("M7.857,5.15 L7.834,5.128 L7.808,5.106 L7.782,5.087 L7.754,5.069 L7.725,5.054 L7.695,5.04 L7.665,5.028 L7.633,5.018 L7.601,5.01 L7.569,5.005 L7.536,5.001 L7.504,5 L7.471,5.001 L7.438,5.004 L7.405,5.009 L7.373,5.016 L7.342,5.026 L7.311,5.037 L7.281,5.05 L7.252,5.066 L7.224,5.083 L7.197,5.102 L7.172,5.123 L7.148,5.145 L7.125,5.169 L7.104,5.194 L7.085,5.221 L7.068,5.249 L7.052,5.278 L7.038,5.308 L7.027,5.339 L7.017,5.37 L7.01,5.402 L7.004,5.434 L7.001,5.467 L7,5.5 L7,15.12 L7.001,15.153 L7.004,15.185 L7.01,15.218 L7.017,15.25 L7.027,15.281 L7.038,15.312 L7.052,15.342 L7.067,15.371 L7.085,15.399 L7.104,15.425 L7.125,15.45 L7.147,15.474 L7.171,15.497 L7.197,15.517 L7.223,15.537 L7.251,15.554 L7.28,15.569 L7.31,15.583 L7.341,15.594 L7.372,15.603 L7.404,15.611 L7.437,15.616 L7.469,15.619 L7.502,15.62 L7.535,15.619 L7.568,15.615 L7.6,15.61 L7.632,15.602 L7.663,15.593 L7.694,15.581 L7.724,15.567 L7.753,15.551 L7.78,15.534 L7.807,15.515 L7.832,15.494 L10.365,13.242 L14.408,13.029 L14.441,13.027 L14.473,13.022 L14.505,13.015 L14.537,13.005 L14.568,12.994 L14.598,12.981 L14.627,12.966 L14.655,12.949 L14.681,12.93 L14.707,12.91 L14.731,12.888 L14.754,12.864 L14.775,12.839 L14.794,12.813 L14.812,12.785 L14.828,12.756 L14.842,12.727 L14.854,12.696 L14.863,12.665 L14.871,12.633 L14.877,12.601 L14.881,12.568 L14.882,12.536 L14.881,12.503 L14.878,12.47 L14.873,12.438 L14.866,12.406 L14.857,12.375 L14.846,12.344 L14.833,12.314 L14.818,12.285 L14.801,12.257 L14.782,12.23 L14.761,12.204 L14.739,12.18 Z", false),
    ]

    var color: Color = .white
    var size: CGFloat = 18

    var body: some View {
        ZStack {
            ForEach(Array(Self.paths.enumerated()), id: \.offset) { _, p in
                BrandMarkShape(d: p.d).fill(color, style: FillStyle(eoFill: p.evenOdd))
            }
        }
        .frame(width: size, height: size)
        .accessibilityLabel("Agent Computer Use")
    }

    /// Minimal SVG path reader for the mark: absolute M, L and Z.
    static func path(_ d: String, in rect: CGRect) -> Path {
        let s = rect.width / viewBox
        var p = Path()
        var cmd: Character = "M"
        var nums: [CGFloat] = []
        func flush() {
            while nums.count >= 2 {
                let pt = CGPoint(x: rect.minX + nums[0] * s, y: rect.minY + nums[1] * s)
                if cmd == "M" { p.move(to: pt); cmd = "L" } else { p.addLine(to: pt) }
                nums.removeFirst(2)
            }
        }
        var token = ""
        func endToken() { if let v = Double(token) { nums.append(CGFloat(v)) }; token = "" }
        for c in d {
            if c == "M" || c == "L" || c == "Z" {
                endToken(); flush()
                if c == "Z" { p.closeSubpath() } else { cmd = c }
            } else if c == "," || c == " " {
                endToken()
            } else if c == "-" && !token.isEmpty {
                endToken(); token = "-"
            } else {
                token.append(c)
            }
        }
        endToken(); flush()
        return p
    }
}

struct BrandMarkShape: Shape {
    let d: String
    func path(in rect: CGRect) -> Path { BrandMark.path(d, in: rect) }
}

/// Wherever an app icon appears (onboarding headers, About): the real AppIcon artwork (the mark on
/// its light tile). Falls back to the vector mark on a light tile if the icon isn't bundled.
struct BrandTile: View {
    var size: CGFloat = 20
    /// Tests / renders point this at assets/brand/AppIcon.icns (no app bundle there).
    nonisolated(unsafe) static var iconURL: URL? = Bundle.main.url(forResource: "AppIcon", withExtension: "icns")
    static var icon: NSImage? { iconURL.flatMap { NSImage(contentsOf: $0) } }

    var body: some View {
        if let img = Self.icon {
            Image(nsImage: img).resizable().interpolation(.high).frame(width: size, height: size)
                .accessibilityLabel("Agent Computer Use")
        } else {
            RoundedRectangle(cornerRadius: size * 0.22, style: .continuous)
                .fill(Color(white: 0.93))
                .frame(width: size, height: size)
                .overlay(BrandMark(color: .black, size: size * 0.72))
        }
    }
}

/// The app's version for About: only ever this app's Info.plist (bundle id com.ingpoc.cua-service),
/// never a host's (the xctest runner). Renders inject Resources/Info.plist via `info`.
enum AppVersion {
    static let bundleID = "com.ingpoc.cua-service"
    nonisolated(unsafe) static var info: [String: Any]? = Bundle.main.infoDictionary

    private static var ours: [String: Any]? {
        guard let i = info, i["CFBundleIdentifier"] as? String == bundleID else { return nil }
        return i
    }
    static var short: String { ours?["CFBundleShortVersionString"] as? String ?? "dev" }
    /// Build number (installer stamps the commit count); nil when it just repeats the version.
    static var build: String? {
        guard let b = ours?["CFBundleVersion"] as? String, !b.isEmpty, b != short else { return nil }
        return "(\(b))"
    }
}
