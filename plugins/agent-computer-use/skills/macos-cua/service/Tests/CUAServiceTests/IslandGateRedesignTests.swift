import XCTest
import AppKit
import CryptoKit
@testable import CUAService

@MainActor
final class IslandGateRedesignTests: XCTestCase {
    func testStepsRequireTapAndReactivationAndPersist() {
        let suite = "island-gate-test-\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let gate = IslandGate(urlOpener: IslandGateRecordingURLOpener(), defaults: defaults)
        XCTAssertEqual(gate.activeStep, 1)
        gate.didBecomeActive()
        XCTAssertFalse(gate.followDone)
        gate.openFollow()
        XCTAssertFalse(gate.followDone)
        gate.didBecomeActive()
        XCTAssertEqual(gate.activeStep, 2)
        gate.openShare(); gate.didBecomeActive()
        XCTAssertEqual(gate.activeStep, 3)
        let restored = IslandGate(urlOpener: IslandGateRecordingURLOpener(), defaults: defaults)
        XCTAssertTrue(restored.followDone); XCTAssertTrue(restored.postDone)
        restored.handleText = "@example"
        restored.startOver()
        XCTAssertEqual(restored.activeStep, 1); XCTAssertEqual(restored.handleText, "")
        XCTAssertFalse(defaults.bool(forKey: "islandGate.followDone"))
    }
    func testFixtureStates() throws {
        let start = try IslandGateFixtures.gate("01-start")
        XCTAssertEqual(start.activeStep, 1); XCTAssertFalse(start.isHandleValid)
        let followed = try IslandGateFixtures.gate("02-followed")
        XCTAssertEqual(followed.activeStep, 2)
        let ready = try IslandGateFixtures.gate("03-ready")
        XCTAssertEqual(ready.activeStep, 3); XCTAssertTrue(ready.isHandleValid)
        let checking = try IslandGateFixtures.gate("04-checking")
        guard case .checking(_, _, .confirmed, .pending) = checking.state else { return XCTFail("wrong checking state") }
        checking.changeInput()
        XCTAssertTrue(checking.focusInput)
        guard case .locked = checking.state else { return XCTFail("change must return to form") }
        let linkedIn = try IslandGateFixtures.gate("09-linkedin")
        XCTAssertEqual(linkedIn.lockedProvider, "linkedin")
        XCTAssertFalse(linkedIn.isHandleValid)
        let unlocked = try IslandGateFixtures.gate("05-unlocked")
        XCTAssertTrue(unlocked.isUnlocked)
        let offline = try IslandGateFixtures.gate("06-error-offline")
        guard case .error(let offlineCode, _) = offline.state else { return XCTFail("expected offline") }
        XCTAssertEqual(offlineCode, "offline")
        let missing = try IslandGateFixtures.gate("07-error-notfound")
        guard case .error(let missingCode, _) = missing.state else { return XCTFail("expected not found") }
        XCTAssertEqual(missingCode, "post_not_found")
        XCTAssertEqual(try IslandGateFixtures.gate("08-waking").state, .waking)
    }
    func testActivationNotificationCompletesOnlyTappedSteps() async throws {
        let gate = IslandGate(urlOpener: IslandGateRecordingURLOpener())
        gate.openFollow()
        NotificationCenter.default.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        for _ in 0..<20 where !gate.followDone { await Task.yield() }
        XCTAssertTrue(gate.followDone)
        XCTAssertFalse(gate.postDone)
    }
    func testStartOverDoesNotResumeAnOldClaim() throws {
        let store = IslandGateMemoryStore()
        let gate = IslandGate(store: store, urlOpener: IslandGateRecordingURLOpener())
        try gate.armCheckingClaimForTests(id: "old-claim")
        XCTAssertNotNil(store.values["claim-id"])
        gate.startOver()
        XCTAssertNil(store.values["claim-id"])
        guard case .locked = gate.state else { return XCTFail("expected form") }
    }
    func testPostAgainFits() {
        let font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        let width = NSAttributedString(string: "Post again", attributes: [.font: font]).size().width
        XCTAssertLessThanOrEqual(width, 84)
    }
    func testWallCaptionsFitOneLine() {
        // 440 window - 2 x 24 insets - 22 pt checkbox inset
        let font = NSFont.systemFont(ofSize: 11)
        for caption in ["Your name and photo on the Agent Plugins site. Turn off any time.", "Your handle and avatar on the Agent Plugins site. Turn off any time."] {
            XCTAssertLessThanOrEqual(NSAttributedString(string: caption, attributes: [.font: font]).size().width, 370, caption)
        }
    }
    func testWakingWaitsForInjectedTimer() async throws {
        var release: CheckedContinuation<Void, Error>?
        let device = IslandGateDevice(id: String(repeating: "d", count: 64), key: P256.Signing.PrivateKey())
        let gate = IslandGate(store: IslandGateMemoryStore(), client: IslandGateURLProtocol.client(), device: device,
                              wakeSleep: { try await withCheckedThrowingContinuation { release = $0 } })
        gate.setClientTimingForTests(requestTimeout: 2, coldStartBudget: 3)
        IslandGateURLProtocol.handler = { _ in
            Thread.sleep(forTimeInterval: 0.3)
            return (201, Data(#"{"claim_id":"c1","status":"pending_verification","checks":{"follow":"pending","post":"pending"}}"#.utf8))
        }
        let finished = expectation(description: "claim completed")
        gate.onChange.append { if case .checking = gate.state { finished.fulfill() } }
        defer { gate.stopPollingForTests(); IslandGateURLProtocol.handler = nil }
        gate.handleText = "@example"
        gate.submitCheck()
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(gate.state, .submitting)
        release?.resume(); release = nil
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(gate.state, .waking)
        await fulfillment(of: [finished], timeout: 3)
        guard case .checking = gate.state else { return XCTFail("expected checking, got \(gate.state)") }
    }
}
