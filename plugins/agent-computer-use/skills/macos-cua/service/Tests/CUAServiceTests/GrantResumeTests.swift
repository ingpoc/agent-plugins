import AppKit
import XCTest
@testable import CUAService

/// Grant → System Settings → back: the surface survives deactivation and a relaunch, and is
/// forgotten once everything is granted or the user closes the window.
@MainActor
final class GrantResumeTests: XCTestCase {
    private var d: UserDefaults!

    override func setUp() {
        d = UserDefaults(suiteName: "GrantResumeTests-\(UUID().uuidString)")
    }

    func testSurfaceRoundTripsAcrossRelaunch() {
        for s in [GrantSurface.onboarding, .settings(.computerUse), .settings(.samantha)] {
            s.save(d)
            // A relaunch only has the defaults domain.
            XCTAssertEqual(GrantSurface.load(d), s)
        }
        GrantSurface.clear(d)
        XCTAssertNil(GrantSurface.load(d))
        XCTAssertNil(GrantSurface(encoded: "settings:nope"))
    }

    func testFinishedOnlyWhenNothingPending() {
        var s = Dictionary(uniqueKeysWithValues: ACUPermission.allCases.map { ($0, PermissionState.granted) })
        XCTAssertTrue(GrantSurface.finished(s))
        let optional = ACUPermission.allCases.first { !$0.required && $0.bundleID != nil }!
        s[optional] = .missing           // e.g. Finder still pending → keep reopening
        XCTAssertFalse(GrantSurface.finished(s))
        s[optional] = .notRunning        // can't be read; not worth nagging
        XCTAssertTrue(GrantSurface.finished(s))
        let req = ACUPermission.allCases.first { $0.required }!
        s[req] = .missing
        XCTAssertFalse(GrantSurface.finished(s))
    }

    func testWindowsNeverHideOnDeactivate() throws {
        for w in [IslandSettingsWindowController.shared.window, OnboardingWindowController.shared.window] {
            let w = try XCTUnwrap(w)
            XCTAssertFalse(w.hidesOnDeactivate)
            XCTAssertFalse(w.isReleasedWhenClosed)
            XCTAssertFalse(w.canHide)
        }
    }

    func testCloseClearsButQuitDoesNot() {
        let r = GrantResume.shared
        let old = r.defaults
        r.defaults = d
        defer { r.defaults = old; r.terminating = false }
        GrantSurface.settings(.computerUse).save(d)
        r.terminating = true
        r.userClosed()
        XCTAssertEqual(GrantSurface.load(d), .settings(.computerUse), "Quit & Reopen must keep the pane")
        r.terminating = false
        r.userClosed()
        XCTAssertNil(GrantSurface.load(d))
    }
}
