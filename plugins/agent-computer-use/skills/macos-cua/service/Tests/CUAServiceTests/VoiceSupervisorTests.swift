import XCTest
@testable import CUAService

@MainActor
final class VoiceSupervisorTests: XCTestCase {
    func testGatewayHealthWaitReturnsWhenProcessIsMissing() async {
        let supervisor = VoiceSupervisor(gatewayPort: 0)
        let healthy = await supervisor.waitForGatewayHealth(timeout: 1)
        XCTAssertFalse(healthy)
    }

    func testStartupTimeoutIsIgnoredAfterStopOrProcessExit() {
        XCTAssertFalse(VoiceSupervisor.isStartupProcessActive(stopping: true, isCurrent: true, isRunning: true))
        XCTAssertFalse(VoiceSupervisor.isStartupProcessActive(stopping: false, isCurrent: false, isRunning: true))
        XCTAssertFalse(VoiceSupervisor.isStartupProcessActive(stopping: false, isCurrent: true, isRunning: false))
        XCTAssertTrue(VoiceSupervisor.isStartupProcessActive(stopping: false, isCurrent: true, isRunning: true))
    }
}
