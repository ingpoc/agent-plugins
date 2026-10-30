import Foundation
import XCTest

/// Every test `UserDefaults` suite goes through here. `removePersistentDomain` empties a suite but
/// cfprefsd leaves `~/Library/Preferences/<suite>.plist` behind, so test runs used to strand one
/// plist per suite (thousands over time). At the end of the test bundle each suite is removed and
/// its plist deleted.
enum TestDefaults {
    private static let lock = NSLock()
    private static var names = Set<String>()
    private static var observer: Cleanup?

    static func make(_ suiteName: String) -> UserDefaults? {
        lock.lock()
        names.insert(suiteName)
        if observer == nil {
            let o = Cleanup()
            observer = o
            XCTestObservationCenter.shared.addTestObserver(o)
        }
        lock.unlock()
        return UserDefaults(suiteName: suiteName)
    }

    static func cleanUp() {
        lock.lock()
        let all = names
        names.removeAll()
        lock.unlock()
        let prefs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Preferences")
        for name in all {
            UserDefaults(suiteName: name)?.removePersistentDomain(forName: name)
            CFPreferencesAppSynchronize(name as CFString)
            try? FileManager.default.removeItem(at: prefs.appendingPathComponent("\(name).plist"))
        }
    }

    private final class Cleanup: NSObject, XCTestObservation {
        func testBundleDidFinish(_ testBundle: Bundle) { TestDefaults.cleanUp() }
    }
}
