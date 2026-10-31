import AppKit
import ArgumentParser
import CoreGraphics

@MainActor
enum IslandGateWindowCapture {
    static func capture(window: NSWindow, to url: URL,
                        screenRecordingAllowed: () -> Bool = CGPreflightScreenCaptureAccess) throws {
        guard screenRecordingAllowed() else {
            throw ValidationError("Screen Recording permission is required for --live-capture. Enable it for CUAService or its launching terminal in System Settings > Privacy & Security > Screen Recording, then rerun. No native screenshot was captured.")
        }
        let app = NSApplication.shared
        app.activate(ignoringOtherApps: true)
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.displayIfNeeded()
        let deadline = Date().addingTimeInterval(3)
        while !isOnScreen(window) && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        guard isOnScreen(window) else { throw ValidationError("The gate window did not appear on screen. Native capture requires an active desktop session.") }
        RunLoop.main.run(until: Date().addingTimeInterval(0.25))
        let capture = Process()
        capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        capture.arguments = ["-o", "-l", String(window.windowNumber), url.path]
        capture.standardOutput = FileHandle.nullDevice
        capture.standardError = Pipe()
        try capture.run()
        capture.waitUntilExit()
        guard capture.terminationStatus == 0 else {
            throw ValidationError("Native gate capture failed (screencapture exit \(capture.terminationStatus)). Check Screen Recording permission and that the desktop session is active. No offscreen fallback was used.")
        }
        guard let bitmap = NSBitmapImageRep(data: try Data(contentsOf: url)), bitmap.pixelsWide > 0, bitmap.pixelsHigh > 0 else {
            throw ValidationError("screencapture did not write a valid image of the gate window.")
        }
    }
    private static func isOnScreen(_ window: NSWindow) -> Bool {
        guard window.isVisible, window.windowNumber > 0,
              let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] else { return false }
        return windows.contains { ($0[kCGWindowNumber as String] as? NSNumber)?.intValue == window.windowNumber }
    }
}
