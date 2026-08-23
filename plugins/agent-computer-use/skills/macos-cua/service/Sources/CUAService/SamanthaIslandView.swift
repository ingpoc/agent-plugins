import AppKit
import SwiftUI

struct IslandSnapshot: Codable, Equatable {
    var kind: String = "idle"
    var title: String = ""
    var detail: String = ""
    var app: String = ""
    var step: String = ""
    var confirm_id: String = ""
    var confirm_prompt: String = ""
    var active_apps: [String] = []
    var voice_side: String = "idle"
    var voice_level: Double = 0
    var voice_levels: [Double] = []
}

enum SamanthaIslandPresentation {
    case menuBarCenter
    case notchCompactLeading
    case notchCompactTrailing
    case notchExpanded
}

@MainActor
final class IslandModel: ObservableObject {
    @Published var kind: String = "idle"
    @Published var title: String = "Samantha"
    @Published var detail: String = ""
    @Published var activeApps: [String] = []
    @Published var expanded: Bool = false
    var resolveConfirm: ((Bool) -> Void)?

    func apply(_ snap: IslandSnapshot) {
        kind = snap.kind
        title = snap.title.isEmpty ? defaultTitle(for: snap.kind) : snap.title
        if snap.kind == "confirm", !snap.confirm_prompt.isEmpty {
            detail = snap.confirm_prompt
        } else {
            detail = snap.detail
        }
        activeApps = snap.active_apps.isEmpty && !snap.app.isEmpty ? [snap.app] : snap.active_apps
        expanded = ["confirm", "secrets", "error"].contains(snap.kind)
    }

    var statusLine: String {
        if !detail.isEmpty, expanded { return detail }
        return title.isEmpty ? defaultTitle(for: kind) : title
    }

    var displayTitle: String {
        title.isEmpty ? defaultTitle(for: kind) : title
    }

    private func defaultTitle(for kind: String) -> String {
        switch kind {
        case "listening": return "Listening"
        case "thinking": return "Thinking"
        case "speaking": return "Samantha"
        case "acting", "driving": return "Working"
        case "secrets": return "Keychain"
        case "confirm": return "Confirm"
        case "error": return "Error"
        case "done": return "Done"
        default: return "Samantha"
        }
    }
}

struct SamanthaAppChip: View {
    let appName: String
    var size: CGFloat = 22

    var body: some View {
        ZStack {
            Circle()
                .fill(Color.black.opacity(0.88))
                .overlay(Circle().strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5))
            appIcon
                .frame(width: size * 0.64, height: size * 0.64)
        }
        .frame(width: size, height: size)
        .help(appName)
    }

    @ViewBuilder
    private var appIcon: some View {
        if let image = AppIconResolver.icon(for: appName) {
            Image(nsImage: image)
                .resizable()
                .scaledToFill()
                .clipShape(Circle())
        } else {
            Image(systemName: AppIconResolver.symbol(for: appName))
                .font(.system(size: size * 0.42, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))
        }
    }
}

enum AppIconResolver {
    static func icon(for appName: String) -> NSImage? {
        let ws = NSWorkspace.shared
        if let url = ws.urlForApplication(withBundleIdentifier: bundleId(for: appName)) {
            return ws.icon(forFile: url.path)
        }
        for candidate in appCandidates(appName) {
            if let url = ws.urlForApplication(withBundleIdentifier: candidate) {
                return ws.icon(forFile: url.path)
            }
            for base in ["/System/Applications", "/Applications"] {
                let path = "\(base)/\(candidate).app"
                if FileManager.default.fileExists(atPath: path) {
                    return ws.icon(forFile: path)
                }
            }
        }
        return nil
    }

    static func symbol(for appName: String) -> String {
        let lower = appName.lowercased()
        if lower.contains("music") { return "music.note" }
        if lower.contains("keychain") || lower.contains("secret") { return "key.fill" }
        if lower.contains("calculator") { return "function" }
        if lower.contains("settings") || lower.contains("system") { return "gearshape.fill" }
        if lower.contains("finder") { return "face.smiling" }
        if lower.contains("safari") { return "safari.fill" }
        if lower.contains("cursor") { return "chevron.left.forwardslash.chevron.right" }
        return "app.fill"
    }

    private static func bundleId(for appName: String) -> String {
        switch appName.lowercased() {
        case "music": return "com.apple.Music"
        case "calculator": return "com.apple.calculator"
        case "finder": return "com.apple.finder"
        case "system settings": return "com.apple.systempreferences"
        case "keychain access": return "com.apple.keychainaccess"
        default: return appName
        }
    }

    private static func appCandidates(_ appName: String) -> [String] {
        [appName, appName.replacingOccurrences(of: " ", with: "")]
    }
}

struct SamanthaIslandBar: View {
    @ObservedObject var model: IslandModel
    var presentation: SamanthaIslandPresentation = .menuBarCenter
    var barHeight: CGFloat = SamanthaMenuBarIslandPanel.menuBarContentHeight(
        on: SamanthaMenuBarIslandPanel.menuBarScreen()
    )

    var body: some View {
        switch presentation {
        case .menuBarCenter: menuBarCenterBar
        case .notchCompactLeading: notchLeading
        case .notchCompactTrailing: notchTrailing
        case .notchExpanded: notchExpandedCard
        }
    }

    private var menuBarCenterBar: some View {
        HStack(spacing: 6) {
            statusPill
            if !model.activeApps.isEmpty {
                appChipRow(max: 3, size: menuBarChipSize, overlap: false)
            }
        }
        .fixedSize(horizontal: true, vertical: true)
    }

