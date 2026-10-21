import AppKit
import SwiftUI

/// First-run / something-missing onboarding: Welcome, then one row per consent — the same rows as
/// ACU Settings › Computer Use (shared defs, SettingRow, live status, Grant = native request or deep link).
@MainActor
final class OnboardingModel: ObservableObject {
    enum Step { case welcome, permissions }
    @Published var step: Step
    init(step: Step = .welcome) { self.step = step }

    static let completedKey = "acu.onboarding.completed"
    /// After launch: only a newly missing required consent re-opens the window (no nagging every poll).
    static func shouldReopen(previous: Set<ACUPermission>, now: Set<ACUPermission>) -> Bool {
        !now.subtracting(previous).isEmpty
    }
    /// First launch, or a required consent is missing now.
    static func shouldShow(defaults: UserDefaults = .standard,
                           missing: [ACUPermission] = PermissionCenter.missingRequired()) -> Bool {
        !defaults.bool(forKey: completedKey) || !missing.isEmpty
    }
}

enum OnboardingStyle {
    /// Same card width as the settings pane (rowWidth) with the pane's 12 pt page padding.
    static let size = CGSize(width: SettingsStyle.rowWidth + 24, height: 640)
}

struct OnboardingView: View {
    @ObservedObject var model: OnboardingModel
    @ObservedObject var store: IslandSettingsStore
    /// Re-renders rows when a refresh lands (rows read PermissionCenter.state via `permissions`).
    @ObservedObject var center: PermissionCenter = .shared
    var rendering = false
    var permissions: (String) -> Bool = IslandSettingsSchema.permissionGranted
    var defs: (SettingsTab) -> [SettingDef] = IslandSettingsSchema.defs(for:)
    var onDone: () -> Void = {}

