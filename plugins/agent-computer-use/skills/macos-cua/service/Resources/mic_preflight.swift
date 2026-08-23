import AppKit
import AVFoundation
import Foundation

/// Trigger macOS microphone TCC for voice-cua.app (PortAudio preflight alone may not prompt).
let app = NSApplication.shared
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)

let sem = DispatchSemaphore(value: 0)
var granted = false

switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized:
    granted = true
    sem.signal()
case .notDetermined:
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        granted = ok
        sem.signal()
    }
default:
    granted = false
    sem.signal()
}

_ = sem.wait(timeout: .now() + 120)
exit(granted ? 0 : 1)
