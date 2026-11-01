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
    private func token(exp: Date = Date().addingTimeInterval(86400 * 14), tier: String? = nil) throws -> String {
        let payload = try JSONEncoder().encode(IslandGateEntitlement(v: 1, acct: "account", prov: "x", handle: "friend", device_id: device.id, iat: Date().timeIntervalSince1970 - 100, exp: exp.timeIntervalSince1970, tier: tier)).gateBase64URL
        return payload + "." + (try signingKey.signature(for: Data(payload.utf8))).gateBase64URL
    }
    private func gate(store: IslandGateMemoryStore = .init()) -> IslandGate {
        IslandGate(store: store, client: IslandGateURLProtocol.client(), publicKey: signingKey.publicKey.rawRepresentation, device: device, urlOpener: IslandGateRecordingURLOpener())
    }
    func testRejectedCopyForReasonCodes() {
        for code in ["follow_missing", "post_missing", "claim_expired"] {
            let gate = gate()
            gate.handle(IslandGateFailure(code: code))
            guard case .rejected(let message) = gate.state else { return XCTFail("Expected rejected for \(code)") }
            XCTAssertEqual(message, IslandGateFailure.friendlyMessage(for: code))
        }
        for code in ["post_not_found", "post_author_mismatch", "post_too_old", "post_missing_tag"] {
            let gate = gate()
            gate.handle(IslandGateFailure(code: code))
            XCTAssertEqual(gate.state, .error(code: code, message: IslandGateFailure.friendlyMessage(for: code)))
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
            } else if reason == "post_too_old" {
                XCTAssertEqual(gate.state, .error(code: reason, message: IslandGateFailure.friendlyMessage(for: reason)))
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
    func testSignedTrialPersistsPollsExpiresAndConverts() async throws {
        let store = IslandGateMemoryStore()
        let gate = gate(store: store)
        gate.setConfigForTests(.init(
            x: .init(handle: "guru", profile_url: URL(string: "https://x.com/guru")!, intent_url: URL(string: "https://x.com/intent/post")!, preset_text: "legacy", launch_post_id: "123", repost_url: URL(string: "https://x.com/intent/retweet?tweet_id=123")),
            linkedin: .init(share_url: URL(string: "https://www.linkedin.com/feed/")!, preset_text: "legacy", follow_url: URL(string: "https://www.linkedin.com/in/guru")!, launch_post_url: nil, repost_url: nil),
            match: nil, grace_days: 14, poll_seconds: 60, check_eta_text: nil
        ))
        try gate.armCheckingClaimForTests(id: "trial-claim")
        let expired = expectation(description: "trial expires")
        gate.onChange.append { if case .trialExpired = gate.state { expired.fulfill() } }
        try gate.applyEntitlementForTests(token(exp: Date().addingTimeInterval(0.5), tier: "trial"))
        guard case .trial = gate.state else { return XCTFail("signed trial tier should unlock trial state") }
        XCTAssertEqual(String(data: try XCTUnwrap(store.values["entitlement-tier"]), encoding: .utf8), "trial")
        XCTAssertEqual(String(data: try XCTUnwrap(store.values["claim-id"]), encoding: .utf8), "trial-claim")
        await fulfillment(of: [expired], timeout: 2)
        XCTAssertFalse(gate.isUnlocked)

        try gate.applyEntitlementForTests(token())
        guard case .unlocked = gate.state else { return XCTFail("full entitlement should leave trial state") }
        XCTAssertNil(store.values["entitlement-tier"])
        XCTAssertNil(store.values["trial-ends-at"])
        XCTAssertNil(store.values["claim-id"])
    }
    func testExpiredFixtureEntitlementsDoNotBypassSignedExpiry() throws {
        let account = IslandGateAccount(id: "account", provider: "x", handle: "friend")
        let gate = IslandGate(fixtureState: .trial(account: account, ends: Date().addingTimeInterval(-1)), fixtureAccount: account)
        XCTAssertFalse(gate.isUnlocked)
        let permanent = IslandGate(fixtureState: .unlocked(account: account, verified: true, exp: Date().addingTimeInterval(-1)), fixtureAccount: account)
        XCTAssertFalse(permanent.isUnlocked)
    }
    func testPrelaunchTrialExpiryKeepsIdentityAndRefreshesWithSignedToken() async throws {
        let store = IslandGateMemoryStore()
        let account = IslandGateAccount(id: "account", provider: "x", handle: "friend")
        store.values["account"] = try JSONEncoder().encode(account)
        store.values["entitlement-tier"] = Data("trial".utf8)
        store.values["trial-ends-at"] = Data(ISO8601DateFormatter().string(from: Date().addingTimeInterval(-1)).utf8)
        store.values["entitlement"] = Data(try token(exp: Date().addingTimeInterval(-1), tier: "trial").utf8)
        let gate = gate(store: store)
        let fresh = try token(exp: Date().addingTimeInterval(72 * 3600), tier: "trial")
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/entitlement/refresh")
            return (200, try JSONSerialization.data(withJSONObject: ["entitlement": fresh]))
        }
        defer { gate.stopPollingForTests(); IslandGateURLProtocol.handler = nil }
        let refreshed = expectation(description: "server returns rolling signed trial")
        gate.onChange.append { if case .trial(_, let ends) = gate.state, ends.timeIntervalSinceNow > 71 * 3600 { refreshed.fulfill() } }
        gate.start()
        XCTAssertFalse(gate.isUnlocked, "Expired signed token must not be extended locally")
        await fulfillment(of: [refreshed], timeout: 2)
        XCTAssertTrue(gate.isUnlocked)
        XCTAssertEqual(gate.account?.id, "account")
        XCTAssertEqual(String(data: try XCTUnwrap(store.values["entitlement-tier"]), encoding: .utf8), "trial")
    }
    func testServerExpiryRefreshesStalePrelaunchConfigOnce() async throws {
        let gate = gate()
        try gate.applyEntitlementForTests(token(exp: Date().addingTimeInterval(3600), tier: "trial"))
        var refreshes = 0
        var configs = 0
        IslandGateURLProtocol.handler = { request in
            if request.url?.path == "/v1/entitlement/refresh" {
                refreshes += 1
                return (410, Data(#"{"error":"trial_expired"}"#.utf8))
            }
            XCTAssertEqual(request.url?.path, "/v1/config")
            configs += 1
            return (200, Data(#"{"x":{"handle":"guru","profile_url":"https://x.com/guru","intent_url":"https://x.com/intent/post","preset_text":"legacy","launch_post_id":"123","repost_url":"https://x.com/intent/retweet?tweet_id=123"},"linkedin":{"share_url":"https://www.linkedin.com/feed/","preset_text":"legacy","follow_url":"https://www.linkedin.com/in/example/"}}"#.utf8))
        }
        defer { gate.stopPollingForTests(); IslandGateURLProtocol.handler = nil }
        let expired = expectation(description: "configured launch expires the cached prelaunch trial")
        gate.onChange.append { if case .trialExpired = gate.state { expired.fulfill() } }
        await gate.refresh()
        await fulfillment(of: [expired], timeout: 2)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(refreshes, 1, "An expiry response must not trigger an immediate refresh loop")
        XCTAssertEqual(configs, 1)
        XCTAssertFalse(gate.isUnlocked)
        XCTAssertNotNil(gate.account)
    }
    func testUnsignedTrialEndDateDoesNotExtendAccess() async throws {
        let store = IslandGateMemoryStore()
        store.values["claim-id"] = Data("claim-unsigned-trial".utf8)
        let gate = gate(store: store)
        gate.start()
        IslandGateURLProtocol.handler = { _ in
            (200, Data(#"{"status":"trial","trial_ends_at":"2099-01-01T00:00:00.000Z"}"#.utf8))
        }
        defer { gate.stopPollingForTests(); IslandGateURLProtocol.handler = nil }
        await gate.pollOnce()
        XCTAssertFalse(gate.isUnlocked)
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
    func testRepostOpensConfiguredURLWithoutClipboard() {
        let opener = IslandGateRecordingURLOpener()
        let liRepost = URL(string: "https://www.linkedin.com/feed/update/urn:li:activity:123456")!
        let gate = IslandGate(store: IslandGateMemoryStore(), client: IslandGateURLProtocol.client(), publicKey: signingKey.publicKey.rawRepresentation, device: device, urlOpener: opener)
        gate.setConfigForTests(IslandGateConfig(
            x: .init(handle: "guru", profile_url: URL(string: "https://x.com/guru")!, intent_url: URL(string: "https://x.com/intent/post")!, preset_text: "x", repost_url: URL(string: "https://x.com/intent/retweet?tweet_id=123456")),
            linkedin: .init(share_url: URL(string: "https://www.linkedin.com/sharing/share-offsite/?url=x")!, preset_text: "li preset", follow_url: URL(string: "https://www.linkedin.com/in/guru"), repost_url: liRepost),
            match: nil, grace_days: 14, poll_seconds: 60, check_eta_text: nil
        ))
        gate.transition(.locked(linkedIn: true))
        gate.openRepost()
        XCTAssertEqual(opener.opened, [liRepost])
        XCTAssertEqual((gate.pasteboard as? IslandGateRecordingPasteboard)?.written, [])
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
        let status = StatusBarController(gate: gate)
        let menu = status.makeMenu()
        let access = try XCTUnwrap(menu.items.first { $0.title == gate.accessSummary })
        XCTAssertNil(access.submenu)
        for tier in [String?.some("trial"), nil] {
            try gate.applyEntitlementForTests(token(tier: tier))
            XCTAssertTrue(gate.isUnlocked)
            XCTAssertTrue(island.panelAllowed)
            XCTAssertTrue(island.debugPanel.isShown)
            XCTAssertNotNil(access.submenu, "Trial and full tokens unlock the same status menu features")
        }
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
