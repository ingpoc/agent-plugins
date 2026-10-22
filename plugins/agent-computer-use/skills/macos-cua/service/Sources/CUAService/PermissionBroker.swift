import AppKit
import ApplicationServices
import AVFoundation
import CoreGraphics
import EventKit
import Intents
import os
import ServiceManagement

/// THE choke point for every macOS consent prompt ACU can cause (TCC, Automation, Login Item,
/// Keychain "Always Allow"). Guru's rule: every approval happens once, from a Grant click in
/// onboarding (or the same row in ACU Settings) — nowhere else. Everything outside this file uses
/// preflight / no-prompt reads only; `tests/test_prompt_lint.py` fails the build if a request API
/// appears anywhere else.
@MainActor
enum PermissionBroker {
    enum Origin: String { case onboarding, settingsGrant }

    private static let log = Logger(subsystem: "com.ingpoc.cua-service", category: "permissions")
    /// Tests: record instead of firing real prompts.
    nonisolated(unsafe) static var dryRun = false
    nonisolated(unsafe) static var fired: [(ACUPermission, Origin)] = []
    /// Tests: stand-in for "that window is on screen".
    nonisolated(unsafe) static var originVisible: ((Origin) -> Bool)?

    /// Guard: only a Grant click in a visible onboarding / ACU Settings window may prompt.
    static func allowed(_ origin: Origin) -> Bool {
        if let v = originVisible { return v(origin) }
        switch origin {
        case .onboarding: return OnboardingWindowController.shared.window?.isVisible == true
        case .settingsGrant: return IslandSettingsWindowController.shared.window?.isVisible == true
        }
    }

    /// Which Grant the click came from (both windows share the row component).
    static func origin() -> Origin {
        OnboardingWindowController.shared.window?.isKeyWindow == true ? .onboarding : .settingsGrant
    }

    @discardableResult
    static func request(_ p: ACUPermission, from origin: Origin) -> Bool {
        guard Thread.isMainThread, allowed(origin) else {
            log.fault("blocked \(p.rawValue, privacy: .public) request outside onboarding/Settings Grant")
            assert(dryRun || originVisible != nil, "PermissionBroker.request outside a Grant click")
            return false
        }
        if dryRun { fired.append((p, origin)); return true }
        fire(p)
        return true
    }

    /// Settings › Calendar / Focus buttons are Grant rows too.
    static func requestCalendars() { request(.calendars, from: .settingsGrant) }
    static func requestFocus() { request(.focus, from: .settingsGrant) }

    /// Real request API first, from this (installed) process, so TCC
    /// registers this exact app (bundle + designated requirement) and lists it in System Settings.
    /// Only if the permission is still not granted afterwards does it deep-link to the pane.
    private static func fire(_ p: ACUPermission) {
        // Re-check first: an already-granted row just turns Allowed (no request, no pane, no
        // window re-ordering). A decided-but-not-granted status cannot prompt again, so the
        // request would return at once; go straight to the pane instead.
        let raw = PermissionProbe.rawStatus(p)
        SamanthaActivityLog.startup(phase: "permission_grant", status: raw == "granted" ? "ok" : "pending",
                                    detail: "\(p.rawValue): \(raw)")
        if PermissionProbe.status(p).satisfied {
            PermissionCenter.shared.refresh()
            return
        }
        if [.calendars, .focus, .microphone].contains(p), !PermissionProbe.canPrompt(rawStatus: raw) {
            GrantResume.shared.begin(p)
            if let u = p.paneURL { NSWorkspace.shared.open(u) }
            PermissionCenter.shared.refresh()
            return
        }
        GrantResume.shared.begin(p)
        let openPaneIfStillMissing: @MainActor () -> Void = {
            if !PermissionProbe.status(p).satisfied, let u = p.paneURL { NSWorkspace.shared.open(u) }
            PermissionCenter.shared.refresh()
        }
        // Give TCC a moment to register the request before the pane opens (it lists the app then).
        let later = { DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { MainActor.assumeIsolated { openPaneIfStillMissing() } } }
        switch p {
        case .accessibility:
            let prompt = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            _ = AXIsProcessTrustedWithOptions([prompt: true] as CFDictionary)
            if !CGPreflightPostEventAccess() { _ = CGRequestPostEventAccess() }
            later()
        case .screenRecording:
            _ = CGRequestScreenCaptureAccess()
            later()
        case .microphone:
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                Task { @MainActor in openPaneIfStillMissing() }
            }
        case .calendars:
            let store = EKEventStore()
            let done: (Bool, Error?) -> Void = { _, _ in Task { @MainActor in openPaneIfStillMissing() } }
            if #available(macOS 14.0, *) { store.requestFullAccessToEvents(completion: done) }
            else { store.requestAccess(to: .event, completion: done) }
        case .focus:
            INFocusStatusCenter.default.requestAuthorization { _ in Task { @MainActor in openPaneIfStillMissing() } }
        case .keychain:
            // Blocks while the Keychain's own prompts are up: off main, one item at a time.
            Task.detached(priority: .userInitiated) {
                KeychainGrant.readAll()
                await MainActor.run { PermissionCenter.shared.refresh() }
            }
        case .loginItem:
            do { try SMAppService.mainApp.register() } catch { SamanthaActivityLog.startup(phase: "login_item", status: "fail", detail: "\(error)") }
            if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
            PermissionCenter.shared.refresh()
        default:
            guard let bid = p.bundleID else { return }
            Task.detached(priority: .userInitiated) {
                await Self.prewarmAutomation(bid)   // AEDeterminePermissionToAutomateTarget(ask: true)
                await MainActor.run { openPaneIfStillMissing() }
            }
        }
    }

    /// One harmless, consent-gated Apple Event per target: launch it hidden if needed, then
    /// AEDeterminePermissionToAutomateTarget(ask: true) — macOS shows its own prompt once.
    nonisolated static func prewarmAutomation(_ bundleID: String) async {
        guard PermissionProbe.installed(bundleID) else { return }
        if NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty,
           let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = false
            cfg.hides = true
            _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: cfg)
            for _ in 0..<30 where NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        }
        guard let desc = NSAppleEventDescriptor(bundleIdentifier: bundleID).aeDesc else { return }
        _ = AEDeterminePermissionToAutomateTarget(desc, typeWildCard, typeWildCard, true)
    }
}
