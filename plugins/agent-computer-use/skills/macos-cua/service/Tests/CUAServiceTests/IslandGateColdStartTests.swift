import XCTest
import CryptoKit
@testable import CUAService

@MainActor
final class IslandGateColdStartTests: XCTestCase {
    override func tearDown() {
        IslandGateURLProtocol.handler = nil
        super.tearDown()
    }
    private let signingKey = Curve25519.Signing.PrivateKey()
    private let device = IslandGateDevice(id: String(repeating: "d", count: 64), key: P256.Signing.PrivateKey())

    private func makeGate(store: IslandGateMemoryStore = .init()) -> IslandGate {
        let client = IslandGateURLProtocol.client()
        client.requestTimeout = 0.05
        client.coldStartBudget = 0.35
        client.retryBackoffBase = 0.05
        return IslandGate(store: store, client: client, publicKey: signingKey.publicKey.rawRepresentation, device: device, wakeSleep: { try await Task.sleep(nanoseconds: 10_000_000) })
    }

    private func signedEntitlement(tier: String?, exp: Date) throws -> String {
        let payload = try JSONEncoder().encode(IslandGateEntitlement(v: 1, acct: "account", prov: "x", handle: "friend", device_id: device.id,
            iat: Date().timeIntervalSince1970 - 1, exp: exp.timeIntervalSince1970, tier: tier)).gateBase64URL
        return payload + "." + (try signingKey.signature(for: Data(payload.utf8))).gateBase64URL
    }

    private static func configPayload(eta: String? = "usually within the hour") -> Data {
        var root: [String: Any] = [
            "x": ["handle": "guru", "profile_url": "https://x.com/guru", "intent_url": "https://x.com/intent/post?text=server", "preset_text": "server"],
            "linkedin": ["share_url": "https://www.linkedin.com/sharing/share-offsite/?url=server", "preset_text": "from server", "follow_url": "https://www.linkedin.com/in/example"],
            "poll_seconds": 60, "grace_days": 14
        ]
        if let eta { root["check_eta_text"] = eta }
        return try! JSONSerialization.data(withJSONObject: root)
    }