    private var rows: [SettingDef] { IslandSettingsRoot.visibleDefs(.computerUse, store: store, defs: defs) }
    private var missingRequired: Int {
        rows.filter { d in ACUPermission.from(key: d.key)?.required == true && !permissions(d.key) }.count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if rendering {
                HStack(spacing: 8) {
                    ForEach([Color(red: 1, green: 0.37, blue: 0.34), Color(white: 0.45), Color(white: 0.45)], id: \.self) { c in
                        Circle().fill(c).frame(width: 12, height: 12)
                    }
                }
                .padding(.leading, 18).padding(.top, 18)
            }
            Spacer().frame(height: rendering ? 12 : 40)
            switch model.step {
            case .welcome: welcome
            case .permissions: permissionsStep
            }
        }
        .frame(width: OnboardingStyle.size.width, height: rendering ? nil : OnboardingStyle.size.height, alignment: .topLeading)
        .frame(minHeight: OnboardingStyle.size.height, alignment: .top)
        .coordinateSpace(name: IslandLayoutRecorder.space)
        .background(SettingsStyle.windowBG)
        .environment(\.colorScheme, .dark)
        .tint(SettingsStyle.accent)
    }

    // MARK: Welcome

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 0) {
            welcomeContent
            Spacer(minLength: 0)
            footer(primary: "Continue") { model.step = .permissions }.padding(.bottom, 8)
        }
        .frame(maxHeight: .infinity, alignment: .top)
    }

    private var welcomeContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            IconTile(symbol: SettingsTab.computerUse.symbol, color: Color(nsColor: SettingsTab.computerUse.tint), size: 48)
                .layoutProbe("welcome-icon", .glyph)
                .padding(.leading, 4)
            Text("Welcome to Agent Computer Use")
                .font(.system(size: 20, weight: .semibold)).foregroundStyle(.white)
                .fixedSize(horizontal: false, vertical: true)
                .layoutProbe("welcome-title", truncates: true).padding(.leading, 4)
            Text("Agents see and use your Mac on your behalf. Grant each permission once here, so a task never stops halfway to ask.")
                .font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.7))
                .fixedSize(horizontal: false, vertical: true)
                .layoutProbe("welcome-body", truncates: true).padding(.leading, 4)
            SettingsCard {
                ForEach(Array(Self.points.enumerated()), id: \.offset) { i, p in
                    if i > 0 { Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10) }
                    HStack(spacing: 10) {
                        Image(systemName: p.0).font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Color(nsColor: SettingsTab.computerUse.tint)).frame(width: 18)
                            .layoutProbe("welcome-symbol:\(i)", .glyph)
                        Text(p.1).font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.92))
                            .lineLimit(1).truncationMode(.tail)
                            .layoutProbe("welcome-point:\(i)", truncates: true)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
                    .layoutContainer("welcome|point|\(i)")
                }
            }
            Text("Every prompt is macOS's own. Nothing is granted for you.")
                .font(.system(size: 11)).foregroundStyle(Color.white.opacity(0.45))
                .fixedSize(horizontal: false, vertical: true)
                .layoutProbe("welcome-note", truncates: true).padding(.leading, 4)
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .layoutContainer("onboarding|welcome")
    }

    static let points: [(String, String)] = [
        ("hand.point.up.left", "Read and drive app controls"),
        ("rectangle.dashed", "See windows when controls are unreadable"),
        ("mic", "Hear you when Samantha is on"),
        ("applescript", "Script the apps agents use"),
    ]

    // MARK: Permissions

    private var permissionsStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                IconTile(symbol: SettingsTab.computerUse.symbol, color: Color(nsColor: SettingsTab.computerUse.tint), size: 18)
                    .layoutProbe("pane-icon", .glyph)
                Text("Permissions").font(.system(size: 15, weight: .semibold)).foregroundStyle(.white).layoutProbe("pane-title")
                Spacer(minLength: 0)
                Text(missingRequired == 0 ? "Required: all allowed" : "Required: \(missingRequired) missing")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(missingRequired == 0 ? Color(nsColor: .systemGreen) : Color(nsColor: .systemOrange))
                    .lineLimit(1).fixedSize()
                    .layoutProbe("summary")
            }
            .padding(.horizontal, 12).frame(height: 50, alignment: .leading)
            .layoutContainer("onboarding|header")
            if rendering { page.padding(.horizontal, 12).padding(.bottom, 12) }
            else { ScrollView { page.padding(.horizontal, 12).padding(.bottom, 12) } }
            footer(primary: missingRequired == 0 ? "Done" : "Later") { onDone() }
                .padding(.bottom, 8)
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .layoutContainer("content")
    }

    private var page: some View {
        let groups = IslandSettingsRoot.group(rows)
        return VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(groups.enumerated()), id: \.offset) { gi, g in
                VStack(alignment: .leading, spacing: 6) {
                    if let s = g.section {
                        Text(s).font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.white.opacity(0.55))
                            .lineLimit(1).truncationMode(.tail)
                            .layoutProbe("section:\(gi)", truncates: true).padding(.leading, 4)
                    }
                    SettingsCard {
                        ForEach(Array(g.rows.enumerated()), id: \.element.key) { i, d in
                            if i > 0 { Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10) }
                            SettingRow(def: d, store: store, rendering: rendering, card: "c\(gi)",
                                       granted: permissions(d.key),
                                       onButton: { key in IslandSettingsRoot.perform(key, onShowLog: {}) })
                        }
                    }
                }
            }
            Text("Change these later in ACU Settings › Computer Use.")
                .font(.system(size: 11)).foregroundStyle(Color.white.opacity(0.45))
                .fixedSize(horizontal: false, vertical: true)
                .layoutProbe("permissions-note", truncates: true).padding(.leading, 4)
        }
    }

    private func footer(primary: String, action: @escaping () -> Void) -> some View {
        HStack {
            Spacer(minLength: 0)
            Button(primary, action: action)
                .buttonStyle(PillButtonStyle(primary: true))
                .frame(width: PillButtonStyle.width(primary))
                .keyboardShortcut(.defaultAction)
                .layoutProbe("footer-button", .control)
        }
        .padding(.horizontal, 12)
        .frame(height: 44)
        .layoutContainer("onboarding|footer")
    }
}

/// The onboarding window: shown on first launch and whenever a required consent goes missing.
/// Rows re-check every time the app becomes active (e.g. back from System Settings).
@MainActor
final class OnboardingWindowController: NSWindowController {
    static let shared = OnboardingWindowController()
    let model = OnboardingModel()

    private init() {
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: OnboardingStyle.size),
                              styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: true)
        window.title = "Welcome to Agent Computer Use"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.19, green: 0.19, blue: 0.19, alpha: 1)
        super.init(window: window)
        window.contentView = NSHostingView(rootView: OnboardingView(model: model, store: .shared, onDone: {
            UserDefaults.standard.set(true, forKey: OnboardingModel.completedKey)
            OnboardingWindowController.shared.close()
        }))
        window.setContentSize(OnboardingStyle.size)
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { PermissionCenter.shared.refresh() }
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// Launch check: refresh real status first, then show only if needed.
    static func showIfNeeded() {
        PermissionCenter.shared.refresh {
            guard OnboardingModel.shouldShow() else { return }
            // Returning user with something missing goes straight to the rows.
            if UserDefaults.standard.bool(forKey: OnboardingModel.completedKey) { shared.model.step = .permissions }
            shared.show()
        }
    }

    func show() {
        if window?.isVisible != true { window?.center() }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