    /// Status text + indicator — separate from per-app circular pills.
    private var statusPill: some View {
        Group {
            if model.kind == "confirm" {
                HStack(spacing: 8) {
                    leadingIndicator(compact: true)
                    confirmControls(compact: true)
                }
            } else {
                HStack(spacing: 8) {
                    leadingIndicator(compact: true)
                    Text(model.statusLine)
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                }
            }
        }
        .padding(.horizontal, 10)
        .frame(height: barHeight)
        .background(islandCapsule)
    }

    /// Full menu-bar strip height — app chips are standalone circles, not inset in the status pill.
    private var menuBarChipSize: CGFloat {
        max(16, barHeight - 2)
    }

    private var notchLeading: some View {
        HStack(spacing: 6) {
            leadingIndicator(compact: true)
            Text(model.statusLine)
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)
                .lineLimit(1)
        }
    }

    private var notchTrailing: some View {
        appChipRow(max: 2, size: 20, overlap: false)
    }

    private var notchExpandedCard: some View {
        HStack(spacing: 8) {
            HStack(spacing: 10) {
                leadingIndicator(compact: false)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.displayTitle)
                        .font(.system(size: 13, weight: .semibold, design: .rounded))
                        .foregroundStyle(.white)
                    if !model.detail.isEmpty {
                        Text(model.detail)
                            .font(.system(size: 11, weight: .regular, design: .rounded))
                            .foregroundStyle(.white.opacity(0.65))
                            .lineLimit(2)
                    }
                    if model.kind == "confirm" {
                        confirmControls(compact: false)
                            .padding(.top, 4)
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(minWidth: 220, maxWidth: 320)
            .background(islandCapsule)
            if !model.activeApps.isEmpty {
                appChipRow(max: 4, size: 24, overlap: false)
            }
        }
    }

    @ViewBuilder
    private func leadingIndicator(compact: Bool = false) -> some View {
        let dot = compact ? max(16, barHeight - 2) : 22.0
        ZStack {
            Circle()
                .fill(statusColor.opacity(0.22))
                .frame(width: dot, height: dot)
            Image(systemName: statusSymbol)
                .font(.system(size: compact ? max(9, dot * 0.38) : 10, weight: .bold))
                .foregroundStyle(statusColor)
        }
    }

    @ViewBuilder
    private func confirmControls(compact: Bool) -> some View {
        let prompt = model.detail.isEmpty ? model.title : model.detail
        VStack(alignment: .leading, spacing: compact ? 4 : 6) {
            if compact {
                Text(prompt)
                    .font(.system(size: 11, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.85))
                    .lineLimit(1)
                    .frame(maxWidth: 140, alignment: .leading)
            }
            HStack(spacing: 6) {
                Button("Allow") { model.resolveConfirm?(true) }
                    .buttonStyle(SamanthaConfirmButtonStyle(tone: .allow, compact: compact))
                Button("Deny") { model.resolveConfirm?(false) }
                    .buttonStyle(SamanthaConfirmButtonStyle(tone: .deny, compact: compact))
            }
        }
    }

    @ViewBuilder
    private func appChipRow(max: Int, size: CGFloat, overlap: Bool) -> some View {
        HStack(spacing: overlap ? -4 : 5) {
            ForEach(Array(model.activeApps.prefix(max).enumerated()), id: \.offset) { _, app in
                SamanthaAppChip(appName: app, size: size)
            }
        }
    }

    private var islandCapsule: some View {
        Capsule(style: .continuous)
            .fill(Color.black.opacity(0.88))
            .overlay(
                Capsule(style: .continuous)
                    .strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5)
            )
    }

    private var statusSymbol: String {
        switch model.kind {
        case "listening": return "mic.fill"
        case "thinking": return "ellipsis"
        case "acting", "driving": return "hand.tap.fill"
        case "secrets": return "key.fill"
        case "confirm": return "exclamationmark.triangle.fill"
        case "error": return "xmark"
        case "done": return "checkmark"
        case "speaking": return "speaker.wave.2.fill"
        default: return "mic.fill"
        }
    }

    private var statusColor: Color {
        switch model.kind {
        case "listening": return .green
        case "thinking": return .orange
        case "acting", "driving": return .cyan
        case "secrets": return .purple
        case "confirm": return .yellow
        case "error": return .red
        case "done": return .green
        case "speaking": return .pink
        default: return .white
        }
    }
}

struct SamanthaIslandExpandedView: View {
    @ObservedObject var model: IslandModel
    var body: some View {
        SamanthaIslandBar(model: model, presentation: .notchExpanded)
    }
}

struct SamanthaIslandCompactLeading: View {
    @ObservedObject var model: IslandModel
    var body: some View {
        SamanthaIslandBar(model: model, presentation: .notchCompactLeading)
    }
}

struct SamanthaIslandCompactTrailing: View {
    @ObservedObject var model: IslandModel
    var body: some View {
        SamanthaIslandBar(model: model, presentation: .notchCompactTrailing)
    }
}

private struct SamanthaConfirmButtonStyle: ButtonStyle {
    enum Tone { case allow, deny }

    let tone: Tone
    let compact: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: compact ? 10 : 11, weight: .semibold, design: .rounded))
            .padding(.horizontal, compact ? 8 : 10)
            .padding(.vertical, compact ? 3 : 4)
            .background(
                Capsule(style: .continuous)
                    .fill(background.opacity(configuration.isPressed ? 0.75 : 1))
            )
            .foregroundStyle(.white)
    }

    private var background: Color {
        switch tone {
        case .allow: return Color.green.opacity(0.85)
        case .deny: return Color.red.opacity(0.75)
        }
    }
}
