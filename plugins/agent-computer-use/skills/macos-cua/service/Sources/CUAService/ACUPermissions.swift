import AppKit
import ApplicationServices
import AVFoundation
import CoreGraphics
import EventKit
import Intents
import os
import ServiceManagement

/// Every macOS consent Agent Computer Use needs, with real status and the native way to ask.
/// Status reads never prompt. Never mirrors or fakes a system dialog; never touches TCC.db.
enum ACUPermission: String, CaseIterable, Sendable {
    case accessibility, screenRecording, microphone
    case automationSystemEvents, automationFinder, automationMusic, automationSpotify, automationComet, automationTextEdit
    case calendars, focus, loginItem, keychain

    static let keyPrefix = "cua.permission."
    var key: String { Self.keyPrefix + rawValue }
    static func from(key: String) -> ACUPermission? {
        key.hasPrefix(keyPrefix) ? ACUPermission(rawValue: String(key.dropFirst(keyPrefix.count))) : nil
    }

    /// Agents stop without these (onboarding re-opens and the island warns when one goes missing).
    var required: Bool { self == .accessibility || self == .screenRecording || self == .microphone }

    var bundleID: String? {
        switch self {
        case .automationSystemEvents: return "com.apple.systemevents"
        case .automationFinder: return "com.apple.finder"
        case .automationMusic: return "com.apple.Music"
        case .automationSpotify: return "com.spotify.client"
        case .automationComet: return "ai.perplexity.comet"
        case .automationTextEdit: return "com.apple.TextEdit"
        default: return nil
        }
    }

    var section: String { required ? "Required" : bundleID != nil ? "Automation" : "Optional" }

    var label: String {
        switch self {
        case .accessibility: return "Accessibility"
        case .screenRecording: return "Screen Recording"
        case .microphone: return "Microphone"
        case .automationSystemEvents: return "System Events"
        case .automationFinder: return "Finder"
        case .automationMusic: return "Music"
        case .automationSpotify: return "Spotify"
        case .automationComet: return "Comet"
        case .automationTextEdit: return "TextEdit"
        case .calendars: return "Calendars"
        case .focus: return "Focus"
        case .loginItem: return "Open at Login"
        case .keychain: return "Keychain access"
        }
    }

    var caption: String {
        switch self {
        case .accessibility: return "Reads and drives app controls, clicks and keys."
        case .screenRecording: return "Sees windows when controls are unreadable."
        case .microphone: return "Lets Samantha hear you."
        case .automationSystemEvents: return "Keys, displays and app lists from agent scripts."
        case .automationFinder: return "Reveals and opens files for tasks."
        case .automationMusic: return "Now Playing and favorites on the island."
        case .automationSpotify: return "Now Playing on the island."
        case .automationComet: return "Browser tasks in Comet."
        case .automationTextEdit: return "Text tasks and checks in TextEdit."
        case .calendars: return "Upcoming events on the island."
        case .focus: return "Quiet peeks while a Focus is on."
        case .loginItem: return "Starts the island and agent service when you log in."
        case .keychain: return "Reads the island tokens and API keys without asking again."
        }
    }

    /// System Settings pane for this consent (deep link).
    var paneURL: URL? {
        let base = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
        switch self {
        case .accessibility: return URL(string: base + "Accessibility")
        case .screenRecording: return URL(string: base + "ScreenCapture")
        case .microphone: return URL(string: base + "Microphone")
        case .calendars: return URL(string: base + "Calendars")
        case .focus: return URL(string: base + "Focus")
        case .loginItem: return URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension")
        case .keychain: return nil   // the Keychain's own "Always Allow" prompt is the grant
        default: return URL(string: base + "Automation")
        }
    }
}

enum PermissionState: String, Sendable {
    case granted, missing
    /// Automation target not running: status unknown until it is (Grant launches it hidden, then asks).
    case notRunning
    /// Automation target not installed: nothing to grant, row hidden.
    case notInstalled

    var satisfied: Bool { self == .granted || self == .notInstalled }
}

