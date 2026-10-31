import AppKit
import CryptoKit
import XCTest
@testable import CUAService

@MainActor
final class IslandGateWallTests: XCTestCase {
    private func gate(provider: String = "x", consent: Bool? = nil) throws -> (IslandGate, IslandGateMemoryStore) {
        let store = IslandGateMemoryStore()
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
        for state in ["locked", "locked-linkedin", "unlocked"] {
            XCTAssertFalse(try IslandGateFixtures.gate(state).showOnWall)
        }
    }
    func testProviderSpecificCheckboxLabels() throws {
        let (x, _) = try gate()
        x.handleText = "@friend"
        XCTAssertEqual(x.wallConsentTitle, "Show @friend on the Agent Plugins site")
        let (linkedin, _) = try gate(provider: "linkedin")
        linkedin.transition(.locked(linkedIn: true))
        XCTAssertEqual(linkedin.wallConsentTitle, "Show my name on the Agent Plugins site")
    }
    func testClaimSendsWallConsent() async throws {
        let store = IslandGateMemoryStore()
        let gate = IslandGate(store: store, client: IslandGateURLProtocol.client(), device: IslandGateDevice(id: String(repeating: "d", count: 64), key: P256.Signing.PrivateKey()))
        gate.start()
        gate.handleText = "@friend"
        gate.setLocalWallConsent(true)
        gate.setConfigForTests(IslandGateConfig(
            x: .init(handle: "g", profile_url: URL(string: "https://x.com/g")!, intent_url: URL(string: "https://x.com/intent/post")!, preset_text: "p"),
            linkedin: .init(share_url: URL(string: "https://www.linkedin.com/sharing/share-offsite/?url=x")!, preset_text: "p", follow_url: nil),
            match: nil, grace_days: 14, poll_seconds: 60, check_eta_text: nil
        ))
        let finished = expectation(description: "Claim submitted")
        gate.onChange.append {
            if case .checking = gate.state { finished.fulfill() }
        }
        IslandGateURLProtocol.handler = { request in
            if request.url?.path == "/v1/claims" {
                let body = try Self.body(request)
                XCTAssertEqual(body["show_on_wall"] as? Bool, true)
                return (201, Data(#"{"claim_id":"c1","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":60}"#.utf8))
            }
            throw URLError(.badURL)
        }
        gate.submitCheck()
        await fulfillment(of: [finished], timeout: 3)
    }
    func testWallToggleIsOptimisticSendsSignatureAndPersists() async throws {
        let (gate, store) = try gate()
        let finished = expectation(description: "Wall updated")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/wall")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertNotNil(request.value(forHTTPHeaderField: "X-ACU-Sig"))
            let body = try Self.body(request)
            XCTAssertEqual(body["show"] as? Bool, true)
            return (200, Data(#"{"show":true}"#.utf8))
        }
        gate.setWallVisibility(true)
        XCTAssertTrue(gate.showOnWall)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertTrue(gate.showOnWall)
        XCTAssertNil(gate.wallError)
        let stored = try JSONDecoder().decode(IslandGateAccount.self, from: XCTUnwrap(store.values["account"]))
        XCTAssertEqual(stored.show_on_wall, true)
    }
    func testFailedOptInRollsBackWithoutLockingIsland() async throws {
        let (gate, store) = try gate()
        let finished = expectation(description: "Failed wall update rolled back")
        gate.onChange.append { if !gate.wallUpdating { finished.fulfill() } }
        IslandGateURLProtocol.handler = { _ in (503, Data(#"{"error":"server_error","message":"raw server detail"}"#.utf8)) }
        gate.setWallVisibility(true)
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertFalse(gate.showOnWall)
        XCTAssertTrue(gate.isUnlocked)
        XCTAssertNotNil(gate.wallError)
        let stored = try JSONDecoder().decode(IslandGateAccount.self, from: XCTUnwrap(store.values["account"]))
        XCTAssertNil(stored.show_on_wall)
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
        await fulfillment(of: [finished], timeout: 3)
        XCTAssertEqual(toggle.state, .off)
    }
}
