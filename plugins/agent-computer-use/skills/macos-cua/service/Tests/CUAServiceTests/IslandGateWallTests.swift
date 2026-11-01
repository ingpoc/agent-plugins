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
        let gate = IslandGate(store: store, client: IslandGateURLProtocol.client(), device: device, urlOpener: IslandGateRecordingURLOpener())
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
            if case .pendingFollow = gate.state { finished.fulfill() }
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
    /// The site-wall answer from unlock is final: the account submenu keeps only "Account details…".
    func testAccountMenuHasNoSiteToggle() throws {
        _ = NSApplication.shared
        let (gate, _) = try gate()
        let menu = StatusBarController(gate: gate).makeMenu()
        let account = try XCTUnwrap(menu.items.first(where: { $0.title == gate.accessSummary }))
        XCTAssertEqual(account.submenu?.items.map(\.title), ["Account details…"])
        XCTAssertEqual(IslandGateWallCopy.choice(true), "Shown on the Agent Plugins site")
        XCTAssertEqual(IslandGateWallCopy.choice(false), "Not shown on the Agent Plugins site")
    }
}
