import AppKit
import SwiftUI

// MARK: - App icons

/// Real app icon → app-specific SF Symbol → `app.dashed` at 50 % white.
///
/// Blank-circle root cause: names were resolved to guessed paths, and
/// `NSWorkspace.icon(forFile:)` returns the generic white *document* icon for a
/// path that isn't a real .app. TextEdit's real icon is also almost uniformly
/// white at 18 px; the old chip then `.scaledToFill` + `clipShape(Circle)` it at
/// 14 pt, cropping the white squircle into a plain white disc. Now only real
/// .app bundles are used, icons are drawn whole (scaledToFit, no clip), and any
/// icon that renders near-uniform white is rejected as blank.
@MainActor
enum AppIconResolver {
    enum Resolved {
        case image(NSImage)
        case symbol(String)
    }

    private static var cache: [String: Resolved] = [:]

    static let bundleIDs: [String: String] = [
        "Comet": "ai.perplexity.comet",
        "Grok Bot": "com.anysphere.sand",
        "Calculator": "com.apple.calculator",
        "TextEdit": "com.apple.TextEdit",
        "Notes": "com.apple.Notes",
        "Safari": "com.apple.Safari",
        "Finder": "com.apple.finder",
        "Terminal": "com.apple.Terminal",
        "System Settings": "com.apple.systempreferences",
        "Mail": "com.apple.mail",
        "Calendar": "com.apple.iCal",
        "Messages": "com.apple.MobileSMS",
        "Music": "com.apple.Music",
        "Preview": "com.apple.Preview",
        "Google Chrome": "com.google.Chrome",
        "Cursor": "com.todesktop.230313mzl4w4u92",
        "Codex": "com.openai.codex",
        "ChatGPT": "com.openai.chat",
    ]

    static let symbols: [String: String] = [
        "TextEdit": "doc.text", "Comet": "globe", "Safari": "safari", "Finder": "folder",
        "Terminal": "terminal", "Notes": "note.text", "Calculator": "plus.forwardslash.minus",
        "Mail": "envelope", "Calendar": "calendar", "Messages": "message", "Music": "music.note",
        "Preview": "photo", "System Settings": "gearshape", "Cursor": "cursorarrow.rays", "Codex": "terminal", "Grok Bot": "sparkles",
    ]

    static let fallbackSymbol = "app.dashed"

    static func resolve(_ name: String) -> Resolved {
        if let hit = cache[name] { return hit }
        let result: Resolved
        if let url = appURL(for: name),
           case let icon = NSWorkspace.shared.icon(forFile: url.path),
           !isBlank(icon) {
            result = .image(icon)
        } else {
            result = .symbol(symbols[name] ?? fallbackSymbol)
        }
        cache[name] = result
        return result
    }

    /// Only real .app bundles — never a guessed path (that yields the blank document icon).
    static func appURL(for name: String) -> URL? {
        let fm = FileManager.default
        func isApp(_ url: URL?) -> URL? {
            guard let url, url.pathExtension == "app" else { return nil }
            var dir: ObjCBool = false
            return fm.fileExists(atPath: url.path, isDirectory: &dir) && dir.boolValue ? url : nil
        }
        if let running = NSWorkspace.shared.runningApplications.first(where: { $0.localizedName == name }),
           let url = isApp(running.bundleURL) { return url }
        if let id = bundleIDs[name], let url = isApp(NSWorkspace.shared.urlForApplication(withBundleIdentifier: id)) {
            return url
        }
        let dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                    "/Applications/Utilities", NSHomeDirectory() + "/Applications"]
        for dir in dirs {
            if let url = isApp(URL(fileURLWithPath: dir).appendingPathComponent("\(name).app")) { return url }
        }
        return nil
    }

    /// Near-uniform white at the rendered size (mean luminance > 0.88, sd < 0.04
    /// over opaque pixels) reads as a blank disc → treat as missing.
    nonisolated static func isBlank(_ image: NSImage, side: Int = 18) -> Bool {
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0
        ) else { return true }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        image.draw(in: NSRect(x: 0, y: 0, width: side, height: side))
        NSGraphicsContext.restoreGraphicsState()
        var values: [Double] = []
        for y in 0..<side {
            for x in 0..<side {
                guard let c = rep.colorAt(x: x, y: y), c.alphaComponent > 0.5 else { continue }
                values.append(0.2126 * c.redComponent + 0.7152 * c.greenComponent + 0.0722 * c.blueComponent)
            }
        }
        guard values.count >= side * side / 4 else { return true } // mostly transparent
        let mean = values.reduce(0, +) / Double(values.count)
        let sd = sqrt(values.map { ($0 - mean) * ($0 - mean) }.reduce(0, +) / Double(values.count))
        return mean > 0.88 && sd < 0.04
    }

    static func activate(_ name: String) {
        if let running = NSWorkspace.shared.runningApplications.first(where: { $0.localizedName == name }) {
            running.activate(options: [])
            return
        }
        guard let url = appURL(for: name) else { return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
}


// MARK: - Glyphs (§3) — drawn on black, 16 pt, no grey discs

/// Samantha's identity: white 90 % disc with a 6/16 accent dot.
/// Card art block: 56×56, continuous radius 14, holding a 30 pt glyph.
struct SamanthaArtTile<Content: View>: View {
    let fill: Color
    @ViewBuilder var content: () -> Content
    var body: some View {
        RoundedRectangle(cornerRadius: IslandBarLayout.artTileRadius, style: .continuous)
            .fill(fill)
            .frame(width: 56, height: 56)
            .overlay(content())
    }
}

struct SamanthaOrb: View {
    var size: CGFloat = 16
    var dotSize: CGFloat? = nil
    var dim = false
    var body: some View {
        let dot = dotSize ?? size * 6 / 16
        ZStack {
            Circle().fill(Color.white)
            Circle().fill(IslandPalette.accent).frame(width: dot, height: dot)
        }
        .frame(width: size, height: size)
        .opacity(dim ? 0.6 : 1)
    }
}

/// 5 bars × 2 pt, 2 pt gap (18 pt), 3…14 pt tall. Listening rises from the
/// baseline in white; speaking is mirrored about the centre line in the accent.
struct SamanthaBars: View {
    let levels: [Double]
    var mirrored = false
    var body: some View {
        Canvas { ctx, size in
            let n = levels.count
            let total = CGFloat(n) * 2 + CGFloat(max(0, n - 1)) * 2
            var x = (size.width - total) / 2
            for v in levels {
                let h = 3 + CGFloat(max(0, min(1, v))) * 11
                let y = mirrored ? (size.height - h) / 2 : (size.height - 1 - h)
                let rect = CGRect(x: x, y: max(0, y), width: 2, height: h)
                ctx.fill(Path(roundedRect: rect, cornerRadius: 1),
                         with: .color(mirrored ? IslandPalette.accent : Color.white))
                x += 4
            }
        }
        .frame(width: 20, height: 16)
    }
}

/// 3 × 3 pt dots, 3 pt gap, opacity 0.3→1.0 in sequence, 0.9 s per cycle (10 Hz redraw).
struct SamanthaThinkingDots: View {
    var reduceMotion = false
    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.1)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            HStack(spacing: 3) {
                ForEach(0..<3, id: \.self) { i in
                    let phase = (t / 0.9 - Double(i) / 3).truncatingRemainder(dividingBy: 1)
                    let p = phase < 0 ? phase + 1 : phase
                    Circle().fill(Color.white)
                        .frame(width: 3, height: 3)
                        .opacity(reduceMotion ? 0.8 : 0.3 + 0.7 * (1 - p))
                }
            }
            .frame(width: 20, height: 16)
        }
    }
}

/// 12 pt ring, 2 pt stroke on a white 15 % track: spinning 90° accent arc
/// (1.2 s/turn) when total is unknown, or filled to `fraction`.
struct SamanthaProgressRing: View {
    var fraction: Double?
    var reduceMotion = false
    @State private var spin = false
    var body: some View {
        ZStack {
            Circle().stroke(IslandPalette.track, lineWidth: 2)
            if let f = fraction {
                Circle().trim(from: 0, to: max(0.02, min(1, f)))
                    .stroke(IslandPalette.accent, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            } else {
                Circle().trim(from: 0, to: 0.25)
                    .stroke(IslandPalette.accent, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .rotationEffect(.degrees(spin ? 360 : 0))
                    .onAppear {
                        guard !reduceMotion else { return }
                        withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) { spin = true }
                    }
            }
        }
        .frame(width: 12, height: 12)
        .frame(width: 20, height: 16)
    }
}

/// 64×4 capsule like Alcove's Sound HUD.
struct SamanthaProgressBar: View {
    let fraction: Double
    var width: CGFloat = 64
    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(IslandPalette.track)
            Capsule().fill(Color.white).frame(width: max(4, width * CGFloat(max(0, min(1, fraction)))))
        }
        .frame(width: width, height: 4)
    }
}

