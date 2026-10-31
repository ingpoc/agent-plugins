#if DEBUG
import XCTest
@testable import CUAService

final class IslandGateDemoTests: XCTestCase {
    func testRequiresExplicitDevelopmentOptIn() {
        XCTAssertThrowsError(try IslandGateDemoCommand.requireDev([:]))
        XCTAssertThrowsError(try IslandGateDemoCommand.requireDev(["ACU_GATE_DEV": "0"]))
        XCTAssertNoThrow(try IslandGateDemoCommand.requireDev(["ACU_GATE_DEV": "1"]))
    }
}
#endif
