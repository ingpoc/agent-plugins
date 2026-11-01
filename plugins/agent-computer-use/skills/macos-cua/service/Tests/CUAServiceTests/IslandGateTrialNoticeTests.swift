import XCTest
import UserNotifications
@testable import CUAService

final class IslandGateTrialNoticeTests: XCTestCase {
    func testNoticeIsDueExactlyOneDayBeforeTrialEnd() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let ends = now.addingTimeInterval(72 * 60 * 60)
        XCTAssertEqual(IslandGateTrialNoticeSchedule.fireDate(trialEnds: ends, now: now), now.addingTimeInterval(48 * 60 * 60))
        XCTAssertNil(IslandGateTrialNoticeSchedule.fireDate(trialEnds: now.addingTimeInterval(23 * 60 * 60), now: now))
    }

    func testNoticeRequiresExistingPermission() {
        XCTAssertTrue(IslandGateTrialNoticeSchedule.isAlreadyAuthorized(.authorized))
        XCTAssertTrue(IslandGateTrialNoticeSchedule.isAlreadyAuthorized(.provisional))
        XCTAssertFalse(IslandGateTrialNoticeSchedule.isAlreadyAuthorized(.notDetermined))
        XCTAssertFalse(IslandGateTrialNoticeSchedule.isAlreadyAuthorized(.denied))
    }
}