/// 6 pt signal dot; pulses 1.0→1.3 every 1.6 s for needs-you.
struct SamanthaSignalDot: View {
    let color: Color
    var pulse = false
    var reduceMotion = false
    var differentiate = false
    @State private var up = false
    var body: some View {
        Circle().fill(color)
            .overlay {
                if differentiate && pulse {
                    Text("!").font(.system(size: 6, weight: .black)).foregroundStyle(Color.black)
                }
            }
            .frame(width: 6, height: 6)
            .scaleEffect(up ? 1.3 : 1.0)
            .onAppear {
                guard pulse, !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true)) { up = true }
            }
    }
}

struct SamanthaSymbol: View {
    let name: String
    var color: Color = .white
    var size: CGFloat = 16
    var body: some View {
        Image(systemName: name)
            .font(.system(size: size * 0.82, weight: .semibold))
            .foregroundStyle(color)
            .frame(width: size, height: size)
    }
}

// MARK: - App icons

struct SamanthaAppIcon: View {
    let appName: String
    var size: CGFloat = IslandBarLayout.circleIcon
    var radius: CGFloat = 0

    var body: some View {
        switch AppIconResolver.resolve(appName) {
        case .image(let image):
            Image(nsImage: image)
                .resizable()
                .interpolation(.high)
                .scaledToFit()
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
        case .symbol(let name):
            Image(systemName: name)
                .font(.system(size: size * 0.7, weight: .semibold))
                .foregroundStyle(IslandInk.metaColor)
                .frame(width: size, height: size)
        }
    }
}

/// App icon with a 6 pt amber dot at its top-right (agent approvals).
struct SamanthaAgentBadgeIcon: View {
    let app: String
    var size: CGFloat = 16
    var dot: CGFloat? = nil
    var body: some View {
        let d = dot ?? min(12, max(6, size * 6 / 16))
        return SamanthaAppIcon(appName: app, size: size, radius: size / 4)
            .overlay(alignment: .topTrailing) {
                Circle().fill(IslandPalette.amber)
                    .frame(width: d, height: d)
                    .overlay(Circle().stroke(Color.black, lineWidth: 1))
                    .offset(x: size * 0.12, y: -size * 0.12)
            }
    }
}

// MARK: - Circles

struct SamanthaAppCircle: View {
    let item: IslandCircleItem

    var body: some View {
        ZStack {
            Circle().fill(Color.black)
            switch item {
            case .app(let name, _, let driving):
                SamanthaAppIcon(appName: name)
                Circle().strokeBorder(driving ? IslandPalette.accent : IslandPalette.keyLine,
                                      lineWidth: driving ? 1.5 : 1)
            case .overflow(let n):
                Text("+\(n)")
                    .font(.system(size: 11, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(IslandInk.secondaryColor)
                Circle().strokeBorder(IslandPalette.keyLine, lineWidth: 1)
            }
        }
        .frame(width: IslandBarLayout.circleDiameter, height: IslandBarLayout.circleDiameter)
        .opacity(dimmed ? 0.55 : 1)
        .contentShape(Circle())
        .onTapGesture {
            if case .app(let name, _, _) = item { AppIconResolver.activate(name) }
        }
        .accessibilityLabel(accessibility)
    }

    private var dimmed: Bool {
        if case .app(_, let d, _) = item { return d }
        return false
    }

    private var accessibility: String {
        switch item {
        case .app(let name, _, _): return name
        case .overflow(let n): return "\(n) more apps"
        }
    }
}

/// One circle per source (max 3 + "+N"): urgent groups sort first and retain state markers.
struct SamanthaSourceCircle: View {
    let group: IslandSourceGroup
    var differentiate = false
    var body: some View {
        ZStack {
            Circle().fill(Color.black)
            if group.overflow > 0 {
                Text("+\(group.overflow)")
                    .font(.system(size: 11, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(IslandInk.secondaryColor)
            } else if group.processCount > 0 {
                Image(systemName: "cpu").font(.system(size: 14, weight: .medium)).foregroundStyle(IslandInk.secondaryColor)
            } else {
                SamanthaAppIcon(appName: group.app.isEmpty ? group.id : group.app)
            }
            Circle().strokeBorder(IslandPalette.keyLine, lineWidth: 1)
        }
        .frame(width: IslandBarLayout.circleDiameter, height: IslandBarLayout.circleDiameter)
        .overlay(alignment: .topTrailing) {
            if let count = IslandAgentBadge.count(needsYou: group.needsYou), count >= 2 {
                Text("\(count)")
                    .font(.system(size: 9, weight: .bold)).monospacedDigit()
                    .foregroundStyle(Color.black)
                    .frame(minWidth: 12, minHeight: 12)
                    .background(Capsule().fill(IslandPalette.amber))
                    .offset(x: 3, y: -3)
            } else if IslandAgentBadge.count(needsYou: group.needsYou) == 1 {
                Text("1")
                    .font(.system(size: 8, weight: .heavy).monospacedDigit())
                    .foregroundStyle(Color.black)
                    .frame(width: 12, height: 12)
                    .background(Circle().fill(IslandPalette.amber))
                    .offset(x: 2, y: -2)
            }
        }
        .overlay(alignment: group.needsYou > 0 ? .bottomTrailing : .topTrailing) {
            if group.expired {
                IslandHollowRing().overlay {
                    if differentiate { Text("×").font(.system(size: 7, weight: .bold)).foregroundStyle(IslandPalette.red) }
                }.offset(x: 1, y: group.needsYou > 0 ? 1 : -1)
            }
        }
        .overlay(alignment: .bottomLeading) {
            if differentiate && group.needsYou > 0 {
                Text("!").font(.system(size: 7, weight: .black)).foregroundStyle(Color.black)
                    .frame(width: 9, height: 9).background(Circle().fill(IslandPalette.amber))
            }
        }
        .overlay(alignment: .topLeading) {
            if group.failed {
                Text(differentiate ? "×" : "!")
                    .font(.system(size: 8, weight: .heavy))
                    .foregroundStyle(Color.white)
                    .frame(width: 12, height: 12)
                    .background(Circle().fill(IslandPalette.red))
                    .offset(x: -2, y: -2)
            }
        }
        .contentShape(Circle())
        .accessibilityLabel(accessibility)
    }

    private var accessibility: String {
        if group.processCount > 0 { return "\(group.processCount) local observations detected" }
        var s = group.overflow > 0 ? "\(group.overflow) more agents" : group.id
        if group.needsYou > 0 { s += ", needs you" }
        if group.failed { s += ", an agent failed or stopped updating" }
        if group.expired { s += ", a request timed out" }
        return s
    }
}

/// Timed-out mark: 7 pt hollow red ring (persists until acknowledged).
struct IslandHollowRing: View {
    var size: CGFloat = 7
    var body: some View {
        Circle().strokeBorder(IslandPalette.red, lineWidth: 1.5)
            .background(Circle().fill(Color.black))
            .frame(width: size, height: size)
    }
}

/// Final 60 s: amber ring that empties with the time left, pulsing.
struct IslandCountdownRing: View {
    let remaining: Int
    var size: CGFloat = 12
    var reduceMotion = false
    @State private var up = false
    var body: some View {
        ZStack {
            Circle().stroke(IslandPalette.amber.opacity(0.25), lineWidth: 2)
            Circle().trim(from: 0, to: max(0.02, min(1, Double(remaining) / IslandPeekScheduler.finalLead)))
                .stroke(IslandPalette.amber, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: size, height: size)
        .scaleEffect(up ? 1.15 : 1)
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true)) { up = true }
        }
        .accessibilityLabel("\(remaining) seconds left")
    }
}

/// Card art border for the final 60 s (amber, pulsing).
struct IslandPulseBorder: View {
    var reduceMotion = false
    @State private var on = false
    var body: some View {
        RoundedRectangle(cornerRadius: IslandBarLayout.artTileRadius, style: .continuous)
            .strokeBorder(IslandPalette.amber, lineWidth: 2)
            .opacity(on ? 1 : 0.35)
            .onAppear {
                guard !reduceMotion else { on = true; return }
                withAnimation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true)) { on = true }
            }
    }
}

// MARK: - Island

