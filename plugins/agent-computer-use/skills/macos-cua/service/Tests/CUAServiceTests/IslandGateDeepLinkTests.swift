import XCTest
@testable import CUAService

final class IslandGateDeepLinkTests: XCTestCase {
    func testRouteAcceptsUnlockOnly() {
        XCTAssertEqual(IslandGateDeepLink.route(URL(string: "acu-island://unlock")!), .unlock)
        XCTAssertEqual(IslandGateDeepLink.route(URL(string: "ACU-ISLAND://unlock")!), .unlock)
        XCTAssertNil(IslandGateDeepLink.route(URL(string: "acu-island://auth")!))
        XCTAssertNil(IslandGateDeepLink.route(URL(string: "acu-island://unlock/extra")!))
        XCTAssertNil(IslandGateDeepLink.route(URL(string: "https://example.com")!))
    }

    @MainActor
    func testHandleUnlockOpensGateWhenLocked() {
        let gate = IslandGate(fixtureState: .locked())
        IslandGateDeepLink.handle(URL(string: "acu-island://unlock")!, gate: gate)
        XCTAssertNil(gate.deepLinkBanner)
        XCTAssertNotNil(gate.state)
        guard case .locked = gate.state else { return XCTFail("Expected locked") }
    }

    @MainActor
    func testHandleUnlockSetsBannerWhenAlreadyUnlocked() throws {
        let account = IslandGateAccount(id: "a", provider: "x", handle: "@you")
        let gate = IslandGate(fixtureState: .unlocked(account: account, verified: false, exp: Date().addingTimeInterval(3600)))
        IslandGateDeepLink.handle(URL(string: "acu-island://unlock")!, gate: gate)
        XCTAssertEqual(gate.deepLinkBanner, "Your island is already unlocked.")
    }
}
