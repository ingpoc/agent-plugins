import Foundation
import AppKit

struct IslandClaudeDesktopStatus: Codable, Equatable {
    var attached_pid: Int32?
    var last_read_at: String?
    var rows_by_status: [String: Int]
    var row_count: Int
    var model_picker_seen: Bool
    var manual_accessibility: ManualAccessibility
    var chat_tab_active: Bool

    struct ManualAccessibility: Codable, Equatable {
        var set: Bool
        var attempts: Int
        var last_error: String?

        private enum CodingKeys: String, CodingKey { case set, attempts, last_error }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(set, forKey: .set)
            try c.encode(attempts, forKey: .attempts)
            try c.encode(last_error, forKey: .last_error)
        }
    }

    private static let lock = NSLock()
    private static let defaultsKey = "IslandClaudeDesktopStatus.metadata"
    private static var latest = IslandClaudeDesktopStatus(attached_pid: nil, last_read_at: nil,
        rows_by_status: emptyCounts, row_count: 0, model_picker_seen: false,
        manual_accessibility: .init(set: false, attempts: 0, last_error: nil), chat_tab_active: false)

    static func current() -> Self {
        lock.lock(); defer { lock.unlock() }
        if let data = UserDefaults.standard.data(forKey: defaultsKey), let stored = try? JSONDecoder().decode(Self.self, from: data) {
            latest = stored
        }
        if let pid = latest.attached_pid,
           !NSRunningApplication.runningApplications(withBundleIdentifier: IslandClaudeDesktopActivity.bundleID)
            .contains(where: { $0.processIdentifier == pid_t(pid) && !$0.isTerminated }) {
            latest = Self(attached_pid: nil, last_read_at: nil, rows_by_status: emptyCounts, row_count: 0,
                model_picker_seen: false, manual_accessibility: .init(set: false, attempts: 0, last_error: nil), chat_tab_active: false)
            UserDefaults.standard.removeObject(forKey: defaultsKey)
        }
        return latest
    }

    static func update(pid: Int32?, snapshot: IslandClaudeDesktopActivity.Snapshot?, readAt: Date?, attempts: Int, lastError: String?) {
        lock.lock(); defer { lock.unlock() }
        let rows = snapshot?.rows ?? []
        var counts = emptyCounts
        for row in rows {
            let key: String
            switch row.status {
            case .working: key = "working"
            case .needsYou: key = "needs_you"
            case .done: key = "done"
            case .error: key = "error"
            case .idle: key = "idle"
            }
            counts[key, default: 0] += 1
        }
        latest = Self(attached_pid: pid, last_read_at: readAt.map(iso8601), rows_by_status: counts,
            row_count: rows.count, model_picker_seen: snapshot?.modelLabel != nil,
            manual_accessibility: .init(set: attempts > 0 && lastError == nil, attempts: attempts, last_error: lastError),
            chat_tab_active: snapshot?.chatTabActive ?? false)
        if let data = try? JSONEncoder().encode(latest) { UserDefaults.standard.set(data, forKey: defaultsKey) }
    }

    private static var emptyCounts: [String: Int] { ["working": 0, "idle": 0, "error": 0, "done": 0, "needs_you": 0] }

    private enum CodingKeys: String, CodingKey { case attached_pid, last_read_at, rows_by_status, row_count, model_picker_seen, manual_accessibility, chat_tab_active }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(attached_pid, forKey: .attached_pid)
        try c.encode(last_read_at, forKey: .last_read_at)
        try c.encode(rows_by_status, forKey: .rows_by_status)
        try c.encode(row_count, forKey: .row_count)
        try c.encode(model_picker_seen, forKey: .model_picker_seen)
        try c.encode(manual_accessibility, forKey: .manual_accessibility)
        try c.encode(chat_tab_active, forKey: .chat_tab_active)
    }

    private static func iso8601(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}
