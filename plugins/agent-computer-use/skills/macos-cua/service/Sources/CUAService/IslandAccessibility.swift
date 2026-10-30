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
    static func label(running: Int, needsYou: Int) -> String {
        let suffix = needsYou > 0 ? ", \(needsYou) \(needsYou == 1 ? "needs" : "need") you" : ""
        return "Agents: \(running) running" + suffix
    }
}


enum IslandAnnouncementPriority { case medium, high }
enum IslandAnnouncementTrigger {
    case approval, choice, needsYou, error, done, expiry
    var priority: IslandAnnouncementPriority {
        switch self {
        case .approval, .choice, .needsYou: return .high
        case .error, .done, .expiry: return .medium
        }
    }
}

struct IslandAnnouncementThrottle {
    var clock: () -> Date = Date.init
    private var last: Date?
    mutating func allows() -> Bool {
        let now = clock()
        if let last, now.timeIntervalSince(last) < 3 { return false }
        last = now
        return true
    }
}
