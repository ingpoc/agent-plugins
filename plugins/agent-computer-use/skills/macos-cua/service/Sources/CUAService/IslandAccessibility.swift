import Foundation
import SwiftUI

/// Shared opacity levels for island content, including increased contrast.
enum IslandInk {
    static let primary = 1.0
    static let secondary = 0.70
    static let meta = 0.60
    static let hairline = 0.15
    static let fill = 0.08
    static let increasedSecondary = 0.85
    static let increasedOutline = 0.35
    static let onWhiteSecondary = 0.70
    @MainActor static var increased = false
    @MainActor static var secondaryColor: Color { .white.opacity(increased ? increasedSecondary : secondary) }
    @MainActor static var metaColor: Color { .white.opacity(increased ? increasedSecondary : meta) }
}


enum IslandAgentBadge {
    static func showsCircle(rowCount: Int) -> Bool { rowCount > 0 }
    static func count(needsYou: Int) -> Int? { needsYou > 0 ? needsYou : nil }
}

enum IslandAgentAccessibility {
    /// Priority first: "Agents: 1 needs you, 1 error, 2 running, 1 done".
    static func label(_ agents: [IslandAgent]) -> String {
        let tasks = agents.filter { $0.status != "process" && $0.status != "harness" }
        let needs = tasks.filter { $0.needs_you || $0.status == "needs_you" }.count
        let errors = tasks.filter { !$0.needs_you && ["error", "stale", "expired"].contains($0.status) }.count
        let running = tasks.filter { !$0.needs_you && $0.status == "running" }.count
        let done = tasks.filter { !$0.needs_you && $0.status == "done" }.count
        let parts = [needs > 0 ? "\(needs) \(needs == 1 ? "needs" : "need") you" : nil,
                     errors > 0 ? "\(errors) \(errors == 1 ? "error" : "errors")" : nil,
                     running > 0 ? "\(running) running" : nil,
                     done > 0 ? "\(done) done" : nil].compactMap { $0 }
        return "Agents: " + (parts.isEmpty ? "none running" : parts.joined(separator: ", "))
    }
}


enum IslandAnnouncementPriority: Int, Comparable {
    case medium, high
    static func < (a: Self, b: Self) -> Bool { a.rawValue < b.rawValue }
}
enum IslandAnnouncementTrigger {
    case approval, choice, needsYou, error, done, expiry
    var priority: IslandAnnouncementPriority {
        switch self {
        case .approval, .choice, .needsYou: return .high
        case .error, .done, .expiry: return .medium
        }
    }
}

/// At most one announcement per `interval`. Extras are coalesced, never dropped outright:
/// the most important pending one (latest wins at equal priority) is delivered when the window opens,
/// so a routine message can never swallow a later approval.
struct IslandAnnouncementThrottle {
    typealias Item = (text: String, priority: IslandAnnouncementPriority)
    var clock: () -> Date = Date.init
    var interval: TimeInterval = 3
    private var last: Date?
    private(set) var pending: Item?

    /// Returns the item to post now, or nil when it was queued (see `nextDue`).
    mutating func offer(_ text: String, priority: IslandAnnouncementPriority) -> Item? {
        let now = clock()
        if let last, now.timeIntervalSince(last) < interval {
            if pending == nil || priority >= pending!.priority { pending = (text, priority) }
            return nil
        }
        last = now
        pending = nil
        return (text, priority)
    }

    /// When the queued item may be posted.
    var nextDue: Date? {
        guard pending != nil, let last else { return nil }
        return last.addingTimeInterval(interval)
    }

    /// The queued item once the window has opened.
    mutating func drain() -> Item? {
        guard let item = pending, let due = nextDue, clock() >= due else { return nil }
        pending = nil
        last = clock()
        return item
    }
}
