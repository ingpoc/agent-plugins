import XCTest
import CryptoKit
@testable import CUAService

@MainActor
final class IslandGateRefreshCompatibilityTests: XCTestCase {
    private let signingKey = Curve25519.Signing.PrivateKey()
    private let device = IslandGateDevice(id: String(repeating: "b", count: 64), key: P256.Signing.PrivateKey())

    private func token(expiry: Date = Date().addingTimeInterval(3600), tier: String? = nil) throws -> String {
        var body: [String: Any] = ["v": 1, "acct": "test-account", "prov": "x", "handle": "testfriend",
                                  "device_id": device.id, "iat": Date().timeIntervalSince1970 - 60, "exp": expiry.timeIntervalSince1970]
        if let tier { body["tier"] = tier }
        let payload = try JSONSerialization.data(withJSONObject: body).gateBase64URL
        return payload + "." + (try signingKey.signature(for: Data(payload.utf8))).gateBase64URL
    }
    private func gate(store: IslandGateMemoryStore = .init()) -> IslandGate {
        IslandGate(store: store, client: IslandGateURLProtocol.client(), publicKey: signingKey.publicKey.rawRepresentation,
                   device: device, urlOpener: IslandGateRecordingURLOpener(), pasteboard: IslandGateRecordingPasteboard())
    }
    private func assertDeviceBodyAndSignature(_ request: URLRequest, action: String) throws -> [String: Any] {
        let raw = try IslandGateClientTests.body(request)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: raw) as? [String: Any])
        XCTAssertEqual(body["device_id"] as? String, device.id)
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-ACU-Device"), device.id)
        let timestamp = try XCTUnwrap(request.value(forHTTPHeaderField: "X-ACU-TS").flatMap(Int64.init))
        let signature = try P256.Signing.ECDSASignature(derRepresentation: XCTUnwrap(Data(base64Encoded: XCTUnwrap(request.value(forHTTPHeaderField: "X-ACU-Sig")))))
        let signed = IslandGateDevice.signMessage(action: action, deviceID: device.id, timestamp: timestamp, body: raw)
        XCTAssertTrue(device.key.publicKey.isValidSignature(signature, for: Data(signed.utf8)))
        return body
    }
    func testRefreshMatches0267And0268DeviceBodyContractAndAcceptsLegacyToken() async throws {
        let legacy = try token()
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/entitlement/refresh")
            let body = try self.assertDeviceBodyAndSignature(request, action: "refresh")
            // Both server versions validate the JSON device_id before looking up the binding.
            guard body["device_id"] as? String == self.device.id else {
                return (400, Data(#"{"error":"invalid_request","message":"invalid request"}"#.utf8))
            }
            return (200, try JSONSerialization.data(withJSONObject: ["entitlement": legacy]))
        }
        defer { IslandGateURLProtocol.handler = nil }
        let reply = try await IslandGateURLProtocol.client().refreshEntitlement(device: device)
        XCTAssertEqual(reply.entitlement, legacy)
        let gate = gate()
        try gate.applyEntitlementForTests(legacy)
        await gate.refresh()
        XCTAssertTrue(gate.isUnlocked)
        guard case .unlocked = gate.state else { return XCTFail("legacy token must remain a permanent unlock") }
    }
    func testWallRequestAlsoIncludesDeviceIDInItsSignedBody() async throws {
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/wall")
            let body = try self.assertDeviceBodyAndSignature(request, action: "wall")
            XCTAssertEqual(body["show"] as? Bool, true)
            return (200, Data(#"{"show":true}"#.utf8))
        }
        defer { IslandGateURLProtocol.handler = nil }
        try await IslandGateURLProtocol.client().setWallVisibility(true, device: device)
    }
    func testValidStoredLegacyEntitlementSurvivesOtherRefreshFailures() async throws {
        let responses: [(Int, String)] = [
            (400, #"{"error":"invalid_request"}"#), (401, #"{"error":"bad_signature"}"#),
            (401, #"{"error":"not_found"}"#),
            (403, #"{"error":"unknown_authorization"}"#), (404, #"{"error":"unknown_route"}"#),
            (409, #"{"error":"device_conflict"}"#), (410, #"{"error":"trial_expired"}"#),
            (422, #"{"error":"post_missing"}"#), (429, #"{"error":"rate_limited"}"#),
            (200, "bad JSON"), (200, #"{"entitlement":"bad-token"}"#), (200, #"{}"#)
        ]
        for tier in [String?.none, .some("trial")] {
        for (status, response) in responses {
            let store = IslandGateMemoryStore(), gate = gate(store: store)
            let saved = try token(tier: tier)
            try gate.applyEntitlementForTests(saved)
            IslandGateURLProtocol.handler = { _ in (status, Data(response.utf8)) }
            await gate.refresh()
            XCTAssertTrue(gate.isUnlocked, "HTTP \(status) must not hide verified offline access")
            XCTAssertEqual(store.values["entitlement"], Data(saved.utf8))
            IslandGateURLProtocol.handler = nil
        }
        }
    }
    func testExplicitBindingRemovalOrRevocationStillLocks() async throws {
        for (status, code) in [(403, "revoked"), (409, "device_moved"), (404, "not_found")] {
            let store = IslandGateMemoryStore(), gate = gate(store: store)
            try gate.applyEntitlementForTests(token())
            IslandGateURLProtocol.handler = { _ in (status, try JSONSerialization.data(withJSONObject: ["error": code])) }
            await gate.refresh()
            XCTAssertFalse(gate.isUnlocked, code)
            XCTAssertNil(store.values["entitlement"])
            IslandGateURLProtocol.handler = nil
        }
    }
    func testOtherGateErrorsDoNotOverrideVerifiedAccessOrExtendExpiry() async throws {
        let gate = gate()
        try gate.applyEntitlementForTests(token(expiry: Date().addingTimeInterval(0.15)))
        gate.handle(IslandGateFailure(code: "keychain"))
        XCTAssertEqual(gate.stateName, "error")
        XCTAssertTrue(gate.isUnlocked, "UI error states cannot erase verified offline access")
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertFalse(gate.isUnlocked, "Offline access never exceeds the signed expiration")
        XCTAssertEqual(gate.stateName, "locked", "Expiration still notifies the controller after an error transition")
    }
    func testStoredValidTokenKeepsAccessWhenKeychainWritesFailDuringStartup() async throws {
        struct ReadOnlyStore: IslandGateStoring {
            let values: [String: Data]
            func read(_ name: String) throws -> Data? { values[name] }
            func write(_ data: Data, name: String) throws { throw IslandGateFailure(code: "keychain") }
            func delete(_ name: String) throws { throw IslandGateFailure(code: "keychain") }
        }
        let account = IslandGateAccount(id: "test-account", provider: "x", handle: "testfriend")
        let store = ReadOnlyStore(values: ["account": try JSONEncoder().encode(account), "entitlement": Data(try token().utf8)])
        let gate = IslandGate(store: store, client: IslandGateURLProtocol.client(), publicKey: signingKey.publicKey.rawRepresentation,
                              device: device, urlOpener: IslandGateRecordingURLOpener(), pasteboard: IslandGateRecordingPasteboard())
        let requests = expectation(description: "startup and explicit refresh finish before removing the mock")
        requests.expectedFulfillmentCount = 2
        IslandGateURLProtocol.handler = { _ in
            requests.fulfill()
            return (400, Data(#"{"error":"invalid_request"}"#.utf8))
        }
        defer { IslandGateURLProtocol.handler = nil }
        gate.start()
        XCTAssertEqual(gate.stateName, "error")
        XCTAssertTrue(gate.isUnlocked, "A verified stored token is still authoritative when persistence fails")
        await gate.refresh()
        await fulfillment(of: [requests], timeout: 1)
        XCTAssertTrue(gate.isUnlocked)
    }
    func testTransitionNamesExcludeAssociatedAccountAndErrorData() {
        let account = IslandGateAccount(id: "private-account", provider: "x", handle: "private-handle")
        let states: [IslandGateState] = [
            .locked(message: "private-message"), .submitting, .waking,
            .checking(claimID: "private-claim", since: Date(), follow: .pending, post: .pending),
            .unlocked(account: account, verified: true, exp: Date()), .pendingFollow(account: account, since: Date()),
            .trial(account: account, ends: Date()), .trialExpired(account: account),
            .repostChecking(account: account, trialEnds: nil), .rejected(message: "private-reason"),
            .conflict(deviceName: "private-device", rebindAvailableAt: nil), .error(code: "private-code", message: "private-message")
        ]
        XCTAssertEqual(states.map(\.name), ["locked", "submitting", "waking", "checking", "unlocked", "pending_follow", "trial", "trial_expired", "repost_checking", "rejected", "conflict", "error"])
    }
    func testControllerLiveStatusUsesActualPanelAndRequiresLocalPeer() throws {
        let gate = gate()
        let suite = "delta5.live-status.\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let controller = IslandController(store: IslandSettingsStore(defaults: defaults), hubPort: nil, startsNowPlaying: false, startsSystem: false, gate: gate)
        var request = IslandHub.Request(method: "GET", path: "/api/island/gate-status")
        XCTAssertEqual(controller.hub.handle(request, port: 8777).status, 403)
        request.peerVerified = true
        XCTAssertEqual(controller.hub.handle(request, port: 0).body["state"], "locked")
        XCTAssertFalse(controller.gateLiveStatus.panel_visible)
        XCTAssertNil(controller.gateLiveStatus.panel_frame, "No window has been created; do not fabricate a frame")
        try gate.applyEntitlementForTests(token())
        XCTAssertEqual(controller.hub.handle(request, port: 0).body["state"], "unlocked")
        XCTAssertFalse(controller.gateLiveStatus.panel_visible, "Gate permission is distinct from actual window visibility")
        request.headers["origin"] = "https://example.invalid"
        XCTAssertEqual(controller.hub.handle(request, port: 0).status, 403)
    }
}
