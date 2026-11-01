import XCTest
@testable import CUAService

@MainActor
final class IslandGateDelta6Tests: XCTestCase {
    func testLockedStepsRequireAValidHandleAndAdvanceAfterFollow() throws {
        let empty = try IslandGateFixtures.gate("locked-x")
        XCTAssertFalse(empty.followStepActive)
        XCTAssertFalse(empty.canOpenRepost)
        empty.handleText = "bad handle"
        XCTAssertFalse(empty.followStepActive)
        XCTAssertFalse(empty.canOpenRepost)
        empty.handleText = "@example"
        XCTAssertTrue(empty.followStepActive)
        XCTAssertTrue(empty.canOpenRepost)
        XCTAssertFalse(empty.repostStepActive)
        let ready = try IslandGateFixtures.gate("locked-x-ready")
        XCTAssertTrue(ready.followStepDone)
        XCTAssertFalse(ready.followStepActive)
        XCTAssertTrue(ready.repostStepActive)
    }

    func testExpiredTrialKeepsFollowDoneAndHandleReadOnlyUntilChange() throws {
        let gate = try IslandGateFixtures.gate("trial-expired")
        XCTAssertTrue(gate.followStepDone)
        XCTAssertFalse(gate.followStepActive)
        XCTAssertTrue(gate.repostStepActive)
        XCTAssertFalse(gate.isEditingHandle)
        XCTAssertFalse(gate.focusInput)
        gate.editHandle()
        XCTAssertTrue(gate.isEditingHandle)
        XCTAssertTrue(gate.focusInput)
    }

    func testClaimReportedUnfollowReactivatesFollowAndConfirmationRestoresRepost() throws {
        let gate = try IslandGateFixtures.gate("trial-expired")
        for status in ["not_following", "unfollowed", "failed"] {
            let reply = try JSONDecoder().decode(IslandGateClaimResponse.self, from: Data("{\"status\":\"expired\",\"checks\":{\"follow\":\"\(status)\",\"post\":\"pending\"}}".utf8))
            try gate.applyClaim(reply, since: Date())
            XCTAssertTrue(gate.followNeedsAttention)
            XCTAssertTrue(gate.followStepActive)
            XCTAssertFalse(gate.followStepDone)
            XCTAssertFalse(gate.repostStepActive)
        }
        let reply = try JSONDecoder().decode(IslandGateClaimResponse.self, from: Data(#"{"status":"expired","checks":{"follow":"confirmed","post":"pending"}}"#.utf8))
        try gate.applyClaim(reply, since: Date())
        XCTAssertFalse(gate.followNeedsAttention)
        XCTAssertTrue(gate.followStepDone)
        XCTAssertTrue(gate.repostStepActive)
        let unfollowed = try IslandGateFixtures.gate("trial-expired-unfollowed")
        XCTAssertTrue(unfollowed.followNeedsAttention)
        XCTAssertTrue(unfollowed.followStepActive)
    }

    func testPrelaunchNeverOffersRepost() throws {
        for state in ["locked-x-prelaunch", "trial-prelaunch"] {
            let gate = try IslandGateFixtures.gate(state)
            XCTAssertFalse(gate.isLaunchConfigured(for: "x"))
            XCTAssertFalse(gate.canOpenRepost)
            XCTAssertFalse(gate.repostStepActive)
        }
    }

    func testOnlyLastDayHasANudgeAndPrelaunchNeverDoes() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertNil(IslandGateView.trialNudge(ends: now.addingTimeInterval(36 * 3600), launchConfigured: true, now: now))
        XCTAssertNil(IslandGateView.trialNudge(ends: now.addingTimeInterval(24 * 3600), launchConfigured: true, now: now))
        XCTAssertEqual(IslandGateView.trialNudge(ends: now.addingTimeInterval(12 * 3600), launchConfigured: true, now: now), "Your trial ends today. Repost to keep your island for good.")
        XCTAssertNil(IslandGateView.trialNudge(ends: now.addingTimeInterval(12 * 3600), launchConfigured: false, now: now))
        XCTAssertNil(IslandGateView.trialNudge(ends: now, launchConfigured: true, now: now))
    }

    func testUnfocusedLinkedInIdentityUsesCompactProfilePath() {
        for input in ["islandfriend", "https://www.linkedin.com/in/islandfriend/", "https://ca.linkedin.com/in/islandfriend/"] {
            XCTAssertEqual(IslandGateView.compactLinkedIn(input), "linkedin.com/in/islandfriend")
        }
        XCTAssertEqual(IslandGateView.compactLinkedIn("bad input"), "bad input")
    }
}