/// Live status from the real APIs. Read-only: `askUserIfNeeded` is false; nothing here prompts.
enum PermissionProbe {
    static func status(_ p: ACUPermission) -> PermissionState {
        switch p {
        case .accessibility:
            // Accessibility list covers both AX and CGEvent posting (PostEvent).
            return AXIsProcessTrusted() && CGPreflightPostEventAccess() ? .granted : .missing
        case .screenRecording: return CGPreflightScreenCaptureAccess() ? .granted : .missing
        case .microphone: return AVCaptureDevice.authorizationStatus(for: .audio) == .authorized ? .granted : .missing
        case .calendars: return calendarState(IslandCalendarController.status)
        case .focus: return focusState(INFocusStatusCenter.default.authorizationStatus)
        case .loginItem: return SMAppService.mainApp.status == .enabled ? .granted : .missing
        case .keychain: return KeychainGrant.status()
        default: return automation(p.bundleID ?? "")
        }
    }

    /// EventKit status (IslandCalendarController.status) -> row state. macOS 14+: fullAccess is
    /// Allowed; writeOnly ("Add Events Only") is not enough to read events and needs Full Access.
    static func calendarState(_ status: String) -> PermissionState {
        status == "granted" ? .granted : .missing
    }

    static func focusState(_ s: INFocusStatusAuthorizationStatus) -> PermissionState {
        s == .authorized ? .granted : .missing
    }

    /// The raw OS answer, for diagnostics and for deciding whether a request can still prompt.
    static func rawStatus(_ p: ACUPermission) -> String {
        switch p {
        case .calendars: return IslandCalendarController.status
        case .focus:
            switch INFocusStatusCenter.default.authorizationStatus {
            case .authorized: return "granted"
            case .denied: return "denied"
            case .restricted: return "restricted"
            case .notDetermined: return "not-determined"
            @unknown default: return "unknown"
            }
        case .microphone:
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized: return "granted"
            case .denied: return "denied"
            case .restricted: return "restricted"
            case .notDetermined: return "not-determined"
            @unknown default: return "unknown"
            }
        default: return status(p) == .granted ? "granted" : "missing"
        }
    }

    /// A request API can only show a prompt while the OS has not decided yet. Once decided
    /// (denied / write-only / restricted), asking returns at once: open the pane instead.
    static func canPrompt(rawStatus: String) -> Bool {
        rawStatus == "not-determined" || rawStatus == "missing" || rawStatus == "unknown"
    }

    static func installed(_ bundleID: String) -> Bool {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) != nil
    }

    /// AEDeterminePermissionToAutomateTarget(askUserIfNeeded: false): a pure status read, never a
    /// prompt (the asking variant lives only in PermissionBroker). Blocks briefly: avoid main.
    static func automation(_ bundleID: String) -> PermissionState {
        guard installed(bundleID) else { return .notInstalled }
        let target = NSAppleEventDescriptor(bundleIdentifier: bundleID)
        guard let desc = target.aeDesc else { return .missing }
        let s = AEDeterminePermissionToAutomateTarget(desc, typeWildCard, typeWildCard, false)
        return classify(s)
    }

    static func classify(_ s: OSStatus) -> PermissionState {
        switch s {
        case noErr: return .granted
        case OSStatus(procNotFound): return .notRunning
        default: return .missing   // errAEEventNotPermitted (-1743), errAEEventWouldRequireUserConsent (-1744)
        }
    }
}

/// Cached permission states shared by onboarding, ACU Settings, the island and the RPC gate.
@MainActor
final class PermissionCenter: ObservableObject {
    static let shared = PermissionCenter()

    @Published private(set) var states: [ACUPermission: PermissionState] = [:]
    /// Called after every refresh (island warning, settings rows).
    var onChange: [([ACUPermission: PermissionState]) -> Void] = []

    private nonisolated static let cache = OSAllocatedUnfairLock<[ACUPermission: PermissionState]>(initialState: [:])
    /// Tests inject states here; nil = real probes.
    nonisolated(unsafe) static var override: [ACUPermission: PermissionState]?

    /// Last known state (any thread). Cheap TCC reads are done live when nothing is cached yet.
    nonisolated static func state(_ p: ACUPermission) -> PermissionState {
        if let o = override { return o[p] ?? .missing }
        if let s = cache.withLock({ $0[p] }) { return s }
        return p.bundleID == nil ? PermissionProbe.status(p) : .notRunning
    }

