import CoreGraphics
import XCTest
@testable import CUAService

/// Window lists captured on macOS (Stage Manager on, Dock auto-hide on) with the Dock shown and in Mission Control.
final class IslandEnvironmentTests: XCTestCase {
    private let display = CGRect(x: 0, y: 0, width: 1920, height: 1080)

    private func win(_ owner: String, _ name: String?, _ layer: Int, _ r: CGRect) -> [String: Any] {
        var w: [String: Any] = [
            kCGWindowOwnerName as String: owner,
            kCGWindowLayer as String: layer,
            kCGWindowBounds as String: ["X": r.minX, "Y": r.minY, "Width": r.width, "Height": r.height] as [String: CGFloat],
        ]
        if let name { w[kCGWindowName as String] = name }
        return w
    }

    func testShownDockIsNotMissionControl() {
        let dock = win("Dock", "Dock", 20, display)
        XCTAssertFalse(IslandEnvironment.missionControlActive(windows: [dock], displays: [display]))
        // Same window without Screen Recording (no window names): still the Dock level, still not Mission Control.
        let unnamed = win("Dock", nil, IslandEnvironment.dockWindowLevel, display)
        XCTAssertFalse(IslandEnvironment.missionControlActive(windows: [unnamed], displays: [display]))
    }

    func testExposeShieldIsMissionControl() {
        let windows = [
            win("Dock", "Dock", 20, display),
            win("WindowManager", "ExposeShieldWindow", 19, display),
            win("WindowManager", "Spaces Bar", 14, CGRect(x: 0, y: 0, width: 1920, height: 96)),
        ]
        XCTAssertTrue(IslandEnvironment.missionControlActive(windows: windows, displays: [display]))
        let unnamed = [win("WindowManager", nil, 19, display)]
        XCTAssertTrue(IslandEnvironment.missionControlActive(windows: unnamed, displays: [display]))
    }

    func testPartialShieldOnOtherDisplayIgnored() {
        let small = win("WindowManager", "ExposeShieldWindow", 19, CGRect(x: 0, y: 0, width: 800, height: 600))
        XCTAssertFalse(IslandEnvironment.missionControlActive(windows: [small], displays: [display]))
    }
}

final class AppResolverSelectorTests: XCTestCase {
    func testPidSelector() {
        XCTAssertEqual(AppResolver.pidSelector("pid:62171"), 62171)
        XCTAssertEqual(AppResolver.pidSelector(" PID:7 "), 7)
        XCTAssertNil(AppResolver.pidSelector("TextEdit"))
        XCTAssertNil(AppResolver.pidSelector("pid:abc"))
        XCTAssertNil(AppResolver.pidSelector("pid:0"))
    }

    func testExtensionBundles() {
        XCTAssertTrue(AppResolver.isExtensionBundle(URL(fileURLWithPath: "/Applications/WhatsApp.app/Contents/PlugIns/ServiceExtension.appex")))
        XCTAssertTrue(AppResolver.isExtensionBundle(URL(fileURLWithPath: "/System/Library/Foo.xpc")))
        XCTAssertFalse(AppResolver.isExtensionBundle(URL(fileURLWithPath: "/Applications/WhatsApp.app")))
        XCTAssertFalse(AppResolver.isExtensionBundle(nil))
    }
}