/// Small amber "!" on the pill's lower-right corner while a required consent is missing.
struct IslandPermissionBadge: View {
    static let size: CGFloat = 14
    let label: String
    var body: some View {
        Circle().fill(Color(nsColor: .systemOrange))
            .overlay(Image(systemName: "exclamationmark").font(.system(size: 8, weight: .heavy)).foregroundStyle(.black))
            .overlay(Circle().strokeBorder(Color.black, lineWidth: 1.5))
            .frame(width: Self.size, height: Self.size)
            .help("\(label) permission is off. Click to fix.")
            .accessibilityElement()
            .accessibilityLabel("\(label) permission is off")
            .accessibilityAddTraits(.isButton)
    }
}

struct SamanthaIslandBar: View {
    @ObservedObject var model: IslandModel
    var barHeight: CGFloat = IslandBarLayout.compactHeight
    var maxWidth: CGFloat = .greatestFiniteMagnitude

    @Environment(\.accessibilityReduceMotion) private var envReduceMotion
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    /// SwiftUI's value OR AppKit's live `accessibilityDisplayShouldReduceMotion` (model mirrors it).
    private var reduceMotion: Bool { envReduceMotion || model.systemReduceMotion }

    var body: some View {
        let rect = model.shapeRect
        ZStack(alignment: .topLeading) {
            Color.clear
            // The whole pill is the click target: the band above it (to the screen edge) and 4 pt around.
            let target = IslandBarLayout.pillTarget(shape: rect)
            Color.black.opacity(0.001)
                .frame(width: target.width, height: target.height)
                .contentShape(Rectangle())
                .gesture(SpatialTapGesture().onEnded { v in
                    // Logged in shape coordinates (y < 0 = the band above the pill).
                    model.click(at: CGPoint(x: v.location.x + target.minX - rect.minX, y: v.location.y + target.minY - rect.minY))
                })
                .offset(x: target.minX, y: target.minY)
                .allowsHitTesting(!model.idleHidden)
            ghostStack(rect)
            // Fixed window-size container: only the path (and offsets) animate, never a frame.
            shape.modifier(IslandMotionProbe(size: rect.size))
            if !model.cardOpen, [.idle, .doneSettled].contains(model.presentation),
               let attention = model.compactAgentAttention {
                let agent = attention.agent
                let status = agent.status == "needs_you" || agent.needs_you ? "Needs you"
                    : agent.status == "stale" ? "Updates stopped" : "Failed"
                let symbol = agent.status == "needs_you" || agent.needs_you ? "hand.raised.fill"
                    : agent.status == "stale" ? "clock.badge.exclamationmark" : "exclamationmark.triangle.fill"
                HStack(spacing: 5) {
                    Image(systemName: symbol).font(.system(size: 12, weight: .semibold)).frame(width: 14)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(agent.name.isEmpty ? agent.app : agent.name)
                            .font(.system(size: 11, weight: .semibold)).lineLimit(1)
                        Text(status).font(.system(size: 10, weight: .medium)).lineLimit(1)
                    }
                    .foregroundStyle(Color.white)
                }
                .frame(width: rect.width - 16, height: IslandBarLayout.compactHeight, alignment: .leading)
                .padding(.horizontal, 8)
                .offset(x: rect.minX, y: rect.minY)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(agent.name.isEmpty ? agent.app : agent.name), \(status)\(attention.parentName.map { ", child of \($0)" } ?? "")")
            }
            if model.showAgentCircle {
                let rs = IslandBarLayout.agentRects(shape: rect, count: model.sourceGroups.count)
                ForEach(Array(zip(model.sourceGroups, rs)), id: \.0.id) { g, r in
                    SamanthaSourceCircle(group: g, differentiate: model.systemDifferentiate)
                        // https://developer.apple.com/documentation/swiftui/view/accessibilityelement(children:)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(IslandAgentAccessibility.label(model.agents))
                        .accessibilityAddTraits(.isButton)
                        .accessibilityAction { model.openAgentList() }
                        .accessibilityHidden(g.id != model.sourceGroups.first?.id)
                        .onTapGesture { model.openAgentList() }
                        .offset(x: r.minX, y: r.minY)
                        .transition(circleTransition)
                }
            }
            if let sec = model.secondary, let sr = model.secondaryRect {
                IslandSecondaryPill(model: model, activity: sec, reduceMotion: reduceMotion)
                    .onTapGesture { model.tapSecondary() }
                    .contextMenu { Button("Swap activities") { model.swapActivities() } }
                    .offset(x: sr.minX, y: sr.minY)
                    .transition(IslandMotion.popTransition(reduce: reduceMotion, anchor: .leading))
                    .id("secondary-\(sec.rawValue)")
            }
            if let warn = model.permissionWarning, !model.cardOpen {
                IslandPermissionBadge(label: warn)
                    .onTapGesture { model.onPermissionWarningTap?() }
                    .offset(x: rect.maxX - IslandPermissionBadge.size / 2 - 2, y: rect.maxY - IslandPermissionBadge.size / 2 - 2)
                    .transition(.opacity)
            }
            let rects = IslandBarLayout.circleRects(shape: model.circleAnchor, count: model.circleCount)
            ForEach(Array(zip(model.circles, rects)), id: \.0.id) { item, r in
                SamanthaAppCircle(item: item)
                    .offset(x: r.minX, y: r.minY)
                    .transition(circleTransition)
            }
        }
        .frame(width: IslandBarLayout.windowSize.width, height: IslandBarLayout.windowSize.height, alignment: .topLeading)
        .coordinateSpace(name: IslandLayoutRecorder.space)
        .background(layoutContainers(rect))
        .opacity(model.idleHidden ? 0 : 1)
        .animation(.easeInOut(duration: 0.2), value: model.idleHidden)
        .animation(openAnimation, value: model.cardOpen)
        .animation(widthAnimation, value: model.drawnWidth)
        .animation(widthAnimation, value: model.circles)
        .animation(widthAnimation, value: model.sourceGroups)
        .animation(IslandMotion.pop(reduce: reduceMotion), value: model.agentApproval?.id)
        .animation(IslandMotion.pop(reduce: reduceMotion), value: model.secondary)
        .animation(IslandMotion.target(model.trayTargeted, reduce: reduceMotion), value: model.trayTargeted)
    }

    /// Layout gate (tests only): the pill / card and the Duo pill with their minimum insets.
    private func layoutContainers(_ rect: CGRect) -> some View {
        if IslandLayoutRecorder.enabled {
            let expanded = model.cardOpen || model.trayTargeted
            IslandLayoutRecorder.shared.container(.init(name: "shape", rect: rect,
                inset: CGSize(width: IslandLayoutGate.minInset, height: expanded ? IslandLayoutGate.minInset : 4)))
            if let s = model.secondaryRect {
                IslandLayoutRecorder.shared.container(.init(name: "secondary", rect: s, inset: CGSize(width: IslandLayoutGate.minInset, height: 4)))
            }
        }
        return Color.clear
    }

    /// Two-layer ghost stack under an open card when more cards are queued.
    @ViewBuilder
    private func ghostStack(_ rect: CGRect) -> some View {
        let layers = model.cardOpen && model.presentation == .agentApproval && model.cardMode == .samantha
            ? min(2, max(0, model.pendingCount - 1)) : 0
        ForEach((1...2).reversed().filter { $0 <= layers }, id: \.self) { i in
            RoundedRectangle(cornerRadius: IslandBarLayout.cardRadius, style: .continuous)
                .fill(Color(white: i == 1 ? 0.10 : 0.06))
                .overlay(RoundedRectangle(cornerRadius: IslandBarLayout.cardRadius, style: .continuous)
                    .strokeBorder(IslandPalette.keyLine, lineWidth: 1))
                .frame(width: rect.width - CGFloat(i) * 24, height: rect.height)
                .offset(x: rect.minX + CGFloat(i) * 12, y: rect.minY + CGFloat(i) * 6)
                .transition(.opacity)
                .allowsHitTesting(false)
        }
    }

    // MARK: Motion (§7)

    private var openAnimation: Animation { IslandMotion.open(cardOpen: model.cardOpen, reduce: reduceMotion) }

    private var widthAnimation: Animation { IslandMotion.width(reduce: reduceMotion) }

    private var circleTransition: AnyTransition { IslandMotion.popTransition(reduce: reduceMotion) }

    private var glyphSwap: AnyTransition {
        if reduceMotion { return .opacity }
        if #available(macOS 14.0, *) { return AnyTransition(.blurReplace) }
        return .opacity
    }

    // MARK: Shape

    private var shape: some View {
        let rect = model.shapeRect
        let radius = model.shapeRadius
        let expanded = model.cardOpen || model.trayTargeted
        let win = IslandBarLayout.windowSize
        let path = IslandShapePath(rect: rect, radius: radius)
        return ZStack(alignment: .topLeading) {
            path.fill(Color.black)
                // Polish bar: shadow only when expanded (0/8/24, black 35 %); none with Reduce Transparency.
                .shadow(color: .black.opacity(expanded && !reduceTransparency ? 0.35 : 0), radius: 12, x: 0, y: 8)
            path.strokeBorder(model.trayTargeted ? IslandPalette.accent
                              : (expanded && (model.contrastOutline || model.systemIncreaseContrast) ? Color.white.opacity(IslandInk.increasedOutline) : .clear),
                              lineWidth: model.trayTargeted ? 1 : 0.5)
            // Content keeps pinned frames (its target size) in a fixed container; the spring moves it with
            // an offset and reveals it through the animated path — no relayout or text reflow mid-spring.
            shapeContent(rect)
                .frame(width: win.width, height: win.height, alignment: .topLeading)
                .offset(x: rect.minX, y: rect.minY)
                .clipShape(path)
        }
        .frame(width: win.width, height: win.height, alignment: .topLeading)
        .scaleEffect(model.hovering && !model.cardOpen && !reduceMotion ? 1.04 : 1,
                     anchor: UnitPoint(x: rect.midX / win.width, y: rect.minY / win.height))
        .animation(.interactiveSpring(response: 0.25), value: model.hovering)
        .contentShape(path)
        .gesture(SpatialTapGesture().onEnded { v in model.click(at: CGPoint(x: v.location.x - rect.minX, y: v.location.y - rect.minY)) })
        .contextMenu {
            if model.secondary != nil { Button("Swap activities") { model.swapActivities() } }
            if !model.tray.isEmpty { Button("Clear File Tray") { model.clearTray() } }
            Button("ACU Settings…") { model.onOpenSettings?() }
        }
        // https://developer.apple.com/documentation/swiftui/view/accessibilityelement(children:)
        .accessibilityElement(children: model.cardOpen ? .contain : .combine)
        // https://developer.apple.com/documentation/swiftui/view/accessibilitylabel(_:)-1d7jv
        .accessibilityLabel(model.accessibilityText)
        .accessibilityValue(model.cardOpen ? "Expanded" : "Collapsed")
        .accessibilityAddTraits(model.cardOpen ? [] : .isButton)
        // https://developer.apple.com/documentation/swiftui/view/accessibilityaction(_:_:)
        .accessibilityAction { model.toggleCard(byClick: false) }
    }

    @ViewBuilder
    private func shapeContent(_ rect: CGRect) -> some View {
        ZStack(alignment: .topLeading) {
            if model.trayTargeted && !model.cardOpen {
                IslandDropTarget()
                    .frame(width: IslandBarLayout.dropTargetSize.width, height: IslandBarLayout.dropTargetSize.height)
                    .layoutRect("drop-glyph", .glyph, CGRect(x: rect.midX - 14, y: rect.midY - 12, width: 28, height: 24))
                    .transition(.opacity)
            }
            if model.presentation == .peek, !model.cardOpen, let item = model.peek {
                IslandPeekCompact(item: item)
                    .frame(width: rect.width, height: IslandBarLayout.compactHeight, alignment: .leading)
                    .transition(glyphSwap)
                    .id("peek-\(item.id)")
            }
            if model.cardOpen {
                Group {
                    if model.cardMode == .agents { agentList } else { cardContent }
                }
                // Pinned to the card's own size (never the interpolating shape).
                .frame(width: IslandBarLayout.cardSize.width, height: model.cardHeight, alignment: .topLeading)
                .transition(.asymmetric(
                    insertion: .opacity.animation(.easeOut(duration: reduceMotion ? 0.15 : 0.12).delay(reduceMotion ? 0 : 0.18)),
                    removal: .opacity.animation(reduceMotion ? IslandMotion.crossfade : IslandMotion.contentOut)
                ))
            }
            // Lead and trail are persistent views: they travel (and scale) between the compact
            // slots and the card grid on the same open/close spring — never a cross-fade between two copies.
            if model.credentialRequest == nil && !(model.cardOpen && model.cardMode == .agents) && !model.trayTargeted && model.presentation != .peek
                && !(model.cardOpen && [.calendar, .tray].contains(model.presentation)) {
                leadMorph
                    // Lead is drawn through a scale effect: register its on-screen slot from the layout constants.
                    .layoutRect(open ? "card-art" : "lead", .glyph,
                                open ? CGRect(x: rect.minX + IslandBarLayout.cardPadding, y: rect.minY + IslandBarLayout.cardPadding, width: 56, height: 56)
                                     : CGRect(x: rect.minX + IslandBarLayout.slotInset, y: rect.minY + (IslandBarLayout.compactHeight - 16) / 2, width: 16, height: 16))
                if hasTrail { trailMorph }
            }
        }
    }

    // MARK: Compact (§4): leading = who/what, trailing = live state, centre empty

    /// Compact (§4): leading = who/what, trailing = live state, centre empty — drawn by
    /// `leadMorph` / `trailMorph` so they can travel into the card.
    private var open: Bool { model.cardOpen }

    private var leadMorph: some View {
        let art = model.card.art
        let lead56 = lead(size: 56).id("lead-\(model.presentation.motionFamily)").transition(glyphSwap)
        return ZStack {
            lead56.opacity(open ? 0 : 1)
            self.art(art).opacity(open ? 1 : 0)
        }
        .frame(width: 56, height: 56)
        .scaleEffect(open ? 1 : 16.0 / 56.0, anchor: .topLeading)
        .offset(x: open ? IslandBarLayout.cardPadding : IslandBarLayout.slotInset,
                y: open ? IslandBarLayout.cardPadding : (IslandBarLayout.compactHeight - 16) / 2)
        .animation(.easeInOut(duration: 0.2), value: model.presentation)
        .allowsHitTesting(false)
    }

    private var trailMorph: some View {
        let rect = model.shapeRect
        let compactW: CGFloat = model.presentation == .actingSteps ? 64 : model.presentation == .hud ? IslandBarLayout.hudTrailWidth(shapeWidth: rect.width) : 20
        return ZStack(alignment: .topTrailing) {
            compactTrail.id("trail-\(model.presentation.motionFamily)").transition(glyphSwap)
                .modifier(TrailProbe(on: !open, id: "trail"))
                // Trailing-aligned: content wider than the slot (e.g. "8m •") grows inward, never past the 8 pt inset.
                .frame(width: compactW, height: 16, alignment: .trailing).opacity(open ? 0 : 1)
            trail.modifier(TrailProbe(on: open, id: "card-trail")).frame(width: 20, height: 16).opacity(open ? 1 : 0)
        }
        .frame(width: open ? 20 : compactW, height: 16, alignment: .topTrailing)
        .offset(x: open ? IslandBarLayout.cardTrailOrigin.x : rect.width - IslandBarLayout.slotInset - compactW,
                y: open ? IslandBarLayout.cardTrailOrigin.y : (IslandBarLayout.compactHeight - 16) / 2)
        .animation(.easeInOut(duration: 0.2), value: model.presentation)
        .allowsHitTesting(false)
    }

    private var hasTrail: Bool {
        ![.idle, .doneSettled, .peek].contains(model.presentation)
    }

    @ViewBuilder
    private func lead(size: CGFloat) -> some View {
        switch model.presentation {
        case .idle, .doneSettled:
            SamanthaOrb(size: size, dim: true)
        case .reconnecting:
            SamanthaSymbol(name: "wifi.slash", color: IslandPalette.amber, size: size)
        case .listening:
            SamanthaSymbol(name: "mic.fill", size: size)
        case .speaking:
            SamanthaSymbol(name: "speaker.wave.2.fill", color: IslandPalette.accent, size: size)
        case .thinking:
            SamanthaOrb(size: size)
        case .acting, .actingSteps:
            let a = model.drivingApp.isEmpty ? model.app : model.drivingApp
            if a.isEmpty { SamanthaOrb(size: size) } else { SamanthaAppIcon(appName: a, size: size, radius: size / 4) }
        case .needsYou:
            SamanthaSymbol(name: "hand.raised.fill", color: IslandPalette.amber, size: size)
        case .error:
            SamanthaSymbol(name: model.title == "Action not confirmed" ? "questionmark.circle.fill" : "xmark.circle.fill",
                            color: model.title == "Action not confirmed" ? IslandPalette.amber : IslandPalette.red, size: size)
        case .done:
            SamanthaSymbol(name: "checkmark.circle.fill", color: IslandPalette.green, size: size)
        case .nowPlaying:
            NowPlayingArtwork(info: model.nowPlaying, size: size, flip: model.mediaStyle.artworkFlip && !reduceMotion)
        case .agentApproval:
            if model.duoWithMedia {
                // Duo: music keeps the lead; the choice sits in the trail.
                NowPlayingArtwork(info: model.nowPlaying, size: size, flip: false)
            } else {
                // One amber mark only (in the trail): the source icon here carries no dot.
                SamanthaAppIcon(appName: model.agentApproval?.app ?? "", size: size, radius: size / 4)
            }
        case .expired:
            IslandHollowRing(size: size).padding(size * 0.12)
        case .hud:
            if let h = model.hud { IslandHUDGlyph(state: h, size: size) } else { Color.clear.frame(width: size, height: size) }
        case .peek:
            if let p = model.peek { IslandPeekIcon(item: p, size: size) } else { Color.clear.frame(width: size, height: size) }
        case .calendar:
            IslandCalendarGlyph(event: model.calendarNext, size: size, lead: model.calendarLead)
        case .tray:
            SamanthaSymbol(name: "tray.full.fill", size: size)
        }
    }

    @ViewBuilder
    private var compactTrail: some View {
        if model.presentation == .actingSteps {
            SamanthaProgressBar(fraction: stepFraction).frame(height: 16)
        } else if model.presentation == .hud {
            IslandHUDBar(fraction: model.hud?.fraction ?? 0, width: IslandBarLayout.hudTrailWidth(shapeWidth: model.shapeRect.width), animation: reduceMotion ? nil : model.hudBarAnimation).frame(height: 16)
        } else {
            trail
        }
    }

    private var stepFraction: Double {
        model.stepTotal > 0 ? Double(min(model.stepIndex, model.stepTotal)) / Double(model.stepTotal) : 0
    }

    /// Same view in the compact trailing slot and the card's top-right (20×16).
    @ViewBuilder
    private var trail: some View {
        switch model.presentation {
        case .idle, .doneSettled: Color.clear.frame(width: 20, height: 16)
        case .nowPlaying:
            if open {
                SamanthaSymbol(name: model.mediaDevice.symbol, color: IslandInk.secondaryColor).frame(width: 20, height: 16)
                    .accessibilityLabel(model.mediaDevice.name)
            } else if model.mediaStyle.compactWaveform {
                NowPlayingWaveform(playing: model.nowPlaying?.playing ?? false, color: NowPlayingTint.wave(model),
                                   reduceMotion: reduceMotion)
                    .frame(width: 20, height: 16)
            } else {
                Color.clear.frame(width: 20, height: 16)
            }
        case .listening: SamanthaBars(levels: model.bars5)
        case .thinking, .reconnecting: SamanthaThinkingDots(reduceMotion: reduceMotion)
        case .speaking: SamanthaBars(levels: model.bars5, mirrored: true)
        case .acting: SamanthaProgressRing(fraction: nil, reduceMotion: reduceMotion)
        case .actingSteps: SamanthaProgressRing(fraction: stepFraction)
        case .needsYou:
            SamanthaSignalDot(color: IslandPalette.amber, pulse: true, reduceMotion: reduceMotion, differentiate: model.systemDifferentiate).frame(width: 20, height: 16)
        case .error: SamanthaSymbol(name: model.title == "Action not confirmed" ? "eye" : "arrow.clockwise", color: IslandInk.secondaryColor).frame(width: 20, height: 16)
        case .done:
            let a = model.app.isEmpty ? (model.circleApps.first ?? "") : model.app
            Group {
                if a.isEmpty { SamanthaSymbol(name: "checkmark", color: IslandInk.secondaryColor) }
                else { SamanthaAppIcon(appName: a, size: 16, radius: 4) }
            }
            .frame(width: 20, height: 16)
        case .expired:
            IslandHollowRing(size: 10).frame(width: 20, height: 16)
        case .hud, .peek:
            Color.clear.frame(width: 20, height: 16)
        case .calendar:
            IslandCalendarDot(event: model.calendarNext, useCalendarColor: model.useCalendarColor, countdown: true,
                              // The dot means one thing: a meeting link is available (Join). Otherwise none.
                              showDot: model.secondary == nil && model.calendarNext?.meetingURL != nil)
        case .tray:
            Text("\(model.tray.count)").font(.system(size: 11, weight: .semibold).monospacedDigit())
                .foregroundStyle(IslandInk.secondaryColor).frame(width: 20, height: 16)
                .contentTransition(.numericText())
        case .agentApproval:
            if open && model.isChoiceCard {
                SamanthaSymbol(name: "xmark", color: IslandInk.metaColor).frame(width: 20, height: 16)
            } else if model.duoWithMedia {
                SamanthaAppIcon(appName: model.agentApproval?.app ?? "", size: 14, radius: 3.5)
                    .overlay(alignment: .topTrailing) {
                        if model.pendingCount > 1 {
                            Text("\(model.pendingCount)").font(.system(size: 8, weight: .bold)).monospacedDigit()
                                .foregroundStyle(Color.black).frame(minWidth: 10, minHeight: 10)
                                .background(Capsule().fill(IslandPalette.amber)).offset(x: 4, y: -3)
                        } else if let r = model.cardRemaining {
                            IslandCountdownRing(remaining: r, size: 8, reduceMotion: reduceMotion).offset(x: 3, y: -3)
                        } else {
                            SamanthaSignalDot(color: IslandPalette.amber, pulse: true, reduceMotion: reduceMotion, differentiate: model.systemDifferentiate).offset(x: 3, y: -3)
                        }
                    }
                    .frame(width: 20, height: 16)
            } else if let r = model.cardRemaining, !open {
                IslandCountdownRing(remaining: r, reduceMotion: reduceMotion).frame(width: 20, height: 16)
            } else if model.pendingCount > 1 {
                Text("\(model.pendingCount)")
                    .font(.system(size: 11, weight: .bold)).monospacedDigit()
                    .foregroundStyle(Color.black)
                    .frame(minWidth: 16, minHeight: 16)
                    .background(Capsule().fill(IslandPalette.amber))
                    .frame(width: 20, height: 16)
            } else {
                SamanthaSignalDot(color: IslandPalette.amber, pulse: true, reduceMotion: reduceMotion, differentiate: model.systemDifferentiate).frame(width: 20, height: 16)
            }
        }
    }

    // MARK: Card (§5 grid, §6 content)

    @ViewBuilder
    private var cardContent: some View {
        if let request = model.credentialRequest {
            CredentialEntryView(request: request, model: model).id(request.id)
        } else if model.presentation == .nowPlaying {
            NowPlayingCardBody(model: model)
        } else if model.presentation == .calendar {
            CalendarCardBody(model: model)
        } else if model.presentation == .tray {
            TrayCardBody(model: model, onShare: model.onShare)
        } else {
            standardCardContent
        }
    }

    private var standardCardContent: some View {
        standardCardBody
            .id(model.agentApproval?.id ?? "samantha")
            .transition(reduceMotion ? .opacity : .asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity),
                                                               removal: .opacity))
    }

    private var standardCardBody: some View {
        let card = model.card
        let p = IslandBarLayout.cardPadding
        let inner = IslandBarLayout.cardSize.width - 2 * p
        return ZStack(alignment: .topLeading) {
            VStack(alignment: .leading, spacing: 4) {
                Text(card.title)
                    .font(card.valueTitle ? .system(size: 28, weight: .semibold).monospacedDigit()
                                          : .system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.white)
                    .lineLimit(model.isChoiceCard || [.agentApproval, .expired].contains(model.presentation) ? 2 : 1)
                    .layoutProbe("card-title", truncates: true)
                    .frame(width: IslandBarLayout.cardTrailOrigin.x - IslandBarLayout.cardTextX - 8, alignment: .leading)
                if !card.subtitle.isEmpty {
                    Text(card.subtitle)
                        .font(.system(size: 13))
                        .foregroundStyle(IslandInk.secondaryColor)
                        .lineLimit(model.presentation == .needsYou || model.agentApproval?.destructive == true
                            ? nil : (model.isChoiceCard ? 1 : 2))
                        .truncationMode(.tail)
                        .frame(width: IslandBarLayout.cardTrailOrigin.x - IslandBarLayout.cardTextX - 8, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                        .layoutProbe("card-subtitle", truncates: model.presentation != .needsYou)
                }
            }
            .offset(x: IslandBarLayout.cardTextX, y: p)
            if model.cardOpen && (model.keyboardActive || model.agentApproval != nil) {
                Text("esc").font(.system(size: 11)).foregroundStyle(IslandInk.metaColor)
                    .offset(x: IslandBarLayout.cardTrailOrigin.x - 8, y: 18)
                    .accessibilityHidden(true)
            }
            if case .choice(let a) = card.controls {
                IslandChoiceBody(model: model, approval: a, width: inner)
                    .frame(width: inner, height: IslandChoiceLayout.rowsViewport(a, textLines: model.choiceTextLines), alignment: .topLeading)
                    .offset(x: p, y: IslandChoiceLayout.rowsY)
                Button { perform(.choiceDismiss(a.id)) } label: {
                    Color.clear.frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Dismiss")
                .offset(x: IslandBarLayout.cardTrailOrigin.x - 12, y: 0)
            } else {
                meta(card.meta, width: inner)
                    .offset(x: p, y: IslandBarLayout.cardMetaY)
                controls(card.controls, width: inner)
                    .frame(width: inner, height: IslandBarLayout.cardControlsHeight)
                    .offset(x: p, y: model.cardControlsY)
            }
        }
        .frame(width: IslandBarLayout.cardSize.width, height: model.cardHeight, alignment: .topLeading)
    }

    @ViewBuilder
    private func art(_ art: IslandCardArt) -> some View {
        switch art {
        case .orb:
            SamanthaArtTile(fill: Color.white.opacity(IslandInk.fill)) {
                SamanthaOrb(size: IslandBarLayout.artGlyph, dotSize: 10)
            }
        case .app(let name):
            // Real app icons render at 56 pt with no tile.
            SamanthaAppIcon(appName: name, size: 56, radius: 12)
        case .appDone(let name):
            SamanthaAppIcon(appName: name, size: 56, radius: 12)
                .overlay(alignment: .bottomTrailing) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(IslandPalette.green)
                        .background(Circle().fill(Color.black).padding(1))
                        .offset(x: 4, y: 4)
                }
        case .tile(let symbol, let tint):
            let color = tint == "red" ? IslandPalette.red : IslandPalette.amber
            // Signal tiles: signal colour at 20% fill, glyph in the full signal colour. No solid fills.
            SamanthaArtTile(fill: color.opacity(0.2)) {
                Image(systemName: symbol)
                    .font(.system(size: IslandBarLayout.artGlyph, weight: .semibold))
                    .foregroundStyle(color)
                    .frame(width: IslandBarLayout.artGlyph, height: IslandBarLayout.artGlyph)
            }
        case .agent(let app):
            // Tinted tile (amber 20 %) holding the source icon at the 30 pt glyph size; amber pulse in the last 60 s.
            SamanthaArtTile(fill: IslandPalette.amber.opacity(0.2)) {
                SamanthaAppIcon(appName: app, size: IslandBarLayout.artGlyph, radius: 7)
            }
            .overlay { if model.cardRemaining != nil { IslandPulseBorder(reduceMotion: reduceMotion) } }
        case .expiredRing:
            SamanthaArtTile(fill: IslandPalette.red.opacity(0.12)) {
                Circle().strokeBorder(IslandPalette.red, lineWidth: 3)
                    .frame(width: IslandBarLayout.artGlyph, height: IslandBarLayout.artGlyph)
            }
        case .artwork:
            NowPlayingArtwork(info: model.nowPlaying, size: 56, flip: model.mediaStyle.artworkFlip && !reduceMotion)
        }
    }

    @ViewBuilder
    private func meta(_ meta: IslandCardMeta, width: CGFloat) -> some View {
        switch meta {
        case .none:
            EmptyView()
        case .progress(let f, let label):
            VStack(alignment: .trailing, spacing: 4) {
                SamanthaProgressBar(fraction: f, width: width)
                Text(label)
                    .font(.system(size: 11, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(IslandInk.metaColor)
            }
            .frame(width: width, alignment: .trailing)
        case .micMeter:
            SamanthaMeter(levels: model.meter24, color: Color.white).frame(width: width, height: 16)
        case .voiceMeter:
            SamanthaMeter(levels: model.meter24, color: IslandPalette.accent).frame(width: width, height: 16)
        }
    }

    @ViewBuilder
    private func controls(_ c: IslandCardControl, width: CGFloat) -> some View {
        switch c {
        case .none:
            EmptyView()
        case .icons(let items):
            HStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    Spacer(minLength: 0)
                    Button { perform(item.action) } label: {
                        Image(systemName: item.symbol)
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Color.white)
                            .frame(width: 44, height: 32)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .islandFocusRing(model.isNavFocused(.action(item.action)), shape: .capsule)
                    .accessibilityLabel(item.label)
                    Spacer(minLength: 0)
                }
            }
        case .choice:
            EmptyView()   // drawn by IslandChoiceBody in cardContent
        case .decision(let secondary, let primary, let destructive):
            let missingScope = {
                if case .agentApprove(let id) = primary.1 {
                    return model.agentApproval?.id == id && model.destructiveApprovalMissingScope
                }
                return false
            }()
            HStack(spacing: IslandBarLayout.decisionGap) {
                Button { perform(secondary.1) } label: {
                    HStack(spacing: 5) {
                        Text(secondary.0)
                        if destructive { Text("↩").font(.system(size: 11)).foregroundStyle(IslandInk.metaColor) }
                    }
                }
                    .buttonStyle(SamanthaDecisionStyle(role: .secondary))
                    .frame(height: 44).contentShape(Rectangle())
                    .layoutProbe("decision-secondary", .control)
                    .islandFocusRing(model.isNavFocused(.action(secondary.1)), shape: .capsule)
                Button { perform(primary.1) } label: {
                    HStack(spacing: 5) {
                        Text(primary.0)
                        Text(destructive ? "⌘↩" : "↩").font(.system(size: 11))
                            .foregroundStyle(destructive ? IslandInk.metaColor : Color.black.opacity(IslandInk.onWhiteSecondary))
                    }
                }
                    .buttonStyle(SamanthaDecisionStyle(role: destructive ? .destructive : .primary))
                    .frame(height: 44).contentShape(Rectangle())
                    .layoutProbe("decision-primary", .control)
                    .islandFocusRing(model.isNavFocused(.action(primary.1)), shape: .capsule)
                    .disabled(missingScope)
                    .opacity(missingScope ? 0.55 : 1)
            }
        }
    }

    private func perform(_ action: IslandCardAction) { model.performCardAction(action) }

    // MARK: Agent list card (§6, multi-agent)

    private var agentList: some View {
        IslandAgentListBody(model: model, reduceMotion: reduceMotion)
    }

    @ViewBuilder
    private func statusGlyph(_ a: IslandAgent) -> some View {
        if a.status == "expired" {
            IslandHollowRing(size: 12).frame(width: 16, height: 16)
        } else if a.needs_you || a.status == "needs_you" {
            SamanthaSymbol(name: "hand.raised.fill", color: IslandPalette.amber)
        } else if a.status == "error" {
            SamanthaSymbol(name: "xmark.circle.fill", color: IslandPalette.red)
        } else if a.status == "done" {
            SamanthaSymbol(name: "checkmark.circle.fill", color: IslandPalette.green)
        } else {
            SamanthaProgressRing(fraction: nil, reduceMotion: reduceMotion).frame(width: 16, height: 16)
        }
    }

    nonisolated static func elapsed(_ s: Double) -> String {
        let n = max(0, Int(s))
        return n < 60 ? "\(n)s" : n < 3600 ? "\(n / 60)m" : "\(n / 3600)h"
    }
}

