import XCTest
@testable import CUAService

final class ScreenAvailabilityTests: XCTestCase {
    func testInjectedLockAndSleepState() {
        let locked = MethodRouter.screenFailure(sessionDictionary: { ["CGSSessionScreenIsLocked": true] }, displayIsAsleep: { false })
        XCTAssertEqual(locked?.errorType, "screen_locked")
        XCTAssertEqual(locked?.hint, "Mac is locked; ask the user to unlock")
        let asleep = MethodRouter.screenFailure(sessionDictionary: { [:] }, displayIsAsleep: { true })
        XCTAssertEqual(asleep?.errorType, "display_asleep")
        XCTAssertEqual(asleep?.hint, "Display is asleep; wake it")
        XCTAssertNil(MethodRouter.screenFailure(sessionDictionary: { [:] }, displayIsAsleep: { false }))
    }
}
