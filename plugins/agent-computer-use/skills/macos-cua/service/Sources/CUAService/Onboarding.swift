import AppKit
import Combine
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
    /// Welcome hugs its content (window resized per step); no dead space above the footer.
    static let welcomeMaxHeight: CGFloat = 560
}

/// Amber "Required: N missing" beside a pane title; nothing at all when Required is complete.
struct RequiredSummary: View {
    let missing: Int
    var body: some View {
        if missing > 0 {
            Text("Required: \(missing) missing")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color(nsColor: .systemOrange))
                .lineLimit(1).fixedSize()
                .layoutProbe("summary")
        }
    }

    static func count(_ rows: [SettingDef], permissions: (String) -> Bool) -> Int {
        rows.filter { d in ACUPermission.from(key: d.key)?.required == true && !permissions(d.key) }.count
    }
}

/// The permission rows, shared verbatim by onboarding and ACU Settings › Computer Use: same cards,
/// same SettingRow, same live status (observes PermissionCenter, so a refresh re-renders both).
struct PermissionRows: View {
    let rows: [SettingDef]
    @ObservedObject var store: IslandSettingsStore
    @ObservedObject var center: PermissionCenter = .shared
    var rendering = false
    var focus: String? = nil
    var permissions: (String) -> Bool = IslandSettingsSchema.permissionGranted
    var onButton: (String) -> Void = { key in IslandSettingsRoot.perform(key, onShowLog: {}) }

    var body: some View {
        let groups = IslandSettingsRoot.group(rows)
        VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(groups.enumerated()), id: \.offset) { gi, g in
                VStack(alignment: .leading, spacing: 6) {
                    if let s = g.section {
                        Text(s).font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.white.opacity(0.55))
                            .lineLimit(1).truncationMode(.tail).help(s)
                            .layoutProbe("section:\(gi)", truncates: true).padding(.leading, 4)
                    }
                    SettingsCard {
                        ForEach(Array(g.rows.enumerated()), id: \.element.key) { i, d in
                            if i > 0 { Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10) }
                            SettingRow(def: d, store: store, rendering: rendering, card: "c\(gi)", focused: focus == d.key,
                                       granted: permissions(d.key), onButton: onButton)
                        }
                    }
                }
            }
        }
    }
}

struct OnboardingView: View {
    @ObservedObject var model: OnboardingModel
    @ObservedObject var store: IslandSettingsStore
    /// Re-renders the summary/footer when a refresh lands.
    @ObservedObject var center: PermissionCenter = .shared
    var rendering = false
    var permissions: (String) -> Bool = IslandSettingsSchema.permissionGranted
    var defs: (SettingsTab) -> [SettingDef] = IslandSettingsSchema.defs(for:)
    var onDone: () -> Void = {}

    private var rows: [SettingDef] { IslandSettingsRoot.visibleDefs(.computerUse, store: store, defs: defs) }
    private var missingRequired: Int { RequiredSummary.count(rows, permissions: permissions) }

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
        .frame(width: OnboardingStyle.size.width,
               height: rendering || model.step == .welcome ? nil : OnboardingStyle.size.height, alignment: .topLeading)
        .frame(minHeight: model.step == .welcome ? nil : OnboardingStyle.size.height, alignment: .top)
        .coordinateSpace(name: IslandLayoutRecorder.space)
        .background(SettingsStyle.windowBG)
        .environment(\.colorScheme, .dark)
        .tint(SettingsStyle.accent)
    }

    // MARK: Welcome

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 8) {
            welcomeContent
            footer("Continue", primary: true) { model.step = .permissions }.padding(.bottom, 8)
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    private var welcomeContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            BrandTile(size: 48)
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
                            .foregroundStyle(SettingsStyle.accent).frame(width: 18)
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
                BrandTile(size: 18)
                    .layoutProbe("pane-icon", .glyph)
                Text("Permissions").font(.system(size: 15, weight: .semibold)).foregroundStyle(.white).layoutProbe("pane-title")
                Spacer(minLength: 0)
                RequiredSummary(missing: missingRequired)
            }
            .padding(.horizontal, 12).frame(height: 50, alignment: .leading)
            .layoutContainer("onboarding|header")
            if rendering { page.padding(.horizontal, 12).padding(.bottom, 12) }
            else { ScrollView { page.padding(.horizontal, 12).padding(.bottom, 12) } }
            // Required incomplete: only a grey secondary "Later". Complete: accent "Done" on Return.
            Group {
                if missingRequired == 0 { footer("Done", primary: true) { onDone() } }
                else { footer("Later", primary: false) { onDone() } }
            }
            .padding(.bottom, 8)
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .layoutContainer("content")
    }

    private var page: some View {
        VStack(alignment: .leading, spacing: 14) {
            PermissionRows(rows: rows, store: store, rendering: rendering, permissions: permissions)
            Text("Change these later in ACU Settings › Computer Use.")
                .font(.system(size: 11)).foregroundStyle(Color.white.opacity(0.45))
                .fixedSize(horizontal: false, vertical: true)
                .layoutProbe("permissions-note", truncates: true).padding(.leading, 4)
        }
    }

    /// Only a primary (accent) button is bound to Return; the secondary one is grey and unbound.
    private func footer(_ title: String, primary: Bool, action: @escaping () -> Void) -> some View {
        HStack {
            Spacer(minLength: 0)
            if primary {
                Button(title, action: action)
                    .buttonStyle(PillButtonStyle(primary: true))
                    .frame(width: PillButtonStyle.width(title))
                    .keyboardShortcut(.defaultAction)
                    .layoutProbe("footer-button:primary:\(title)", .control)
            } else {
                Button(title, action: action)
                    .buttonStyle(PillButtonStyle(primary: false))
                    .frame(width: PillButtonStyle.width(title))
                    .layoutProbe("footer-button:secondary:\(title)", .control)
            }
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
    private var hosting: NSHostingView<OnboardingView>?
    private var stepWatch: AnyCancellable?

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
        let host = NSHostingView(rootView: OnboardingView(model: model, store: .shared, onDone: {
            UserDefaults.standard.set(true, forKey: OnboardingModel.completedKey)
            OnboardingWindowController.shared.close()
        }))
        hosting = host
        window.contentView = host
        fitToStep()
        // Welcome hugs its content; the permission rows get the full 640 pt (they scroll).
        stepWatch = model.$step.dropFirst().receive(on: DispatchQueue.main).sink { [weak self] _ in self?.fitToStep() }
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { PermissionCenter.shared.refresh() }
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// Content height for the current step, keeping the window's top edge where it is.
    private func fitToStep() {
        guard let window, let hosting else { return }
        hosting.layoutSubtreeIfNeeded()
        let h = model.step == .welcome ? min(hosting.fittingSize.height, OnboardingStyle.welcomeMaxHeight)
                                       : OnboardingStyle.size.height
        var f = window.frameRect(forContentRect: NSRect(origin: .zero, size: CGSize(width: OnboardingStyle.size.width, height: h)))
        f.origin = CGPoint(x: window.frame.minX, y: window.frame.maxY - f.height)
        window.setFrame(f, display: true, animate: window.isVisible)
    }

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