/// 24-bar meter, 16 pt tall, centred bars.
struct SamanthaMeter: View {
    let levels: [Double]
    let color: Color
    var body: some View {
        Canvas { ctx, size in
            let n = max(1, levels.count)
            let slot = size.width / CGFloat(n)
            let w = max(2, slot * 0.4)
            for (i, v) in levels.enumerated() {
                let h = max(2, CGFloat(max(0, min(1, v))) * size.height)
                let rect = CGRect(x: CGFloat(i) * slot + (slot - w) / 2, y: (size.height - h) / 2, width: w, height: h)
                ctx.fill(Path(roundedRect: rect, cornerRadius: w / 2), with: .color(color))
            }
        }
    }
}

private struct SamanthaDecisionStyle: ButtonStyle {
    enum Role { case primary, secondary, destructive }
    let role: Role

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(role == .primary ? Color.black : Color.white)
            .frame(width: IslandBarLayout.decisionButton.width, height: IslandBarLayout.decisionButton.height)
            .background(Capsule(style: .continuous).fill(fill))
            .opacity(configuration.isPressed ? 0.75 : 1)
            .contentShape(Capsule())
    }

    private var fill: Color {
        switch role {
        case .primary: return .white
        case .secondary: return Color.white.opacity(IslandInk.fill)
        case .destructive: return IslandPalette.destructiveFill
        }
    }
}

