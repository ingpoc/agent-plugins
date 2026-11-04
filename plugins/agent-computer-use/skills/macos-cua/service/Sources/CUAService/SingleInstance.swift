import AppKit
import Darwin
import Foundation

/// One menu-bar CUAService per user (Guru, 2026-11-04 11:18 IST: two ACU menu bar icons). The socket
/// lock lives in ~/.cache, which can be wiped while the app runs; a second launch then got a fresh lock
/// and its own status item. This guard does not depend on that directory: the installed app (bundle id
/// com.ingpoc.cua-service) also takes an exclusive lock in Application Support, and any other running
/// copy of the same bundle id wins. A second launch hands off and exits before any UI or socket.
enum CUAServiceSingleInstance {
    static let bundleID = "com.ingpoc.cua-service"

    enum Decision: Equatable {
        case proceed
        case handOff(existing: pid_t?)
    }

    /// Pure rule (tested): only the bundled app is guarded; test and dev binaries are not.
    static func decide(ownBundleID: String?, ownPID: pid_t, runningPIDs: [pid_t], lockAcquired: Bool) -> Decision {
        guard ownBundleID == bundleID else { return .proceed }
        if let other = runningPIDs.first(where: { $0 != ownPID }) { return .handOff(existing: other) }
        return lockAcquired ? .proceed : .handOff(existing: nil)
    }

    static func lockPath(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> String {
        home.appendingPathComponent("Library/Application Support/CUAService/instance.lock").path
    }

    /// Exclusive, non-blocking flock; the fd stays open for the process lifetime. -1 = held elsewhere.
    static func acquire(path: String) -> Int32 {
        try? FileManager.default.createDirectory(at: URL(fileURLWithPath: path).deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        let fd = Darwin.open(path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { return -1 }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 { Darwin.close(fd); return -1 }
        return fd
    }

    /// Live check at launch: true = this process should keep starting.
    @discardableResult
    static func claim(log: (String) -> Void) -> Bool {
        let own = Bundle.main.bundleIdentifier
        guard own == bundleID else { return true }
        let me = ProcessInfo.processInfo.processIdentifier
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            .filter { !$0.isTerminated }.map(\.processIdentifier)
        let fd = others.contains(where: { $0 != me }) ? -1 : acquire(path: lockPath())
        switch decide(ownBundleID: own, ownPID: me, runningPIDs: others, lockAcquired: fd >= 0) {
        case .proceed:
            heldFD = fd
            return true
        case .handOff(let existing):
            if fd >= 0 { Darwin.close(fd) }
            log("CUAService already running\(existing.map { " (pid \($0))" } ?? ""); handing off and exiting")
            return false
        }
    }

    nonisolated(unsafe) private static var heldFD: Int32 = -1
}
