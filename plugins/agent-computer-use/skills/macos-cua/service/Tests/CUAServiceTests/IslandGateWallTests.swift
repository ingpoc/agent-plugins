import AppKit
import CryptoKit
import XCTest
@testable import CUAService

@MainActor
final class IslandGateWallTests: XCTestCase {
    private func gate(provider: String = "x", consent: Bool? = nil, signedIn: Bool = true) throws -> (IslandGate, IslandGateMemoryStore) {
        let store = IslandGateMemoryStore()
        if signedIn { store.values["session"] = Data("wall-test-session".utf8) }
        let account = IslandGateAccount(id: "wall-account", provider: provider, handle: "@friend", name: "Island Friend", show_on_wall: consent)
        store.values["account"] = try JSONEncoder().encode(account)
        let device = IslandGateDevice(id: String(repeating: "d", count: 64), key: P256.Signing.PrivateKey())
        let gate = IslandGate(store: store, client: IslandGateURLProtocol.client(), device: device)
        gate.start()
        gate.transition(.unlocked(account: account, verified: false, exp: Date().addingTimeInterval(86400)))
        return (gate, store)
    }
    private static func body(_ request: URLRequest) throws -> [String: Any] {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }
                data.append(contentsOf: bytes.prefix(count))
            }
        }
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
    func testConsentDefaultsFalseIncludingSnapshotFixtures() throws {
        let (gate, _) = try gate()
        XCTAssertFalse(gate.showOnWall)
        XCTAssertFalse(gate.wallUpdating)
        XCTAssertNil(gate.wallError)
        for state in ["needs-verify-x", "needs-verify-linkedin", "unlocked"] {
            XCTAssertFalse(try IslandGateFixtures.gate(state).showOnWall)
        }
    }
    func testProviderSpecificCheckboxLabels() throws {
        let (x, _) = try gate()
        XCTAssertEqual(x.wallConsentTitle, "Show @friend on the Agent Plugins site")
        let (linkedin, _) = try gate(provider: "linkedin")
        XCTAssertEqual(linkedin.wallConsentTitle, "Show my name on the Agent Plugins site")
    }
    private func verifyRequest(provider: String, consent: Bool) async throws {
        let (gate, store) = try gate(provider: provider)
        gate.transition(.needsVerify(provider: provider, follow: false, post: false))
        gate.postURL = "https://www.linkedin.com/posts/example"
        gate.setVerificationWallConsent(consent)
        let finished = expectation(description: "Verification completed")
        gate.onChange.append { if gate.state == .pendingReview { finished.fulfill() } }
        IslandGateURLProtocol.handler = { request in
            if request.url?.path == "/v1/config" {
                return (200, Data(#"{"x":{"handle":"guru","intent_url":"https://x.com/intent/post?text=server","preset_text":"server"},"linkedin":{"share_url":"https://www.linkedin.com/sharing/share-offsite/?url=server","preset_text":"server"}}"#.utf8))
            }
            XCTAssertEqual(request.url?.path, "/v1/verify/\(provider)")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer wall-test-session")
            let body = try Self.body(request)
            XCTAssertEqual(body["show_on_wall"] as? Bool, consent)
            XCTAssertEqual(body.count, provider == "linkedin" ? 2 : 1)
            if provider == "linkedin" { XCTAssertEqual(body["post_url"] as? String, "https://www.linkedin.com/posts/example") }
            return (202, Data(#"{"status":"pending_review"}"#.utf8))
        }
        gate.verify()
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertEqual(gate.showOnWall, consent)
        // Pending review may outlive this process; the confirmed choice must survive it.
        let saved = try JSONDecoder().decode(IslandGateAccount.self, from: XCTUnwrap(store.values["account"]))
        XCTAssertEqual(saved.show_on_wall, consent)
    }
    func testXVerifySendsFalseByDefault() async throws { try await verifyRequest(provider: "x", consent: false) }
    func testXVerifySendsExplicitConsent() async throws { try await verifyRequest(provider: "x", consent: true) }
    func testLinkedInVerifySendsFalseByDefault() async throws { try await verifyRequest(provider: "linkedin", consent: false) }
    func testLinkedInVerifySendsExplicitConsentWithPostURL() async throws { try await verifyRequest(provider: "linkedin", consent: true) }
    func testWallToggleIsOptimisticSendsBearerAndPersistsOn204() async throws {
        let (gate, store) = try gate()
        let finished = expectation(description: "Wall updated")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/wall")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer wall-test-session")
            let body = try Self.body(request)
            XCTAssertEqual(body.count, 1)
            XCTAssertEqual(body["show"] as? Bool, true)
            return (204, Data())
        }
        gate.setWallVisibility(true)
        XCTAssertTrue(gate.showOnWall)
        XCTAssertTrue(gate.wallUpdating)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertTrue(gate.showOnWall)
        XCTAssertNil(gate.wallError)
        XCTAssertTrue(gate.isUnlocked)
        let stored = try JSONDecoder().decode(IslandGateAccount.self, from: XCTUnwrap(store.values["account"]))
        XCTAssertEqual(stored.show_on_wall, true)
        XCTAssertEqual(gate.account?.show_on_wall, true)
    }
    func testFailedOptInRollsBackWithoutLockingIsland() async throws {
        let (gate, store) = try gate()
        let finished = expectation(description: "Failed wall update rolled back")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { _ in (503, Data(#"{"error":"server_error","message":"raw server detail"}"#.utf8)) }
        gate.setWallVisibility(true)
        XCTAssertTrue(gate.showOnWall)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertFalse(gate.showOnWall)
        XCTAssertTrue(gate.isUnlocked)
        XCTAssertNotNil(gate.wallError)
        XCTAssertFalse(gate.wallError?.contains("server_error") ?? true)
        XCTAssertFalse(gate.wallError?.contains("raw server detail") ?? true)
        let stored = try JSONDecoder().decode(IslandGateAccount.self, from: XCTUnwrap(store.values["account"]))
        XCTAssertNil(stored.show_on_wall)
    }
    func testFailedOptOutRestoresExistingConsent() async throws {
        let (gate, _) = try gate(consent: true)
        let finished = expectation(description: "Opt-out failure")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(try Self.body(request)["show"] as? Bool, false)
            throw URLError(.notConnectedToInternet)
        }
        gate.setWallVisibility(false)
        XCTAssertFalse(gate.showOnWall)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertTrue(gate.showOnWall)
        XCTAssertNotNil(gate.wallError)
    }
    func testMissingSessionRollsBackWithSignInCopy() async throws {
        let (gate, _) = try gate(signedIn: false)
        let finished = expectation(description: "Missing session")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { _ in XCTFail("No network request without session"); return (500, Data()) }
        gate.setWallVisibility(true)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertFalse(gate.showOnWall)
        XCTAssertTrue(gate.wallError?.contains("Sign in again") ?? false)
    }
    func testAccountMenuReflectsOptimisticToggleAndRollback() async throws {
        _ = NSApplication.shared
        let (gate, _) = try gate()
        let status = StatusBarController(gate: gate)
        let menu = status.makeMenu()
        let account = try XCTUnwrap(menu.items.first(where: { $0.title == gate.accessSummary }))
        let toggle = try XCTUnwrap(account.submenu?.items.first(where: { $0.title == "Show me on the site" }))
        XCTAssertEqual(toggle.state, .off)
        let finished = expectation(description: "Menu rolled back")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { _ in (500, Data(#"{"error":"server_error"}"#.utf8)) }
        XCTAssertTrue(NSApplication.shared.sendAction(try XCTUnwrap(toggle.action), to: toggle.target, from: toggle))
        XCTAssertEqual(toggle.state, .on)
        XCTAssertFalse(toggle.isEnabled)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertEqual(toggle.state, .off)
        XCTAssertTrue(toggle.isEnabled)
        XCTAssertTrue(account.submenu?.items.contains(where: { !$0.isHidden && $0.title == gate.wallError }) ?? false)
    }
    func testSignOutClearsConsentAndLateResponseCannotRestoreIt() async throws {
        let (gate, _) = try gate()
        let began = expectation(description: "Wall request began")
        let finish = DispatchSemaphore(value: 0)
        defer { finish.signal() }
        IslandGateURLProtocol.handler = { _ in
            began.fulfill()
            guard finish.wait(timeout: .now() + 3) == .success else { throw URLError(.timedOut) }
            return (204, Data())
        }
        gate.setWallVisibility(true)
        await fulfillment(of: [began], timeout: 3)
        gate.signOut(); finish.signal()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(gate.showOnWall)
        XCTAssertFalse(gate.wallUpdating)
        XCTAssertNil(gate.wallError)
    }
}
