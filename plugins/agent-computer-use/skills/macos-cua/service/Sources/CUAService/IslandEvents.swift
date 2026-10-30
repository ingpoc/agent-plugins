import AppKit
import Foundation

/// Island interaction log (`~/.cache/macos-cua/island-events.jsonl`, 0600, rotated at 2 MB).
/// One JSON object per line: ts, event, id, source, kind + small numeric/enum fields.
/// Never card titles, option values or typed text (they can carry secrets).
/// Events: shown · hovered · clicked (x/y) · opened (via) · answered (via) · expired · peek · notice.
enum IslandEventLog {
    /// Tests inject a sink; under XCTest nothing is written to disk unless `url` is set explicitly.
    nonisolated(unsafe) static var sink: (([String: Any]) -> Void)?
    nonisolated(unsafe) static var url: URL? = isTesting ? nil : defaultURL()
    nonisolated static let maxBytes = 2_000_000
    /// Serial: appends never interleave within this process (the only writer).
    private static let queue = DispatchQueue(label: "island-events")
    nonisolated(unsafe) private static var checkedMode = false

    nonisolated static var isTesting: Bool { NSClassFromString("XCTestCase") != nil }

    nonisolated static func defaultURL() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache/macos-cua/island-events.jsonl")
    }

    /// Allowed extra keys only (anything else is dropped): keeps the log free of content.
    nonisolated static let allowedExtras: Set<String> = [
        "x", "y", "via", "state", "pending", "options", "values", "has_text", "peek", "remaining_s", "status",
    ]

    nonisolated static func log(_ event: String, id: String = "", source: String = "", kind: String = "",
                                _ extra: [String: Any] = [:]) {
        var o: [String: Any] = ["ts": ISO8601DateFormatter().string(from: Date()), "event": event]
        if !id.isEmpty { o["id"] = String(id.prefix(100)) }
        if !source.isEmpty { o["source"] = String(source.prefix(40)) }
        if !kind.isEmpty { o["kind"] = kind }
        for (k, v) in extra where allowedExtras.contains(k) {
            switch v {
            case let d as Double: o[k] = (d * 10).rounded() / 10
            case let f as CGFloat: o[k] = (Double(f) * 10).rounded() / 10
            case let i as Int: o[k] = i
            case let b as Bool: o[k] = b
            case let s as String: o[k] = String(s.prefix(40))
            default: break
            }
        }
        sink?(o)
        guard let url else { return }
        queue.async { append(o, to: url) }
    }

    nonisolated private static func append(_ o: [String: Any], to url: URL) {
        guard var line = try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys]) else { return }
        line.append(0x0A)
        let fm = FileManager.default
        try? fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let attrs = try? fm.attributesOfItem(atPath: url.path), let size = attrs[.size] as? Int, size > maxBytes {
            let old = url.appendingPathExtension("1")
            unlink(old.path)
            rename(url.path, old.path)
        }
        if !fm.fileExists(atPath: url.path) {
            fm.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
        } else if !checkedMode {
            chmod(url.path, 0o600)   // a file from an older build: tighten once per process
        }
        checkedMode = true
        guard let h = try? FileHandle(forWritingTo: url) else { return }
        defer { try? h.close() }
        _ = try? h.seekToEnd()
        try? h.write(contentsOf: line)
    }
}

/// Choice-arrival Quick Peek: 4 s non-key peek on arrival, re-peek at +2 min and at T-60 s,
/// max 3 peeks per card. A slot that falls while the user is typing in the source chat app waits
/// until he stops (or the next slot replaces it).
struct IslandPeekScheduler: Equatable {
    static let duration: TimeInterval = 4
    static let repeekAfter: TimeInterval = 120
    static let finalLead: TimeInterval = 60
    static let maxPeeks = 3

    private(set) var id = ""
    private(set) var slots: [Date] = []
    private(set) var count = 0
    private var next = 0

    mutating func reset(id: String, arrived: Date, expires: Date?) {
        self.id = id
        count = 0
        next = 0
        var s = [arrived, arrived.addingTimeInterval(Self.repeekAfter)]
        if let e = expires {
            let t = e.addingTimeInterval(-Self.finalLead)
            // Keep the T-60 slot only if it is clearly after the previous one (no back-to-back peeks).
            if t > arrived.addingTimeInterval(Self.repeekAfter + Self.duration) { s.append(t) }
            else if t > arrived.addingTimeInterval(Self.duration) { s[1] = min(s[1], t) }
            s = s.filter { $0 < e }
        }
        slots = s
    }

    /// True when a peek should start now. Missed slots collapse into the latest due one.
    mutating func due(now: Date, suppressed: Bool) -> Bool {
        guard !id.isEmpty, count < Self.maxPeeks, next < slots.count, now >= slots[next] else { return false }
        if suppressed { return false }
        while next + 1 < slots.count, now >= slots[next + 1] { next += 1 }
        next += 1
        count += 1
        return true
    }

    var finished: Bool { count >= Self.maxPeeks || next >= slots.count }
}

/// Read-only input/front-app probes (no event taps, no permissions).
enum IslandInput {
    /// Seconds since the last key press (HID system state). "Typing" = < 3 s.
    static func secondsSinceKeyDown() -> TimeInterval {
        CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .keyDown)
    }

    static let typingWindow: TimeInterval = 3

    /// The app the card came from (its chat) — by app name or agent source.
    static func isSourceApp(_ frontName: String?, approval a: IslandAgentApproval) -> Bool {
        guard let f = frontName, !f.isEmpty else { return false }
        if !a.app.isEmpty, f == a.app { return true }
        if let s = AgentSource.match(f), s != .samantha, s == (AgentSource.match(a.agent) ?? AgentSource.match(a.app)) { return true }
        return false
    }
}

/// Soft arrival sound (system "Tink" at 0.3 of its own volume — never the system volume or device).
enum IslandSound {
    nonisolated(unsafe) static var player: (() -> Void)? = IslandEventLog.isTesting ? nil : {
        guard let s = NSSound(named: NSSound.Name("Tink"))?.copy() as? NSSound else { return }
        s.volume = 0.3
        s.play()
    }
    nonisolated(unsafe) static var played = 0
    static func tink() { played += 1; player?() }
}
