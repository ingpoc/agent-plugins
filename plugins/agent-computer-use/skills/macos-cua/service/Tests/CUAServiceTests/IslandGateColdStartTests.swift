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
        return IslandGate(store: store, client: client, publicKey: signingKey.publicKey.rawRepresentation, device: device)
    }

    private static func configPayload(eta: String? = "usually within an hour") -> Data {
        var root: [String: Any] = [
            "x": ["handle": "guru", "profile_url": "https://x.com/guru", "intent_url": "https://x.com/intent/post?text=server", "preset_text": "server"],
            "linkedin": ["share_url": "https://www.linkedin.com/sharing/share-offsite/?url=server", "preset_text": "from server", "follow_url": "https://www.linkedin.com/in/example"],
            "poll_seconds": 60, "grace_days": 14
        ]
        if let eta { root["check_eta_text"] = eta }
        return try! JSONSerialization.data(withJSONObject: root)
    }

    func testCheckETATextUsesConfigThenFallback() throws {
        let gate = makeGate()
        XCTAssertEqual(gate.checkETAText, "usually within an hour")
        gate.setConfigForTests(try JSONDecoder().decode(IslandGateConfig.self, from: Self.configPayload(eta: "usually a few minutes")))
        XCTAssertEqual(gate.checkETAText, "usually a few minutes")
    }

    func testSubmitColdStartShowsWakingThenSucceeds() async throws {
        let gate = makeGate()
        gate.handleText = "@friend"
        var attempts = 0
        IslandGateURLProtocol.handler = { req in
            if req.url?.path == "/v1/claims" {
                attempts += 1
                if attempts < 2 { return (503, Data(#"{"error":"server_error"}"#.utf8)) }
                return (201, Data(#"{"claim_id":"c1","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":60}"#.utf8))
            }
            return (200, Self.configPayload())
        }
        gate.submitCheck()
        try await Task.sleep(nanoseconds: 500_000_000)
        XCTAssertEqual(attempts, 2)
        guard case .checking = gate.state else {
            if case .waking = gate.state { /* still retrying */ }
            return XCTFail("Expected checking, got \(gate.state)")
        }
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
        let checking = expectation(description: "Checking")
        gate.onChange.append {
            if case .checking = gate.state { checking.fulfill() }
        }
        IslandGateURLProtocol.handler = { req in
            guard req.url?.path == "/v1/claims" else { return (200, Self.configPayload()) }
            submitted.fulfill()
            return (201, Data(#"{"claim_id":"fresh-claim","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":60}"#.utf8))
        }
        gate.submitCheck()
        await fulfillment(of: [submitted, checking], timeout: 3)
        XCTAssertNotNil(store.values["claim-id"])
        let relaunch = makeGate(store: store)
        relaunch.start()
        guard case .checking(let id, _, _, _) = relaunch.state else { return XCTFail("Relaunch must resume checking") }
        XCTAssertEqual(id, "fresh-claim")
        gate.stopPollingForTests()
        relaunch.stopPollingForTests()
    }
}
