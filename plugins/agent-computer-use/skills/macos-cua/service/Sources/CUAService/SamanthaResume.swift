import Foundation

/// Survives a CUAService restart: if Samantha was on when the service got
/// SIGTERM (install, launchd restart), a short-lived marker brings her back on
/// the next launch — the same supported start path as the menu-bar toggle.
/// A user "off" or Quit never writes it; it expires after 5 min.
enum SamanthaResume {
    static let maxAge: TimeInterval = 300

    static var defaultURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache/macos-cua/samantha-resume.json")
    }

    static func write(at url: URL = defaultURL, now: Date = Date()) {
        let body = #"{"resume":true,"ts":\#(now.timeIntervalSince1970)}"#
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? Data(body.utf8).write(to: url, options: .atomic)
    }

    static func isFresh(written: Date, now: Date) -> Bool {
        let age = now.timeIntervalSince(written)
        return age >= -5 && age <= maxAge
    }

    /// Reads and deletes the marker; true when Samantha should be resumed.
    static func consume(at url: URL = defaultURL, now: Date = Date()) -> Bool {
        guard let data = try? Data(contentsOf: url) else { return false }
        try? FileManager.default.removeItem(at: url)
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["resume"] as? Bool == true, let ts = obj["ts"] as? Double else { return false }
        return isFresh(written: Date(timeIntervalSince1970: ts), now: now)
    }
}