    nonisolated static func missingRequired() -> [ACUPermission] {
        ACUPermission.allCases.filter { $0.required && !state($0).satisfied }
    }

    private var poll: Timer?
    /// Revocations land while the app is in the background (LSUIElement): re-read once a minute.
    func startPolling(every seconds: TimeInterval = 60) {
        poll?.invalidate()
        poll = Timer.scheduledTimer(withTimeInterval: seconds, repeats: true) { _ in
            Task { @MainActor in PermissionCenter.shared.refresh() }
        }
    }

    /// Re-reads every status off the main thread (Apple Event status calls can block briefly).
    func refresh(_ done: (@MainActor () -> Void)? = nil) {
        if let o = Self.override { apply(o); done?(); return }
        Task.detached(priority: .utility) {
            var s: [ACUPermission: PermissionState] = [:]
            for p in ACUPermission.allCases { s[p] = PermissionCenter.remembered(p, PermissionProbe.status(p)) }
            let snapshot = s
            await MainActor.run {
                PermissionCenter.shared.apply(snapshot)
                done?()
            }
        }
    }

    private func apply(_ s: [ACUPermission: PermissionState]) {
        Self.cache.withLock { $0 = s }
        if s != states { states = s }
        for f in onChange { f(s) }
    }

    /// Automation targets that aren't running can't be read; keep the last answer macOS gave us.
    nonisolated static func remembered(_ p: ACUPermission, _ s: PermissionState, defaults: UserDefaults = .standard) -> PermissionState {
        let k = "acu.permission.lastGranted." + p.rawValue
        switch s {
        case .granted: defaults.set(true, forKey: k); return s
        case .missing: defaults.removeObject(forKey: k); return s
        case .notRunning: return defaults.bool(forKey: k) ? .granted : s
        case .notInstalled: return s
        }
    }
}

/// Runtime Apple Events (Now Playing, Music favourite) only go to targets already approved in
/// onboarding: sending one to an undecided target would raise the Automation prompt mid-task.
enum AutomationGate {
    private static let cache = OSAllocatedUnfairLock<[String: (ok: Bool, at: Date)]>(initialState: [:])
    nonisolated(unsafe) static var probe: (String) -> PermissionState = { PermissionProbe.automation($0) }
    static func allowed(_ bundleID: String, now: Date = Date()) -> Bool {
        if let c = cache.withLock({ $0[bundleID] }), now.timeIntervalSince(c.at) < 10 { return c.ok }
        let ok = probe(bundleID) == .granted
        cache.withLock { $0[bundleID] = (ok, now) }
        return ok
    }
    static func reset() { cache.withLock { $0 = [:] } }
}

/// Agent API: methods that need a consent fail fast with PERMISSION_MISSING:<name> instead of hanging.
enum PermissionGate {
    static let errorCode = -32010
    static let accessibilityMethods: Set<String> = [
        "get_app_state", "click", "press_key", "hold_key", "type_text", "scroll", "set_value",
        "select_text", "perform_secondary_action", "drag", "execute_plan",
    ]

    /// Live read by default (AXIsProcessTrusted is cheap), so a revoke is caught without a refresh.
    /// AX reads only (no CGEvent posting): gated on AXIsProcessTrusted alone.
    static let axReadMethods: Set<String> = ["get_app_state"]

    static func missing(for method: String,
                        state: (ACUPermission) -> PermissionState = { PermissionCenter.override == nil ? PermissionProbe.status($0) : PermissionCenter.state($0) },
                        axTrusted: () -> Bool = { PermissionCenter.override.map { $0[.accessibility] == .granted } ?? AXIsProcessTrusted() }) -> ACUPermission? {
        guard accessibilityMethods.contains(method) else { return nil }
        if axReadMethods.contains(method) { return axTrusted() ? nil : .accessibility }
        return state(.accessibility).satisfied ? nil : .accessibility
    }

    static func message(_ p: ACUPermission) -> String { "PERMISSION_MISSING:\(p.rawValue)" }
}