// MARK: - Choice card body (rows from y80: ≤ 4 option slots, 2 per row, then the footer; never scrolls)

struct IslandChoiceBody: View {
    @ObservedObject var model: IslandModel
    let approval: IslandAgentApproval
    let width: CGFloat
    @FocusState private var fieldFocused: Bool

    private enum Slot: Hashable { case option(IslandChoiceOption, Int), more }

    private var slots: [Slot] {
        IslandChoiceLayout.shownOptions(approval).enumerated().map { Slot.option($1, $0 + 1) }
            + (IslandChoiceLayout.hasMore(approval) ? [.more] : [])
    }

    private var rows: [[Slot]] {
        let s = slots
        return stride(from: 0, to: s.count, by: 2).map { Array(s[$0..<min($0 + 2, s.count)]) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: IslandChoiceLayout.rowGap) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(spacing: width - 2 * IslandChoiceLayout.optionWidth) {
                    ForEach(row, id: \.self) { slot in slotView(slot) }
                }
            }
            if IslandChoiceLayout.hasFooter(approval) { footer }
        }
        .frame(width: width, alignment: .topLeading)
        .onChange(of: model.fieldFocusRequested) { v in
            if v { DispatchQueue.main.async { fieldFocused = true } } else { fieldFocused = false }
        }
        .onAppear { if model.fieldFocusRequested { DispatchQueue.main.async { fieldFocused = true } } }
    }

    @ViewBuilder
    private func slotView(_ slot: Slot) -> some View {
        switch slot {
        case .option(let o, let n): option(o, number: n)
        case .more:
            Button { AppIconResolver.activate(approval.app) } label: {
                HStack(spacing: 4) {
                    Text("More in chat").lineLimit(1)
                    Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold))
                }
            }
            .buttonStyle(IslandChoiceCapsule(role: .ghost, selected: false, width: IslandChoiceLayout.optionWidth))
            .accessibilityLabel("\(approval.options.count - 3) more options in chat")
        }
    }

    private func option(_ o: IslandChoiceOption, number: Int) -> some View {
        let isSel = model.choiceSelected.contains(o.value)
        let isDanger = !approval.danger.isEmpty && o.value == approval.danger
        // Danger stays neutral until selected; primary is only a single-select emphasis.
        let role: IslandChoiceCapsule.Role = isDanger ? (isSel ? .danger : .secondary)
            : (o.value == approval.primary && !approval.multi ? .primary : .secondary)
        let confirming = isDanger && isSel && model.confirmPending && !approval.multi
        return Button { model.tapOption(o.value) } label: {
            HStack(spacing: 6) {
                if approval.multi {
                    Image(systemName: isSel ? "checkmark.square.fill" : "square")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(isSel ? (isDanger ? Color.white : IslandPalette.accent) : IslandInk.metaColor)
                } else if model.textEntryActive && model.choiceText.isEmpty {
                    Text("\(number)").font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundStyle(role == .primary ? Color.black.opacity(IslandInk.onWhiteSecondary) : IslandInk.metaColor)
                }
                Text(confirming ? "Confirm \(o.label)" : o.label).lineLimit(1).truncationMode(.tail)
            }
            .padding(.horizontal, 10)
        }
        .buttonStyle(IslandChoiceCapsule(role: role, selected: isSel && role == .secondary, width: IslandChoiceLayout.optionWidth))
        .accessibilityLabel(o.label + (isSel ? ", selected" : ""))
    }

    @ViewBuilder
    private var footer: some View {
        let lines = model.choiceTextLines
        let h = approval.allow_text ? IslandChoiceLayout.footerHeight(lines: lines) : IslandChoiceLayout.rowHeight
        HStack(alignment: .bottom, spacing: 8) {
            if approval.allow_text {
                field(height: h)
                if approval.multi { submitButton(width: 96) } else { sendButton }
            } else {
                submitButton(width: width)
            }
        }
        .frame(width: width, height: h, alignment: .bottomLeading)
    }

    @ViewBuilder
    private func field(height: CGFloat) -> some View {
        let shape = RoundedRectangle(cornerRadius: IslandChoiceLayout.fieldRadius, style: .continuous)
        if model.textEntryActive {
            TextField("Reply…", text: $model.choiceText, axis: .vertical)
                .lineLimit(1...3)
                .textFieldStyle(.plain)
                .font(.system(size: 13))
                .foregroundStyle(Color.white)
                .focused($fieldFocused)
                .onSubmit { model.submitChoice(via: "key") }
                .padding(.horizontal, 12)
                .frame(maxWidth: .infinity, minHeight: height, maxHeight: height, alignment: .leading)
                .background(shape.fill(Color.white.opacity(IslandInk.fill)))
                .overlay(shape.strokeBorder(fieldFocused ? IslandPalette.accent.opacity(0.6) : .clear, lineWidth: 1))
        } else {
            // A click here (or the click that opened the card) is the ONLY way the panel becomes key.
            Button { model.beginTextEntry() } label: {
                Text(model.choiceText.isEmpty ? "Reply…" : model.choiceText)
                    .font(.system(size: 13))
                    .foregroundStyle(model.choiceText.isEmpty ? IslandInk.metaColor : Color.white)
                    .lineLimit(3)
                    .padding(.horizontal, 12)
                    .frame(maxWidth: .infinity, minHeight: height, maxHeight: height, alignment: .leading)
                    .background(shape.fill(Color.white.opacity(IslandInk.fill)))
                    .contentShape(shape)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Reply")
        }
    }

    /// 32×32 accent send; inert (dim, disabled) while there is nothing to send.
    private var sendButton: some View {
        let can = model.choiceCanSend
        let red = model.choiceIncludesDanger && model.confirmPending
        return Button { model.submitChoice() } label: {
            Image(systemName: "arrow.up")
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(can ? Color.black : IslandInk.metaColor)
                .frame(width: IslandChoiceLayout.sendSize, height: IslandChoiceLayout.sendSize)
                .background(Circle().fill(can ? (red ? IslandPalette.destructiveFill : IslandPalette.accent) : Color.white.opacity(IslandInk.fill)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .frame(width: 44, height: 44).contentShape(Rectangle())
        .disabled(!can)
        .accessibilityLabel("Send")
    }

    /// Multi-select: "Submit N"; with the danger option selected the first press flips to Confirm.
    private func submitButton(width w: CGFloat) -> some View {
        let n = model.choiceSelected.count
        let flip = model.choiceIncludesDanger && model.confirmPending
        let dangerLabel = approval.options.first { $0.value == approval.danger }?.label ?? ""
        let wide = w > 120
        let label: String = {
            if flip { return wide ? "Confirm — includes \(dangerLabel)" : "Confirm" }
            if n == 0 { return approval.multi ? (wide ? "Select options" : "Submit") : "Send" }
            return wide ? "Submit \(n) selected" : "Submit \(n)"
        }()
        return Button(label) { model.submitChoice() }
            .buttonStyle(IslandChoiceCapsule(role: flip ? .danger : .primary, selected: false, width: w))
            .disabled(!model.choiceCanSend)
    }
}

struct IslandChoiceCapsule: ButtonStyle {
    enum Role { case primary, secondary, danger, ghost }
    let role: Role
    let selected: Bool
    let width: CGFloat
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(role == .primary ? Color.black : role == .ghost ? IslandInk.metaColor : Color.white)
            .frame(width: width, height: IslandChoiceLayout.visualRowHeight)
            .background(Capsule(style: .continuous).fill(fill))
            .overlay(Capsule(style: .continuous).strokeBorder(stroke, lineWidth: selected ? 1.5 : 1))
            .opacity(!enabled ? 0.4 : configuration.isPressed ? 0.75 : 1)
            .frame(width: width, height: IslandChoiceLayout.rowHeight)
            .contentShape(Rectangle())
    }

    private var stroke: Color {
        if selected { return IslandPalette.accent }
        return role == .ghost ? IslandPalette.keyLine : .clear
    }

    private var fill: Color {
        switch role {
        case .primary: return .white
        case .secondary: return selected ? IslandPalette.accent.opacity(0.25) : Color.white.opacity(IslandInk.fill)
        case .danger: return IslandPalette.destructiveFill
        case .ghost: return .clear
        }
    }
}

// MARK: - Now Playing views

enum NowPlayingTint {
    /// Progress/waveform colour per Settings → Now Playing → Progress colour.
    @MainActor static func primary(_ m: IslandModel) -> Color {
        guard m.mediaStyle.color != "mono", let t = m.nowPlaying?.tints.first else { return .white }
        return Color(nsColor: t)
    }

    /// Compact waveform: dominant artwork colour with a 60 % luminance floor (white when Monochrome / no art).
    @MainActor static func wave(_ m: IslandModel) -> Color {
        guard m.mediaStyle.color != "mono", let t = m.nowPlaying?.waveTint else { return .white }
        return Color(nsColor: t)
    }

    @MainActor static func fill(_ m: IslandModel) -> AnyShapeStyle {
        let tints = m.nowPlaying?.tints ?? []
        switch m.mediaStyle.color {
        case "color" where !tints.isEmpty: return AnyShapeStyle(Color(nsColor: tints[0]))
        case "gradient" where tints.count >= 2:
            return AnyShapeStyle(LinearGradient(colors: [Color(nsColor: tints[0]), Color(nsColor: tints[1])],
                                                startPoint: .leading, endPoint: .trailing))
        default: return AnyShapeStyle(Color.white)
        }
    }
}

/// Album art; 3D flip when the track changes (Settings: Disable artwork flip).
struct NowPlayingArtwork: View {
    let info: NowPlayingInfo?
    let size: CGFloat
    let flip: Bool
    @State private var angle: Double = 0

    var body: some View {
        let r = size >= 40 ? 12 : size / 4
        Group {
            if let img = info?.artwork {
                Image(nsImage: img).resizable().aspectRatio(contentMode: .fill)
            } else {
                ZStack {
                    Color.white.opacity(IslandInk.fill)
                    Image(systemName: "music.note").font(.system(size: size * 0.5, weight: .semibold))
                        .foregroundStyle(IslandInk.secondaryColor)
                }
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: r, style: .continuous))
        .rotation3DEffect(.degrees(angle), axis: (x: 0, y: 1, z: 0))
        .onChange(of: info?.trackKey ?? "") { _ in
            guard flip else { return }
            withAnimation(.spring(response: 0.5, dampingFraction: 0.8)) { angle += 360 }
        }
        .accessibilityHidden(true)
    }
}

/// Synthetic animated waveform (live audio waveform is "Soon"); flat when paused.
struct NowPlayingWaveform: View {
    let playing: Bool
    let color: Color
    let reduceMotion: Bool

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 20, paused: !playing || reduceMotion)) { ctx in
            Canvas { g, size in
                let t = ctx.date.timeIntervalSinceReferenceDate
                // 4 bars × 3 pt, 2 pt gap (18 pt), centred in the 20 pt slot.
                let n = 4
                let w: CGFloat = 3
                let gap: CGFloat = 2
                let x0 = (size.width - (CGFloat(n) * w + CGFloat(n - 1) * gap)) / 2
                for i in 0..<n {
                    let phase = Double(i) * 1.3
                    let v = playing ? (reduceMotion ? 0.6 : 0.35 + 0.65 * abs(sin(t * (3.1 + Double(i) * 0.7) + phase))) : 0.15
                    let h = max(2, CGFloat(v) * size.height)
                    let rect = CGRect(x: x0 + CGFloat(i) * (w + gap), y: (size.height - h) / 2, width: w, height: h)
                    g.fill(Path(roundedRect: rect, cornerRadius: w / 2), with: .color(color))
                }
            }
        }
        .accessibilityHidden(true)
    }
}