    func testBundledConfigMatchesPreset() throws {
        var root = URL(fileURLWithPath: #filePath)
        while root.path != "/" && !FileManager.default.fileExists(atPath: root.appendingPathComponent("services/acu-gate/config/preset.json").path) {
            root.deleteLastPathComponent()
        }
        let file = root.appendingPathComponent("services/acu-gate/config/preset.json")
        guard FileManager.default.fileExists(atPath: file.path) else { throw XCTSkip("Repository preset unavailable") }
        let preset = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        let x = try XCTUnwrap(preset["x"] as? [String: Any])
        let linkedIn = try XCTUnwrap(preset["linkedin"] as? [String: Any])
        let config = try JSONDecoder().decode(IslandGateConfig.self, from: Data(contentsOf: file))
        XCTAssertEqual(config.x.handle, x["handle"] as? String)
        XCTAssertEqual(config.x.profile_url.absoluteString, "https://x.com/\(x["handle"] as! String)")
        XCTAssertEqual(config.x.preset_text, x["preset_text"] as? String)
        XCTAssertEqual(config.linkedin.preset_text, linkedIn["preset_text"] as? String)
        XCTAssertEqual(config.linkedInFollowURL?.absoluteString, linkedIn["follow_url"] as? String)
        XCTAssertEqual(config.poll_seconds, nil)
        XCTAssertEqual(config.check_eta_text, preset["check_eta_text"] as? String)
        XCTAssertEqual(config.trial_hours, preset["trial_hours"] as? Int)
        XCTAssertEqual(config.trial_eta_text, preset["trial_eta_text"] as? String)
    }

    func testFollowActionsOpenConfiguredProfilesWithoutNetwork() {
        let opener = IslandGateRecordingURLOpener()
        let gate = IslandGate(store: IslandGateMemoryStore(), client: IslandGateURLProtocol.client(), device: device, urlOpener: opener)
        IslandGateURLProtocol.handler = { _ in XCTFail("Unexpected network request"); return (503, Data()) }
        gate.openFollow()
        XCTAssertEqual(opener.opened, [IslandGateConfig.bundled.x.profile_url])
        gate.useLinkedInRoute()
        gate.openFollow()
        XCTAssertEqual(Array(opener.opened.suffix(1)), [URL(string: "https://www.linkedin.com/in/gurusharangupta/")!])
        XCTAssertEqual(opener.opened.count, 2)
        XCTAssertEqual((gate.pasteboard as? IslandGateRecordingPasteboard)?.written, [])
    }

    func testPrefetchRetriesWithoutChangingStateAndReplacesDefault() async throws {
        let gate = makeGate()
        var attempts = 0
        let updated = expectation(description: "Server config loaded")
        gate.onChange.append {
            if gate.config?.x.handle == "guru" { updated.fulfill() }
        }
        IslandGateURLProtocol.handler = { _ in
            attempts += 1
            if attempts < 3 { return (503, Data()) }
            return (200, Self.configPayload())
        }
        gate.opened()
        try await Task.sleep(nanoseconds: 25_000_000)
        XCTAssertEqual(gate.state, .locked())
        XCTAssertEqual(gate.config?.x.handle, "gurusharan")
        await fulfillment(of: [updated], timeout: 2)
        XCTAssertEqual(gate.state, .locked())
        XCTAssertEqual(attempts, 3)
    }

    func testCheckETATextUsesConfigThenFallback() throws {
        let gate = makeGate()
        XCTAssertEqual(gate.checkETAText, "usually within the hour")
        gate.setConfigForTests(try JSONDecoder().decode(IslandGateConfig.self, from: Self.configPayload(eta: "usually a few minutes")))
        XCTAssertEqual(gate.checkETAText, "usually a few minutes")
    }

    func testLegacyConfigDecodesWithoutTrialAndRepostFields() throws {
        let config = try JSONDecoder().decode(IslandGateConfig.self, from: Self.configPayload())
        XCTAssertNil(config.x.launch_post_id)
        XCTAssertNil(config.x.repost_url)
        XCTAssertNil(config.linkedin.launch_post_url)
        XCTAssertNil(config.linkedin.repost_url)
        XCTAssertNil(config.trial_hours)
        XCTAssertNil(config.trial_eta_text)
    }

    func testWindowOpeningRefreshesConfigForTrialAndExpiredStates() async throws {
        for expired in [false, true] {
            let gate = makeGate()
            if expired {
                gate.transition(.trialExpired(account: IslandGateAccount(id: "account", provider: "x", handle: "friend")))
            } else {
                try gate.applyEntitlementForTests(signedEntitlement(tier: "trial", exp: Date().addingTimeInterval(72 * 3600)))
            }
            let updated = expectation(description: expired ? "expired config" : "trial config")
            gate.onChange.append { if gate.config?.x.handle == "guru" { updated.fulfill() } }
            IslandGateURLProtocol.handler = { _ in (200, Self.configPayload()) }
            gate.opened()
            await fulfillment(of: [updated], timeout: 2)
            XCTAssertEqual(gate.config?.x.handle, "guru")
            gate.closed()
        }
    }

    func testSubmitColdStartShowsWakingThenSucceeds() async throws {
        let gate = makeGate()
        gate.handleText = "@friend"
        var attempts = 0
        var sawWaking = false
        gate.onChange.append { if case .waking = gate.state { sawWaking = true } }
        IslandGateURLProtocol.handler = { req in
            if req.url?.path == "/v1/claims" {
                attempts += 1
                if attempts < 3 { return (503, Data(#"{"error":"server_error"}"#.utf8)) }
                return (201, Data(#"{"claim_id":"c1","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":60}"#.utf8))
            }
            return (200, Self.configPayload())
        }
        gate.submitCheck()
        try await Task.sleep(nanoseconds: 500_000_000)
        XCTAssertEqual(attempts, 3)
        XCTAssertFalse(sawWaking, "A quick first response must not show the waking state")
        guard case .pendingFollow = gate.state else {
            if case .waking = gate.state { /* still retrying */ }
            return XCTFail("Expected pending follow, got \(gate.state)")
        }
        gate.stopPollingForTests()
    }

    func testSubmitColdStartBudgetShowsRetryError() async throws {
        let gate = makeGate()
        gate.handleText = "@friend"
        IslandGateURLProtocol.handler = { _ in (503, Data(#"{"error":"server_error"}"#.utf8)) }
        gate.submitCheck()
        try await Task.sleep(nanoseconds: 600_000_000)
        guard case .error(_, let message) = gate.state else { return XCTFail("Expected error after wake budget") }
        XCTAssertTrue(message.contains("waking up"))
    }

    func testPollIgnoresTransientFailure() async throws {
        let gate = makeGate()
        try gate.armCheckingClaimForTests(id: "claim-9")
        IslandGateURLProtocol.handler = { _ in (503, Data(#"{"error":"server_error"}"#.utf8)) }
        await gate.pollOnce()
        guard case .checking = gate.state else { return XCTFail("Poll must keep checking") }
    }

    func testCleanMacFirstRunSubmitAndRelaunchResume() async throws {
        let store = IslandGateMemoryStore()
        let gate = makeGate(store: store)
        gate.start()
        guard case .locked = gate.state else { return XCTFail("Fresh store must start locked") }
        gate.handleText = "@friend"
        gate.setConfigForTests(try JSONDecoder().decode(IslandGateConfig.self, from: Self.configPayload()))
        let submitted = expectation(description: "Claim created")
        let pendingFollow = expectation(description: "Starting trial verification")
        gate.onChange.append {
            if case .pendingFollow = gate.state { pendingFollow.fulfill() }
        }
        IslandGateURLProtocol.handler = { req in
            guard req.url?.path == "/v1/claims" else { return (200, Self.configPayload()) }
            submitted.fulfill()
            return (201, Data(#"{"claim_id":"fresh-claim","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":60}"#.utf8))
        }
        gate.submitCheck()
        await fulfillment(of: [submitted, pendingFollow], timeout: 3)
        XCTAssertNotNil(store.values["claim-id"])
        let relaunch = makeGate(store: store)
        relaunch.start()
        guard case .pendingFollow = relaunch.state else { return XCTFail("Relaunch must resume trial verification") }
        let id = String(data: try XCTUnwrap(store.values["claim-id"]), encoding: .utf8)
        XCTAssertEqual(id, "fresh-claim")
        gate.stopPollingForTests()
        relaunch.stopPollingForTests()
    }
    func testInvalidHandleCannotSubmit() {
        let gate = makeGate()
        for value in ["", "@", "bad handle", String(repeating: "a", count: 16)] {
            gate.handleText = value
            XCTAssertFalse(gate.isHandleValid)
            gate.submitCheck()
            guard case .locked = gate.state else { return XCTFail("Invalid handle changed state") }
        }
        gate.handleText = "@valid_name"
        XCTAssertTrue(gate.isHandleValid)
    }

    func testLinkedInClaimSendsHandleWithoutPostURL() async throws {
        let gate = makeGate()
        gate.useLinkedInRoute()
        gate.handleText = "uk.linkedin.com/in/Some-Name"
        XCTAssertTrue(gate.isHandleValid)
        let submitted = expectation(description: "LinkedIn claim submitted")
        let checking = expectation(description: "claim accepted")
        gate.onChange.append { if case .checking = gate.state { checking.fulfill() } }
        IslandGateURLProtocol.handler = { request in
            let data = try IslandGateClientTests.body(request)
            let body = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            XCTAssertEqual(body?["provider"] as? String, "linkedin")
            XCTAssertEqual(body?["handle"] as? String, "some-name")
            XCTAssertNil(body?["post_url"])
            submitted.fulfill()
            return (201, Data(#"{"claim_id":"li-claim","status":"pending_verification","poll_after":60}"#.utf8))
        }
        gate.submitCheck()
        await fulfillment(of: [submitted, checking], timeout: 3)
        guard case .checking = gate.state else { return XCTFail("Unexpected state: \(gate.state)") }
        gate.stopPollingForTests()
    }

    func testFirstPollWaitsForClaimInterval() async throws {
        let gate = makeGate()
        gate.handleText = "friend"
        gate.setConfigForTests(try JSONDecoder().decode(IslandGateConfig.self, from: Self.configPayload()))
        let pendingFollow = expectation(description: "Claim accepted")
        let polled = expectation(description: "Poll after interval")
        var didPoll = false
        gate.onChange.append {
            if case .pendingFollow = gate.state { pendingFollow.fulfill() }
        }
        IslandGateURLProtocol.handler = { request in
            if request.url?.path == "/v1/claims" {
                return (201, Data(#"{"claim_id":"wait-claim","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":1}"#.utf8))
            }
            if !didPoll { didPoll = true; polled.fulfill() }
            return (200, Data(#"{"status":"pending_verification","checks":{"follow":"pending","post":"pending"}}"#.utf8))
        }
        gate.submitCheck()
        await fulfillment(of: [pendingFollow], timeout: 2)
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertFalse(didPoll)
        await fulfillment(of: [polled], timeout: 2)
        gate.stopPollingForTests()
    }

}
