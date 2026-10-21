import AVFoundation
import Foundation

/// Microphone status for voice-cua.app — preflight only, never a prompt (Guru's rule: every
/// approval happens once, in ACU onboarding). Exit 0 = authorized, 2 = not yet asked, 1 = denied.
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized: exit(0)
case .notDetermined: exit(2)
default: exit(1)
}
