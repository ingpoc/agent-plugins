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
    func testRejectedCopyForReasonCodes() {
        for code in ["follow_missing", "post_missing", "post_author_mismatch", "post_too_old", "claim_expired"] {
            let gate = gate()
            gate.handle(IslandGateFailure(code: code))
            guard case .rejected(let message) = gate.state else { return XCTFail("Expected rejected for \(code)") }
            XCTAssertEqual(message, IslandGateFailure.friendlyMessage(for: code))
        }
    }
    func testConflictTransitions() {
        let gate = gate()
        gate.handle(IslandGateFailure(code: "device_conflict", device_name: "Studio", rebind_available_at: "2026-11-01"))
        XCTAssertEqual(gate.state, .conflict(deviceName: "Studio", rebindAvailableAt: "2026-11-01"))
    }
    func testPollMapsBlockedClaims() async {
        for reason in ["rebind_cooldown", "post_url_used", "post_too_old", "device_taken"] {
            let store = IslandGateMemoryStore()
            store.values["claim-id"] = Data("blocked-claim".utf8)
            let gate = gate(store: store)
            gate.start()
            IslandGateURLProtocol.handler = { _ in
                let body: [String: Any] = ["status": "rejected", "reason": reason, "rebind_available_at": "2026-11-08T00:00:00.000Z"]
                return (200, try! JSONSerialization.data(withJSONObject: body))
            }
            await gate.pollOnce()
            XCTAssertNil(store.values["claim-id"])
            if reason == "rebind_cooldown" {
                XCTAssertEqual(gate.state, .conflict(deviceName: nil, rebindAvailableAt: "2026-11-08T00:00:00.000Z"))
            } else {
                XCTAssertEqual(gate.state, .rejected(message: IslandGateFailure.friendlyMessage(for: reason)))
            }
        }
    }
    func testValidEntitlementUnlocksAndPersists() throws {
        let store = IslandGateMemoryStore(), gate = gate(store: store)
        try gate.applyEntitlementForTests(token())
        XCTAssertTrue(gate.isUnlocked); XCTAssertNotNil(store.values["entitlement"])
    }
    func testMovedAndRevokedLockImmediatelyAndDeleteToken() throws {
        for code in ["device_moved", "revoked"] {
            let store = IslandGateMemoryStore(), gate = gate(store: store)
            try gate.applyEntitlementForTests(token())
            gate.handle(IslandGateFailure(code: code))
            XCTAssertFalse(gate.isUnlocked); XCTAssertNil(store.values["entitlement"])
            guard case .locked = gate.state else { return XCTFail("Expected locked") }
        }
    }
    func testOfflineAndServerFailureKeepValidEntitlement() async throws {
        let gate = gate()
        try gate.applyEntitlementForTests(token())
        IslandGateURLProtocol.handler = { _ in throw URLError(.notConnectedToInternet) }
        await gate.refresh(); XCTAssertTrue(gate.isUnlocked)
        IslandGateURLProtocol.handler = { _ in (503, Data(#"{"error":"server_error"}"#.utf8)) }
        await gate.refresh(); XCTAssertTrue(gate.isUnlocked)
    }
    func testRefreshMovedLocksImmediately() async throws {
        let gate = gate(); try gate.applyEntitlementForTests(token())
        IslandGateURLProtocol.handler = { _ in (409, Data(#"{"error":"device_moved"}"#.utf8)) }
        await gate.refresh(); XCTAssertFalse(gate.isUnlocked)
    }
    func testExpiryHidesPanelWithoutWaitingForDailyRefresh() async throws {
        let gate = gate()
        try gate.applyEntitlementForTests(token(exp: Date().addingTimeInterval(0.1)))
        XCTAssertTrue(gate.isUnlocked)
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertFalse(gate.isUnlocked)
        guard case .locked = gate.state else { return XCTFail("Expired gate must lock") }
    }
    func testSignOutCannotRefreshBackToUnlocked() async throws {
        let store = IslandGateMemoryStore(), gate = gate(store: store)
        try gate.applyEntitlementForTests(token())
        gate.signOut()
        IslandGateURLProtocol.handler = { _ in XCTFail("Signed-out gate must not refresh"); return (500, Data()) }
        await gate.refresh(); XCTAssertFalse(gate.isUnlocked)
        XCTAssertNil(store.values["entitlement"])
    }
    func testLaunchResumesOpenClaim() throws {
        let store = IslandGateMemoryStore()
        store.values["claim-id"] = Data("open-claim".utf8)
        let gate = gate(store: store)
        gate.start()
        guard case .checking(let id, _, _, _) = gate.state else { return XCTFail("Expected checking") }
        XCTAssertEqual(id, "open-claim")
    }
    func testPollUnlockStoresEntitlement() async throws {
        let store = IslandGateMemoryStore()
        store.values["claim-id"] = Data("claim-42".utf8)
        let gate = gate(store: store)
        gate.start()
        let data = try JSONSerialization.data(withJSONObject: ["status": "unlocked", "entitlement": token()])
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.url?.path, "/v1/claims/claim-42")
            return (200, data)
        }
        await gate.pollOnce()
        XCTAssertTrue(gate.isUnlocked)
        XCTAssertNil(store.values["claim-id"])
    }
    func testShareCopiesLinkedInPreset() {
        let gate = gate()
        gate.setConfigForTests(IslandGateConfig(
            x: .init(handle: "guru", profile_url: URL(string: "https://x.com/guru")!, intent_url: URL(string: "https://x.com/intent/post")!, preset_text: "x"),
            linkedin: .init(share_url: URL(string: "https://www.linkedin.com/sharing/share-offsite/?url=x")!, preset_text: "li preset", follow_url: URL(string: "https://www.linkedin.com/in/guru")),
            match: nil, grace_days: 14, poll_seconds: 60, check_eta_text: nil
        ))
        gate.transition(.locked(linkedIn: true))
        gate.openShare()
        XCTAssertEqual(NSPasteboard.general.string(forType: .string), "li preset")
        XCTAssertTrue(gate.copied)
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
        island.startStreaming()
        XCTAssertFalse(island.debugPanel.isShown)
        island.stopStreaming()
        island.ingest(IslandSnapshot(kind: "listening", title: "Listening"))
        try gate.applyEntitlementForTests(token())
        XCTAssertTrue(island.panelAllowed)
        XCTAssertTrue(island.debugPanel.isShown)
        settings.set(false, for: IslandSettingsSchema.showIsland)
        XCTAssertFalse(island.panelAllowed)
        gate.signOut(); XCTAssertFalse(island.panelAllowed)
    }
    func testEverySnapshotFixtureAvoidsKeychainAndNetwork() throws {
        for state in ["locked", "locked-linkedin", "waking", "checking", "conflict", "unlocked", "rejected", "error"] {
            let gate = try IslandGateFixtures.gate(state)
            gate.start()
            XCTAssertEqual(gate.isUnlocked, state == "unlocked")
        }
    }
}
