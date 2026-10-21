import Foundation

/// Island copy contract (mirrors voice_cua/island_copy.py — Python is the
/// source, this is the defence so a raw string can never reach the pill).
///
/// Compact `title` ≤ 28 characters, verb + target. Overflow moves to the
/// expanded-only `detail`; titles are cut on a word boundary, never with an
/// ellipsis. Errors map to short human copy; raw text is logged, not shown.
enum IslandCopy {
    static let maxTitle = 28
    static let maxError = 32

    static let fallbackError = "Missed that, retrying"

    private static let benign: [String] = [
        "already shorter than", "cancel_not_active", "no active response",
        "item_not_found", "buffer_clear_empty",
    ]

    /// (lower-cased needles, copy) — first match wins. `{target}` = app/control.
    private static let errorTable: [([String], String)] = [
        (["mic unavailable", "microphone", "sounddevice", "input device"], "Can't hear you, check mic"),
        (["websocket", "connection reset", "connection closed", "connection lost", "connection refused",
          "reconnect", "ping timeout", "session expired", "session not found"], "Connection dropped, reconnecting"),
        (["label not found", "control not found", "no such element", "click"], "Couldn't click {target}"),
        (["app not found", "can't open app", "unable to open", "not installed"], "Couldn't open {target}"),
        (["no window"], "No {target} window"),
        (["denied"], "Stopped — you said no"),
        (["comet unavailable", "comet busy", "extension"], "Comet isn't available"),
        (["verif", "expect"], fallbackError),
        (["timeout", "timed out"], "That took too long"),
        (["config", "invalid_request", "invalid_value", "realtime"], "Voice hiccup, still here"),
    ]

    private static let humanCopy: Set<String> = [
        fallbackError, "Can't hear you, check mic", "Connection dropped, reconnecting",
        "Stopped — you said no", "Comet isn't available", "That took too long", "Voice hiccup, still here",
    ]

    static func squash(_ s: String) -> String {
        s.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    /// Clamp the title to `maxTitle`; overflow goes to the front of `detail`.
    static func compact(title rawTitle: String, detail rawDetail: String) -> (title: String, detail: String) {
        let title = squash(rawTitle), detail = squash(rawDetail)
        guard title.count > maxTitle else { return (title, detail) }
        func join(_ rest: String) -> String {
            let r = squash(rest)
            if r.isEmpty { return detail }
            return detail.isEmpty ? r : "\(r) · \(detail)"
        }
        if let range = title.range(of: " · ") {
            let head = String(title[..<range.lowerBound])
            if head.count <= maxTitle { return (head, join(String(title[range.upperBound...]))) }
        }
        let words = title.split(separator: " ").map(String.init)
        var kept: [String] = []
        for w in words {
            if (kept + [w]).joined(separator: " ").count > maxTitle { break }
            kept.append(w)
        }
        if kept.isEmpty { // one giant token (URL/path): keep its start, no ellipsis
            return (String(title.prefix(maxTitle)), join(String(title.dropFirst(maxTitle))))
        }
        return (kept.joined(separator: " "), join(words.dropFirst(kept.count).joined(separator: " ")))
    }

    static func isBenign(_ raw: String) -> Bool {
        let low = raw.lowercased()
        return benign.contains { low.contains($0) }
    }

    /// Human copy for an error, or nil when it must never be shown.
    static func humanError(_ rawText: String, target: String = "") -> String? {
        let raw = squash(rawText)
        if isBenign(raw) { return nil }
        if humanCopy.contains(raw) { return raw }
        if (raw.hasPrefix("Couldn't ") || raw.hasPrefix("No ")), raw.count <= maxError { return raw }
        let low = raw.lowercased()
        let name = String(squash(target).prefix(16))
        for (needles, copy) in errorTable where needles.contains(where: { low.contains($0) }) {
            let text = copy.replacingOccurrences(of: "{target}", with: name.isEmpty ? "that" : name)
            return String(text.prefix(maxError)).trimmingCharacters(in: .whitespaces)
        }
        return fallbackError
    }
}
