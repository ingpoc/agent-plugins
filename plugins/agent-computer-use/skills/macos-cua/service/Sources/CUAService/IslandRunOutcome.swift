import Foundation

/// How an agent turn ended. `done` is a real finish; `stopped` is any end that produced no
/// final answer (quota cutoff, API failure, abort) and carries the short row reason.
enum IslandRunOutcome: Equatable {
    case done
    case stopped(reason: String)

    // MARK: - Codex rollout (`task_complete`)

    /// `task_complete` fires both on success and on a cutoff, so the payload's `error` and
    /// `last_agent_message` decide. `rateLimits` is the latest `token_count` window map.
    static func codex(taskComplete payload: [String: Any], rateLimits: [String: Any]?,
                      now: Date = Date()) -> IslandRunOutcome {
        let (failed, message) = codexError(payload["error"])
        guard failed else {
            return finalMessage(payload).isEmpty ? .stopped(reason: "No final answer") : .done
        }
        let exhausted = exhaustedWindow(rateLimits)
        if mentionsQuota(message) || exhausted != nil {
            return .stopped(reason: quotaReason(exhausted, now: now))
        }
        return .stopped(reason: "API error")
    }

    /// Folds one `token_count` `rate_limits` map into the latest known one. Codex also logs maps
    /// for other limits (for example `premium`) whose windows are null, right before a cutoff's
    /// `task_complete`; those carry no window and must not hide the exhausted `codex` window.
    static func mergeRateLimits(_ current: [String: Any]?, _ update: [String: Any]) -> [String: Any]? {
        ["primary", "secondary"].contains { update[$0] is [String: Any] } ? update : current
    }

    private static func codexError(_ value: Any?) -> (failed: Bool, message: String) {
        guard let value, !(value is NSNull) else { return (false, "") }
        if let error = value as? [String: Any] { return (true, error["message"] as? String ?? "") }
        if let text = value as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return (!trimmed.isEmpty, trimmed)
        }
        return (true, "")
    }

    private static func finalMessage(_ payload: [String: Any]) -> String {
        (payload["last_agent_message"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    /// The rate-limit window that hit 100%: the weekly (secondary) window first, then the 5 h one.
    private static func exhaustedWindow(_ rateLimits: [String: Any]?) -> [String: Any]? {
        ["secondary", "primary"].compactMap { rateLimits?[$0] as? [String: Any] }
            .first { (number($0["used_percent"]) ?? 0) >= 100 }
    }

    // MARK: - Claude Code projects jsonl

    /// One projects jsonl line: the end of a turn, or nil while the turn continues.
    static func claudeCode(line: [String: Any]) -> IslandRunOutcome? {
        let type = line["type"] as? String
        if type == "assistant", line["isApiErrorMessage"] as? Bool == true {
            switch (line["error"] as? String)?.lowercased() ?? "" {
            case "rate_limit": return .stopped(reason: "Quota exhausted")
            case "authentication_failed": return .stopped(reason: "Not logged in")
            default: return .stopped(reason: "API error")
            }
        }
        if type == "system", line["subtype"] as? String == "api_error" {
            let status = number((line["error"] as? [String: Any])?["status"])
            if status == 429 { return .stopped(reason: "Quota exhausted") }
            if status == 401 || status == 403 { return .stopped(reason: "Not logged in") }
            return .stopped(reason: "API error")
        }
        if type == "assistant",
           (line["message"] as? [String: Any])?["stop_reason"] as? String == "end_turn" { return .done }
        return nil
    }

    // MARK: - OpenCode assistant message data

    /// One assistant message: a terminal finish or error, or nil while tools still run.
    static func openCode(message: [String: Any]) -> IslandRunOutcome? {
        if let error = message["error"] as? [String: Any] {
            let name = error["name"] as? String ?? ""
            let text = ((error["data"] as? [String: Any])?["message"] as? String) ?? ""
            switch name {
            case "MessageAbortedError": return .stopped(reason: "Aborted")
            case "ContextOverflowError": return .stopped(reason: "Context overflow")
            default:
                return .stopped(reason: mentionsQuota(text) ? "Quota exhausted" : "API error")
            }
        }
        guard let finish = message["finish"] as? String else { return nil }
        switch finish {
        case "stop": return .done
        case "tool-calls": return nil
        case "length": return .stopped(reason: "Context overflow")
        case "error": return .stopped(reason: "API error")
        case "other", "unknown": return .stopped(reason: "Aborted")
        default: return nil
        }
    }

    // MARK: - Shared

    private static func mentionsQuota(_ text: String) -> Bool {
        let lower = text.lowercased()
        return ["usage limit", "rate limit", "quota"].contains { lower.contains($0) }
    }

    /// "Quota exhausted, resets 12:28 IST"; the reset date is the exhausted window's `resets_at`.
    /// A reset on another day in India adds "d MMM"; no `resets_at` omits the clause entirely.
    private static func quotaReason(_ window: [String: Any]?, now: Date) -> String {
        guard let seconds = number(window?["resets_at"]), seconds > 0 else { return "Quota exhausted" }
        let reset = Date(timeIntervalSince1970: seconds)
        let sameDay = calendar.isDate(reset, inSameDayAs: now)
        return "Quota exhausted, resets \(formatter(sameDay ? "HH:mm" : "d MMM HH:mm").string(from: reset)) IST"
    }

    private static let ist = TimeZone(identifier: "Asia/Kolkata") ?? .current
    private static let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = ist
        return calendar
    }()

    private static func formatter(_ format: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = ist
        formatter.dateFormat = format
        return formatter
    }

    private static func number(_ value: Any?) -> Double? {
        if let number = value as? NSNumber { return number.doubleValue }
        if let value = value as? Double { return value }
        if let value = value as? Int { return Double(value) }
        return nil
    }
}
