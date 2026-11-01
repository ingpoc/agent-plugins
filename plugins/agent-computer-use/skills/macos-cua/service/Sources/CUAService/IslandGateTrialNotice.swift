import Foundation
import UserNotifications

struct IslandGateTrialNoticeSchedule {
    static func parseDate(_ raw: String) -> Date? {
        let standard = ISO8601DateFormatter()
        if let date = standard.date(from: raw) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: raw)
    }

    static func fireDate(trialEnds: Date, now: Date) -> Date? {
        let due = trialEnds.addingTimeInterval(-24 * 60 * 60)
        return due > now ? due : nil
    }

    static func isAlreadyAuthorized(_ status: UNAuthorizationStatus) -> Bool {
        status == .authorized || status == .provisional
    }

    static func cancel(accountID: String, fixture: Bool) {
        PermissionBroker.cancelTrialNotice(accountID: accountID, fixture: fixture)
    }

    static func schedule(accountID: String, trialEnds: Date, fixture: Bool, now: Date = Date()) {
        PermissionBroker.scheduleTrialNotice(accountID: accountID, trialEnds: trialEnds, fixture: fixture, now: now)
    }
}
