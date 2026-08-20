@preconcurrency import ApplicationServices
import AppKit
import Foundation
import Logging

struct ResolvedApp: @unchecked Sendable {
    let pid: pid_t
    let bundleID: String?
    let name: String
    let windowID: CGWindowID
    let windowTitle: String?
    let axApp: AXUIElement

    var axWindow: AXUIElement? {
        var windowsRef: CFTypeRef?
        AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef)
        guard let windows = windowsRef as? [AXUIElement] else { return nil }
        for win in windows {
            var winID: CGWindowID = 0
            if _CGSGetWindowID(win, &winID) == .success, winID == windowID {
                return win
            }
        }
        return windows.first
    }
}

@_silgen_name("_AXUIElementGetWindow")
func _CGSGetWindowID(_ element: AXUIElement, _ windowID: inout CGWindowID) -> AXError

final class AppResolver: @unchecked Sendable {
    private let logger: Logger
    private let lock = NSLock()
    private var windowCache: (pid: pid_t, id: CGWindowID, at: TimeInterval)?

    init(logger: Logger) { self.logger = logger }

    func invalidateWindowCache() {
        lock.lock()
        windowCache = nil
        lock.unlock()
    }

    func resolve(
        _ app: String,
        raiseForInput: Bool = false,
        preferFocusedWindow: Bool = false
    ) throws -> ResolvedApp {
        let runningApp = findRunningApp(app)
        guard let runningApp else {
            throw RPCMethodError(code: -32001, message: "App not found: \(app)")
        }
        let pid = runningApp.processIdentifier
        let axApp = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(axApp, 1.0)
        if preferFocusedWindow {
            invalidateWindowCache()
        }
        guard let listedID = findMainWindow(pid: pid) else {
            throw RPCMethodError(code: -32002, message: "No window for app: \(app)")
        }
        let focusedID = focusedWindowID(axApp: axApp)
        let windowID = preferFocusedWindow ? (focusedID ?? listedID) : listedID
        let cg = quartzBounds(windowID)
        let tiny = (cg?.width ?? 0) * (cg?.height ?? 0) < 80 * 80
        reveal(
            runningApp, axApp: axApp, windowID: windowID,
            tiny: tiny, raiseForInput: raiseForInput
        )
        if tiny {
            Thread.sleep(forTimeInterval: 0.08)
        }
        let raisedID: CGWindowID
        if tiny || preferFocusedWindow {
            raisedID = focusedWindowID(axApp: axApp) ?? windowID
        } else {
            raisedID = windowID
        }
        return ResolvedApp(
            pid: pid,
            bundleID: runningApp.bundleIdentifier,
            name: runningApp.localizedName ?? app,
            windowID: raisedID,
            windowTitle: windowTitle(axApp: axApp),
            axApp: axApp
        )
    }