/// Card: title/artist at x84 (15/13 pt like the choice card) · flat 4 pt scrub (20 pt hit, knob on hover)
/// at y88 · three centred transport controls (22 pt, 44 pt hits); the action slot only when available.
struct NowPlayingCardBody: View {
    @ObservedObject var model: IslandModel
    @State private var dragFraction: Double?
    @State private var scrubHover = false

    var body: some View {
        let p = IslandBarLayout.cardPadding
        let inner = IslandBarLayout.cardSize.width - 2 * p
        let info = model.nowPlaying ?? NowPlayingInfo()
        ZStack(alignment: .topLeading) {
            VStack(alignment: .leading, spacing: 4) {
                Text(info.title)
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(Color.white).lineLimit(1)
                    .frame(width: IslandBarLayout.cardTrailOrigin.x - IslandBarLayout.cardTextX - 8, alignment: .leading)
                Text(info.artist.isEmpty ? info.album : info.artist)
                    .font(.system(size: 13)).foregroundStyle(IslandInk.secondaryColor).lineLimit(1)
                    .frame(width: IslandBarLayout.cardSize.width - IslandBarLayout.cardTextX - p, alignment: .leading)
            }
            .offset(x: IslandBarLayout.cardTextX, y: p)
            scrubber(info, width: inner).offset(x: p, y: IslandBarLayout.cardMetaY - 8)
            controls(info, width: inner)
                .frame(width: inner, height: 44)
                .offset(x: p, y: IslandBarLayout.cardControlsY - 6)
        }
        .frame(width: IslandBarLayout.cardSize.width, height: IslandBarLayout.cardSize.height, alignment: .topLeading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Now playing: \(info.title), \(info.artist)")
    }

    static let scrubHit: CGFloat = 20
    static let scrubTrack: CGFloat = 4

    private func scrubber(_ info: NowPlayingInfo, width: CGFloat) -> some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { ctx in
            let elapsed = info.elapsed(at: ctx.date)
            let dur = max(info.duration, 0)
            let f = min(1, max(0, dragFraction ?? (dur > 0 ? elapsed / dur : 0)))
            let shown = dragFraction.map { $0 * dur } ?? elapsed
            VStack(spacing: 0) {
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2).fill(IslandPalette.track).frame(width: width, height: Self.scrubTrack)
                    RoundedRectangle(cornerRadius: 2).fill(NowPlayingTint.fill(model))
                        .frame(width: max(Self.scrubTrack, width * CGFloat(f)), height: Self.scrubTrack)
                    if scrubHover || dragFraction != nil {
                        Circle().fill(Color.white).frame(width: 10, height: 10)
                            .offset(x: min(width - 10, max(0, width * CGFloat(f) - 5)))
                            .transition(.opacity)
                    }
                }
                .frame(width: width, height: Self.scrubHit)
                .contentShape(Rectangle())
                .onHover { h in withAnimation(.easeOut(duration: 0.12)) { scrubHover = h } }
                .gesture(DragGesture(minimumDistance: 0)
                    .onChanged { v in if dur > 0 { dragFraction = min(1, max(0, Double(v.location.x / width))) } }
                    .onEnded { _ in
                        if let d = dragFraction, dur > 0 { model.onMedia?(.seek(d * dur)) }
                        dragFraction = nil
                    })
                HStack {
                    Text(NowPlayingRules.clock(shown))
                    Spacer()
                    Text(dur > 0 ? "-" + NowPlayingRules.clock(max(0, dur - shown)) : "")
                }
                .font(.system(size: 11, weight: .regular, design: .monospaced))
                .foregroundStyle(IslandInk.metaColor)
                .frame(width: width)
            }
        }
        .accessibilityLabel("Playback position")
    }

    /// Action slot per Settings (Like / Repeat / Copy) — hidden when the source can't do it.
    static func actionItem(_ info: NowPlayingInfo, action: String) -> (symbol: String, label: String, on: Bool, id: String)? {
        switch action {
        case "repeat": return (info.repeatMode == 2 ? "repeat.1" : "repeat", "Repeat", info.repeatOn, "repeat")
        case "copy": return ("link", "Copy title", false, "copy")
        default:   // "favorite" (and the cut "shuffle")
            return info.likeAvailable ? (info.isLiked ? "star.fill" : "star", "Like", info.isLiked, "favorite") : nil
        }
    }

    private func controls(_ info: NowPlayingInfo, width: CGFloat) -> some View {
        ZStack {
            HStack(spacing: 20) {
                button("backward.fill", "Previous") { model.onMedia?(.previous) }
                button(info.playing ? "pause.fill" : "play.fill", info.playing ? "Pause" : "Play") { model.onMedia?(.togglePlayPause) }
                button("forward.fill", "Next") { model.onMedia?(.next) }
            }
            .frame(width: width)
            if let a = Self.actionItem(info, action: model.mediaStyle.action) {
                HStack {
                    button(a.symbol, a.label, on: a.on, size: 16) {
                        switch a.id {
                        case "repeat": model.onMedia?(.toggleRepeat)
                        default: model.onMediaAction?(a.id)
                        }
                    }
                    Spacer()
                }
                .frame(width: width)
            }
        }
    }

    private func button(_ symbol: String, _ label: String, on: Bool = false, size: CGFloat = 22,
                        _ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            Image(systemName: symbol)
                .font(.system(size: size, weight: .semibold))
                .foregroundStyle(on ? IslandPalette.accent : Color.white)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// Probes the visible trail only (the hidden twin of the morph keeps opacity 0).
private struct TrailProbe: ViewModifier {
    let on: Bool, id: String
    func body(content: Content) -> some View {
        if on { content.layoutProbe(id, .glyph) } else { content }
    }
}
