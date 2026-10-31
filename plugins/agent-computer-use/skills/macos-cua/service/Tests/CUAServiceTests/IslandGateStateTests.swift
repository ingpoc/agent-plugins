import XCTest
import CryptoKit
import AppKit
@testable import CUAService

final class IslandGateMemoryStore: IslandGateStoring {
    var values: [String: Data] = [:]
    func read(_ name: String) throws -> Data? { values[name] }
    func write(_ data: Data, name: String) throws { values[name] = data }
    func delete(_ name: String) throws { values[name] = nil }
}

@MainActor
final class IslandGateStateTests: XCTestCase {
    private let signingKey = Curve25519.Signing.PrivateKey()
    private let device = IslandGateDevice(id: String(repeating: "d", count: 64), key: P256.Signing.PrivateKey())
    private func token(exp: Date = Date().addingTimeInterval(86400 * 14)) throws -> String {
        let payload = try JSONEncoder().encode(IslandGateEntitlement(v: 1, acct: "account", prov: "x", handle: "friend", device_id: device.id, iat: Date().timeIntervalSince1970 - 100, exp: exp.timeIntervalSince1970)).gateBase64URL
        return payload + "." + (try signingKey.signature(for: Data(payload.utf8))).gateBase64URL
    }
    private func gate(store: IslandGateMemoryStore = .init()) -> IslandGate {
        IslandGate(store: store, client: IslandGateURLProtocol.client(), publicKey: signingKey.publicKey.rawRepresentation, device: device)
    }
    func testCancelReturnsToLocked() {
        let gate = gate(); gate.transition(.signingIn("x")); gate.cancelSignIn()
        XCTAssertEqual(gate.state, .locked()); XCTAssertFalse(gate.isUnlocked)
    }
    func testMissingFollowAndPostFlagsRemainIndependent() {
        let gate = gate()
        gate.handle(IslandGateFailure(code: "follow_missing", follow: false, post: true))
        XCTAssertEqual(gate.state, .needsVerify(provider: "x", follow: false, post: true))
        gate.handle(IslandGateFailure(code: "post_missing", follow: true, post: false))
        XCTAssertEqual(gate.state, .needsVerify(provider: "x", follow: true, post: false))
    }
    func testPendingAndConflictTransitions() {
        let gate = gate()
        gate.handle(IslandGateFailure(code: "pending_review")); XCTAssertEqual(gate.state, .pendingReview)
        gate.handle(IslandGateFailure(code: "device_conflict", device_name: "Studio", bound_at: "2026-10-20", rebind_available_at: "2026-11-01"))
        XCTAssertEqual(gate.state, .conflict(deviceName: "Studio", boundAt: "2026-10-20", rebindAvailableAt: "2026-11-01"))
        gate.handle(IslandGateFailure(code: "rebind_cooldown", rebind_available_at: "2026-11-01"))
        XCTAssertEqual(gate.state, .conflict(deviceName: "your other Mac", boundAt: nil, rebindAvailableAt: "2026-11-01"))
    }
    func testPostURLFailureAllowsEditingAgain() {
        let gate = gate(); gate.postURL = "bad"
        gate.handle(IslandGateFailure(code: "post_url_invalid"))
        XCTAssertEqual(gate.state, .needsVerify(provider: "linkedin", follow: false, post: false))
        XCTAssertNotNil(gate.verificationMessage)
    }
    func testSessionExpiryRequiresSignIn() {
        let gate = gate(); gate.handle(IslandGateFailure(code: "session_expired"))
        guard case .locked(let message) = gate.state else { return XCTFail("Expected locked") }
        XCTAssertNotNil(message)
    }
    func testValidEntitlementUnlocksAndPersists() throws {
        let store = IslandGateMemoryStore(), gate = gate(store: store)
        try gate.apply(IslandGateReply(status: "unlocked", entitlement: token()))
        XCTAssertTrue(gate.isUnlocked); XCTAssertNotNil(store.values["entitlement"])
    }
    func testMovedAndRevokedLockImmediatelyAndDeleteToken() throws {
        for code in ["device_moved", "revoked"] {
            let store = IslandGateMemoryStore(), gate = gate(store: store)
            try gate.apply(IslandGateReply(entitlement: token()))
            gate.handle(IslandGateFailure(code: code))
            XCTAssertFalse(gate.isUnlocked); XCTAssertNil(store.values["entitlement"])
            guard case .locked(let message) = gate.state else { return XCTFail("Expected locked") }
            XCTAssertNotNil(message)
        }
    }
    func testOfflineAndServerFailureKeepValidEntitlement() async throws {
        let gate = gate()
        try gate.apply(IslandGateReply(entitlement: token()))
        IslandGateURLProtocol.handler = { _ in throw URLError(.notConnectedToInternet) }
        await gate.refresh(); XCTAssertTrue(gate.isUnlocked)
        IslandGateURLProtocol.handler = { _ in (503, Data(#"{"error":"server_error"}"#.utf8)) }
        await gate.refresh(); XCTAssertTrue(gate.isUnlocked)
    }
    func testRefreshMovedLocksImmediately() async throws {
        let gate = gate(); try gate.apply(IslandGateReply(entitlement: token()))
        IslandGateURLProtocol.handler = { _ in (409, Data(#"{"error":"device_moved"}"#.utf8)) }
        await gate.refresh(); XCTAssertFalse(gate.isUnlocked)
    }
    func testExpiryHidesPanelWithoutWaitingForDailyRefresh() async throws {
        let gate = gate()
        try gate.apply(IslandGateReply(entitlement: token(exp: Date().addingTimeInterval(0.1))))
        XCTAssertTrue(gate.isUnlocked)
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertFalse(gate.isUnlocked)
        guard case .locked = gate.state else { return XCTFail("Expired gate must lock") }
    }
    func testSignOutCannotRefreshBackToUnlocked() async throws {
        let store = IslandGateMemoryStore(), gate = gate(store: store)
        try gate.apply(IslandGateReply(entitlement: token()))
        gate.signOut()
        IslandGateURLProtocol.handler = { _ in XCTFail("Signed-out gate must not refresh"); return (500, Data()) }
        await gate.refresh(); XCTAssertFalse(gate.isUnlocked)
        XCTAssertNil(store.values["session"]); XCTAssertNil(store.values["entitlement"])
    }
    func testSignOutDiscardsInFlightRefresh() async throws {
        let gate = gate(); try gate.apply(IslandGateReply(entitlement: token()))
        let reply = try JSONSerialization.data(withJSONObject: ["entitlement": token()])
        let began = expectation(description: "Refresh began")
        let finish = DispatchSemaphore(value: 0)
        defer { finish.signal() }
        IslandGateURLProtocol.handler = { _ in
            began.fulfill()
            guard finish.wait(timeout: .now() + 3) == .success else { throw URLError(.timedOut) }
            return (200, reply)
        }
        let refresh = Task { await gate.refresh() }
        await fulfillment(of: [began], timeout: 3)
        gate.signOut(); finish.signal()
        await refresh.value
        XCTAssertFalse(gate.isUnlocked)
        XCTAssertEqual(gate.state, .locked())
    }
    func testPendingPollingOnlyWhileOpen() async throws {
        let store = IslandGateMemoryStore()
        store.values["session"] = Data("test-session".utf8)
        let gate = gate(store: store); gate.start(); gate.transition(.pendingReview)
        var calls = 0
        IslandGateURLProtocol.handler = { req in
            calls += 1
            XCTAssertEqual(req.url?.path, "/v1/status")
            return (200, Data(#"{"status":"pending_review"}"#.utf8))
        }
        await gate.poll(); XCTAssertEqual(calls, 0)
        gate.opened(); await gate.poll(); XCTAssertEqual(calls, 1)
        XCTAssertEqual(gate.state, .pendingReview)
        gate.closed(); await gate.poll(); XCTAssertEqual(calls, 1)
    }
    func testPendingPollCanUnlock() async throws {
        let store = IslandGateMemoryStore(); store.values["session"] = Data("test-session".utf8)
        let gate = gate(store: store); gate.start(); gate.transition(.pendingReview); gate.opened()
        let data = try JSONSerialization.data(withJSONObject: ["status": "unlocked", "entitlement": token()])
        IslandGateURLProtocol.handler = { _ in (200, data) }
        await gate.poll(); XCTAssertTrue(gate.isUnlocked); gate.closed()
    }
    func testConflictRebindSignsRequestAndUnlocks() async throws {
        let store = IslandGateMemoryStore(); store.values["session"] = Data("test-session".utf8)
        let gate = gate(store: store); gate.start()
        gate.handle(IslandGateFailure(code: "device_conflict", device_name: "Studio"))
        let data = try JSONSerialization.data(withJSONObject: ["status": "unlocked", "entitlement": token()])
        let received = expectation(description: "Rebind request")
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.url?.path, "/v1/device/rebind")
            XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer test-session")
            received.fulfill(); return (200, data)
        }
        gate.rebind()
        await fulfillment(of: [received], timeout: 3)
        for _ in 0..<30 {
            if gate.isUnlocked { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(gate.isUnlocked)
    }
    func testIslandPanelPermissionAndVoiceStreamingIndependent() throws {
        let gate = gate()
        let name = "IslandGateControllerTests.\(UUID())"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let settings = IslandSettingsStore(defaults: defaults)
        settings.set(true, for: IslandSettingsSchema.showIsland)
        let island = IslandController(store: settings, hubPort: nil, startsNowPlaying: false, startsSystem: false, gate: gate)
        island.start()
        defer { island.shutdown() }
        XCTAssertFalse(island.panelAllowed)
        XCTAssertFalse(island.debugPanel.isShown)
        // These paths and the hub remain callable with a locked gate.
        island.startStreaming()
        XCTAssertFalse(island.debugPanel.isShown)
        island.stopStreaming()
        XCTAssertFalse(island.debugPanel.isShown)
        island.ingest(IslandSnapshot(kind: "listening", title: "Listening"))
        try gate.apply(IslandGateReply(entitlement: token()))
        XCTAssertTrue(island.panelAllowed)
        XCTAssertTrue(island.debugPanel.isShown)
        settings.set(false, for: IslandSettingsSchema.showIsland)
        XCTAssertFalse(island.panelAllowed)
        gate.signOut(); XCTAssertFalse(island.panelAllowed)
        XCTAssertFalse(island.debugPanel.isShown)
    }
    func testEverySnapshotFixtureAvoidsKeychainAndNetwork() throws {
        for state in ["locked", "signing-in", "needs-verify-x", "needs-verify-linkedin", "pending", "conflict", "unlocked", "error"] {
            let gate = try IslandGateFixtures.gate(state)
            gate.start()
            XCTAssertEqual(gate.isUnlocked, state == "unlocked")
        }
    }
}