    func listApps() -> [[String: Any]] {
        NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular }
            .map { app in
                [
                    "bundleId": app.bundleIdentifier as Any,
                    "name": app.localizedName as Any,
                    "pid": Int(app.processIdentifier),
                    "isActive": app.isActive,
                ] as [String: Any]
            }
    }

    private func findRunningApp(_ query: String) -> NSRunningApplication? {
        let apps = NSWorkspace.shared.runningApplications
        if let match = apps.first(where: { $0.bundleIdentifier == query }) {
            return match
        }
        let lower = query.lowercased()
        if let match = apps.first(where: {
            $0.localizedName?.lowercased() == lower
        }) {
            return match
        }
        if let match = apps.first(where: {
            $0.localizedName?.lowercased().contains(lower) == true
                || $0.bundleIdentifier?.lowercased().contains(lower) == true
        }) {
            return match
        }
        if let url = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: query
        ) {
            let config = NSWorkspace.OpenConfiguration()
            config.activates = false
            let semaphore = DispatchSemaphore(value: 0)
            var launched: NSRunningApplication?
            NSWorkspace.shared.openApplication(at: url, configuration: config) { app, _ in
                launched = app
                semaphore.signal()
            }
            semaphore.wait()
            if let launched {
                Thread.sleep(forTimeInterval: 1.0)
                return launched
            }
        }
        return nil
    }

    /// Stage Manager keeps a tiny on-screen proxy. On-screen-only pick that
    /// thumb; AX then reports the real frame over wallpaper. Prefer the
    /// largest layer-0 window that intersects a display (capturable), else
    /// the largest including off-screen, then raise it.
    private func findMainWindow(pid: pid_t) -> CGWindowID? {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        if let cache = windowCache, cache.pid == pid, now - cache.at < 2.0 {
            let id = cache.id
            lock.unlock()
            return id
        }
        lock.unlock()

        guard let info = CGWindowListCopyWindowInfo(
            [.optionAll, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: Any]] else { return nil }

        var ranked: [(CGWindowID, CGFloat, Bool)] = []
        let displays = Self.quartzDisplays()
        for win in info {
            guard let ownerPID = win[kCGWindowOwnerPID as String] as? pid_t,
                  ownerPID == pid,
                  let winID = win[kCGWindowNumber as String] as? CGWindowID,
                  let bounds = win[kCGWindowBounds as String] as? [String: Any],
                  let x = (bounds["X"] as? NSNumber)?.doubleValue,
                  let y = (bounds["Y"] as? NSNumber)?.doubleValue,
                  let w = (bounds["Width"] as? NSNumber)?.doubleValue,
                  let h = (bounds["Height"] as? NSNumber)?.doubleValue
            else { continue }
            let layer = win[kCGWindowLayer as String] as? Int ?? 0
            if layer != 0 { continue }
            let rect = CGRect(x: x, y: y, width: w, height: h)
            let visible = displays.contains { $0.intersects(rect) }
            ranked.append((winID, CGFloat(w * h), visible))
        }
        ranked.sort { a, b in
            if a.2 != b.2 { return a.2 && !b.2 }
            return a.1 > b.1
        }
        guard let best = ranked.first else { return nil }
        // Stage Manager thumbs must not stick in cache.
        if best.1 >= 80 * 80 {
            lock.lock()
            windowCache = (pid, best.0, now)
            lock.unlock()
        }
        return best.0
    }

    private func focusedWindowID(axApp: AXUIElement) -> CGWindowID? {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(
            axApp, kAXFocusedWindowAttribute as CFString, &ref
        )
        guard let win = ref else { return nil }
        var id: CGWindowID = 0
        guard _CGSGetWindowID(win as! AXUIElement, &id) == .success, id != 0 else {
            return nil
        }
        return id
    }

    private static func quartzDisplays() -> [CGRect] {
        NSScreen.screens.map { screen in
            let key = NSDeviceDescriptionKey("NSScreenNumber")
            if let num = screen.deviceDescription[key] as? NSNumber {
                return CGDisplayBounds(CGDirectDisplayID(num.uint32Value))
            }
            return screen.frame
        }
    }

    private func reveal(
        _ runningApp: NSRunningApplication,
        axApp: AXUIElement,
        windowID: CGWindowID,
        tiny: Bool,
        raiseForInput: Bool
    ) {
        // HID goes to the front app. Skipping raise because isActive was
        // stale true still posted cmd+n into Cursor.
        if !tiny && !raiseForInput { return }
        runningApp.activate(options: [.activateIgnoringOtherApps])
        var windowsRef: CFTypeRef?
        AXUIElementCopyAttributeValue(
            axApp, kAXWindowsAttribute as CFString, &windowsRef
        )
        if let windows = windowsRef as? [AXUIElement] {
            let target = windows.first { win in
                var id: CGWindowID = 0
                return _CGSGetWindowID(win, &id) == .success && id == windowID
            } ?? windows.first
            if let target {
                AXUIElementPerformAction(target, kAXRaiseAction as CFString)
            }
        }
        if raiseForInput {
            waitUntilFrontmost(runningApp.processIdentifier)
        }
    }

    private func waitUntilFrontmost(_ pid: pid_t, timeout: TimeInterval = 0.8) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if Self.hidFrontPid() == pid { return }
            Thread.sleep(forTimeInterval: 0.01)
        }
    }

    /// Who would receive HID. NSWorkspace.frontmostApplication lags
    /// activate; AX focused application matches the key window.
    static func hidFrontPid() -> pid_t? {
        let system = AXUIElementCreateSystemWide()
        var ref: CFTypeRef?
        if AXUIElementCopyAttributeValue(
            system, kAXFocusedApplicationAttribute as CFString, &ref
        ) == .success, let app = ref {
            var axPid: pid_t = 0
            if AXUIElementGetPid(app as! AXUIElement, &axPid) == .success,
               axPid != 0 {
                return axPid
            }
        }
        return NSWorkspace.shared.frontmostApplication?.processIdentifier
    }

    private func quartzBounds(_ windowID: CGWindowID) -> CGRect? {
        guard let info = CGWindowListCopyWindowInfo(
            [.optionIncludingWindow], windowID
        ) as? [[String: Any]],
              let win = info.first,
              let bounds = win[kCGWindowBounds as String] as? [String: Any],
              let x = (bounds["X"] as? NSNumber)?.doubleValue,
              let y = (bounds["Y"] as? NSNumber)?.doubleValue,
              let w = (bounds["Width"] as? NSNumber)?.doubleValue,
              let h = (bounds["Height"] as? NSNumber)?.doubleValue
        else { return nil }
        return CGRect(x: x, y: y, width: w, height: h)
    }

    private func windowTitle(axApp: AXUIElement) -> String? {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &ref)
        guard let win = ref else { return nil }
        let axWin = win as! AXUIElement
        var titleRef: CFTypeRef?
        AXUIElementCopyAttributeValue(axWin, kAXTitleAttribute as CFString, &titleRef)
        return titleRef as? String
    }
}

struct RPCMethodError: Error {
    let code: Int
    let message: String
}
