import AppKit
import Darwin
import Intents

/// Peek privacy (polish bar: peeks never leak during sharing; Focus respected). Public APIs only.
enum IslandPrivacy {
    /// Remote Screen Sharing / ARD session: `screensharingd` is launched on demand per session.
    /// macOS has no public "screen is being recorded" signal; recordings are covered by
    /// `NSWindow.sharingType = .none` while a peek is up (see SamanthaMenuBarIslandPanel).
    static func screenSharingActive() -> Bool { processRunning(["screensharingd"]) }

    static func processRunning(_ names: Set<String>) -> Bool {
        let n = proc_listallpids(nil, 0)
        guard n > 0 else { return false }
        var pids = [pid_t](repeating: 0, count: Int(n) + 32)
        let got = proc_listallpids(&pids, Int32(pids.count * MemoryLayout<pid_t>.size))
        guard got > 0 else { return false }
        var name = [CChar](repeating: 0, count: 256)
        for pid in pids.prefix(Int(got)) where pid > 0 {
            if proc_name(pid, &name, UInt32(name.count)) > 0, names.contains(String(cString: name)) { return true }
        }
        return false
    }

    static func peeksAllowed(sharing: Bool, focused: Bool, hideWhileSharing: Bool, respectFocus: Bool) -> Bool {
        !(hideWhileSharing && sharing) && !(respectFocus && focused)
    }
}

/// Focus / Do Not Disturb via INFocusStatusCenter. Authorization is asked ONLY from the Settings button
/// (a real system prompt); until granted the island cannot see Focus and peeks are unaffected.
@MainActor
final class IslandFocusStatus {
    var authorized: Bool { INFocusStatusCenter.default.authorizationStatus == .authorized }
    var isFocused: Bool { authorized && INFocusStatusCenter.default.focusStatus.isFocused == true }
    var statusText: String {
        switch INFocusStatusCenter.default.authorizationStatus {
        case .authorized: return "Allowed"
        case .denied: return "Denied in System Settings"
        case .restricted: return "Unavailable"
        default: return "Not requested"
        }
    }
    // Asking for access lives in PermissionBroker (onboarding / Settings Grant only).
}
