import AppKit

/// Keeps the window Guru granted from in front of him across a TCC round trip.
/// Grant hands focus to a TCC alert / System Settings; this LSUIElement app has no Dock icon, so its
/// window drops behind System Settings and reads as "closed". Screen Recording's "Quit & Reopen"
/// really does terminate + relaunch the app. So: windows never hide on deactivate, a granted
/// permission (or the app becoming active again) brings the window back, and the surface (pane /
/// onboarding) is persisted so a relaunch reopens it.
enum GrantSurface: Equatable {
    case onboarding
    case settings(SettingsTab)

    static let key = "acu.grant.resumeSurface"   // presence = onboarding/grant in progress

    var encoded: String {
        switch self {
        case .onboarding: return "onboarding"
        case .settings(let t): return "settings:" + t.rawValue
        }
    }

    init?(encoded s: String?) {
        guard let s else { return nil }
        if s == "onboarding" { self = .onboarding; return }
        guard s.hasPrefix("settings:"), let t = SettingsTab(rawValue: String(s.dropFirst(9))) else { return nil }
        self = .settings(t)
    }

    static func load(_ d: UserDefaults = .standard) -> GrantSurface? { GrantSurface(encoded: d.string(forKey: key)) }
    func save(_ d: UserDefaults = .standard) { d.set(encoded, forKey: Self.key) }
    static func clear(_ d: UserDefaults = .standard) { d.removeObject(forKey: key) }

    /// Clear once Required is complete and nothing the user is still working through is pending
    /// (Automation targets / Calendars are optional but Guru grants them in the same session).
    static func finished(_ states: [ACUPermission: PermissionState]) -> Bool {
        ACUPermission.allCases.allSatisfy { p in
            let s = states[p] ?? .missing
            return s.satisfied || (!p.required && s == .notRunning)
        }
    }
}

@MainActor
final class GrantResume {
    static let shared = GrantResume()
    private var watch: Timer?
    /// A Grant is in flight: bring the window back when the app re-activates or the grant lands.
    private(set) var pending: ACUPermission?
    var defaults: UserDefaults = .standard
    /// Quit (incl. Screen Recording "Quit & Reopen"): window teardown must not clear the surface.
    var terminating = false

    private init() {
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { GrantResume.shared.becameActive() }
        }
    }

    /// The surface the Grant came from: the key window (onboarding wins when both are open).
    func currentSurface() -> GrantSurface {
        let ob = OnboardingWindowController.shared.window
        if ob?.isVisible == true, ob?.isKeyWindow == true || IslandSettingsWindowController.shared.window?.isVisible != true {
            return .onboarding
        }
        return .settings(IslandSettingsWindowController.shared.nav.tab)
    }

    func begin(_ p: ACUPermission) {
        currentSurface().save(defaults)
        pending = p
        watch?.invalidate()
        var ticks = 0
        // Grants land in another process; poll this one permission briefly (≤ 3 min) and come back.
        watch = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { t in
            MainActor.assumeIsolated {
                ticks += 1
                let s = PermissionProbe.status(p)
                if s.satisfied || ticks >= 180 {
                    t.invalidate()
                    GrantResume.shared.landed(p, granted: s.satisfied)
                }
            }
        }
    }

    private func landed(_ p: ACUPermission, granted: Bool) {
        guard pending == p else { return }
        pending = nil
        PermissionCenter.shared.refresh()
        if granted { bringFront() }
    }

    private func becameActive() {
        PermissionCenter.shared.refresh()
        if pending != nil || GrantSurface.load(defaults) != nil { bringFront(activate: false) }
    }

    /// Re-show the recorded surface (same pane / onboarding rows) without resetting it.
    func bringFront(activate: Bool = true) {
        guard let surface = GrantSurface.load(defaults) else { return }
        switch surface {
        case .onboarding:
            let c = OnboardingWindowController.shared
            c.model.step = .permissions
            activate ? c.show() : c.window?.makeKeyAndOrderFront(nil)
        case .settings(let tab):
            let c = IslandSettingsWindowController.shared
            if activate { c.showSettings(tab: tab) }
            else { c.nav.tab = tab; c.window?.makeKeyAndOrderFront(nil) }
        }
    }

    /// Launch: macOS relaunched us mid-grant (Screen Recording "Quit & Reopen") → reopen there.
    func restoreAtLaunch() {
        guard GrantSurface.load(defaults) != nil else { return }
        PermissionCenter.shared.refresh { [weak self] in
            guard let self else { return }
            if GrantSurface.finished(PermissionCenter.shared.states) { GrantSurface.clear(self.defaults); return }
            self.bringFront()
        }
    }

    /// Every refresh: all done → forget the surface.
    func update(_ states: [ACUPermission: PermissionState]) {
        if pending == nil, GrantSurface.finished(states) { GrantSurface.clear(defaults) }
    }

    /// The user closed the window (Done / Later / close box): stop resurfacing it.
    func userClosed() {
        guard !terminating else { return }
        pending = nil
        watch?.invalidate()
        GrantSurface.clear(defaults)
    }
}
