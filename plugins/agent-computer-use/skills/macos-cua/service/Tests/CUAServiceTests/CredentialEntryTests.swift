import XCTest
@testable import CUAService

@MainActor
final class CredentialEntryTests: XCTestCase {
    let fields: [String: Any] = ["id": "sample-login", "label": "Sample login", "service": "samantha.test", "account": "test"]

    func testRawValuesAndConcurrentRequestsAreRejected() {
        let c = CredentialEntryController()
        XCTAssertEqual(c.request(fields)["status"] as? String, "pending")
        XCTAssertEqual(c.request(fields)["error"] as? String, "credential_entry_busy")
        c.cancelActive()
        XCTAssertEqual(c.request(fields.merging(["value": "fixture"]) { _, new in new })["ok"] as? Bool, false)
    }

    func testSaveReturnsOnlyLocatorAndCannotBeReplayed() {
        let c = CredentialEntryController()
        var count = 0
        c.write = { request, account, value in
            XCTAssertEqual(request.service, "samantha.test")
            XCTAssertEqual(account, "filled-account")
            XCTAssertEqual(value, "fixture-only")
            count += 1
        }
        let id = c.request(fields)["request_id"] as! String
        XCTAssertNil(c.save(id, account: "filled-account", value: "fixture-only"))
        let status = c.status(id)
        XCTAssertEqual(status["status"] as? String, "saved")
        XCTAssertEqual(status["account"] as? String, "filled-account")
        XCTAssertNil(status["value"])
        XCTAssertFalse(String(describing: status).contains("fixture-only"))
        XCTAssertNotNil(c.save(id, account: "other", value: "fixture-only"))
        XCTAssertEqual(count, 1)
    }

    func testCancelAndStaleSaveDoNotWrite() {
        let c = CredentialEntryController()
        c.write = { _, _, _ in XCTFail("Cancelled request wrote Keychain") }
        let id = c.request(fields)["request_id"] as! String
        XCTAssertEqual(c.cancel(id)["status"] as? String, "cancelled")
        XCTAssertNotNil(c.save(id, account: "test", value: "fixture-only"))
        XCTAssertEqual(c.status("wrong")["ok"] as? Bool, false)
    }

    func testFailedReplacementKeepsRequestForDeliberateRetry() {
        let c = CredentialEntryController()
        var existing = "old-fixture"
        c.write = { _, _, _ in throw NSError(domain: "fixture", code: 1) }
        let id = c.request(fields)["request_id"] as! String
        XCTAssertNotNil(c.save(id, account: "test", value: "new-fixture"))
        XCTAssertEqual(existing, "old-fixture")
        XCTAssertEqual(c.status(id)["status"] as? String, "pending")
        c.write = { _, _, value in existing = value }
        XCTAssertNil(c.save(id, account: "test", value: "new-fixture"))
        XCTAssertEqual(existing, "new-fixture")
    }

    func testTimeoutInvalidatesSave() async throws {
        let c = CredentialEntryController()
        let id = c.request(fields, timeout: 0.01)["request_id"] as! String
        try await Task.sleep(nanoseconds: 40_000_000)
        XCTAssertEqual(c.status(id)["status"] as? String, "expired")
        XCTAssertNotNil(c.save(id, account: "test", value: "fixture-only"))
    }

    func testCredentialCardSurvivesHUDAndTextEntry() {
        let m = IslandModel()
        let request = CredentialRequest(id: "test", catalogID: "sample", label: "Sample", service: "samantha.test", account: "")
        m.setCredentialRequest(request)
        m.showHUD(IslandHUDState(kind: .volume, value: 0.5, muted: false))
        XCTAssertTrue(m.cardOpen)
        XCTAssertEqual(m.credentialRequest, request)
        XCTAssertEqual(m.presentation, .needsYou)
        XCTAssertEqual(m.cardHeight, 292)
        m.beginTextEntry()
        XCTAssertTrue(m.textEntryActive)
        m.setCredentialRequest(nil)
        XCTAssertFalse(m.textEntryActive)
    }

    func testHUDBarFitsEveryPinchedWidthAndDoesNotInheritOpenCard() {
        for width in stride(from: 74.0, through: 200.0, by: 2) {
            let bar = IslandBarLayout.hudTrailWidth(shapeWidth: width)
            XCTAssertLessThanOrEqual(bar + 2 * IslandBarLayout.slotInset + 16 + 8, width)
        }
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "listening", title: "Listening"))
        m.toggleCard()
        XCTAssertTrue(m.cardOpen)
        m.showHUD(IslandHUDState(kind: .volume, value: 0.5, muted: false))
        XCTAssertEqual(m.presentation, .hud)
        XCTAssertFalse(m.cardOpen)
    }

    func testUnverifiedCopyOffersInspectionInsteadOfReplay() {
        XCTAssertEqual(IslandCopy.humanError("expect_unverified"), "Action not confirmed")
        let card = IslandModel.card(for: .init(presentation: .error, title: "Action not confirmed", detail: "", app: "Finder", stepIndex: 0, stepTotal: 0, approval: nil))
        guard case .decision(_, let primary, _) = card.controls else { return XCTFail("Expected inspection control") }
        XCTAssertEqual(primary.1, .show("Finder"))
    }
}

@MainActor
final class SystemVolumeTests: XCTestCase {
    override func tearDown() { SystemVolume.onChange = nil }
    func testVolumeChangeMustReadBack() async {
        var level = 0.5
        let result = await SystemVolume.adjust("up", read: { level }, write: { level = $0; return true }, muted: { false }, unmute: { true })
        XCTAssertEqual(result["verified"] as? Bool, true)
        XCTAssertEqual(result["level"] as? Double, 0.5625)
        let failure = await SystemVolume.adjust("up", read: { 0.5 }, write: { _ in true }, muted: { false }, unmute: { true })
        XCTAssertEqual(failure["ok"] as? Bool, false)
        XCTAssertEqual(failure["error_type"] as? String, "volume_unverified")
    }
    func testVolumeLimitsAreExplicitAndWriteFailuresAreNotSuccess() async {
        let limit = await SystemVolume.adjust("up", read: { 1.0 }, write: { _ in true }, muted: { false }, unmute: { true })
        XCTAssertEqual(limit["at_limit"] as? Bool, true)
        let fail = await SystemVolume.adjust("down", read: { 0.5 }, write: { _ in false }, muted: { false }, unmute: { true })
        XCTAssertEqual(fail["ok"] as? Bool, false)
    }
}
