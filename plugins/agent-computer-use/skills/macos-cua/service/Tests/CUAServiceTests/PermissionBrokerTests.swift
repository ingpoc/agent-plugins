import Security
import XCTest
@testable import CUAService

/// One choke point for every prompt; runtime only preflights.
@MainActor
final class PermissionBrokerTests: XCTestCase {
    override func setUp() {
        PermissionBroker.dryRun = true
        PermissionBroker.fired = []
    }
    override func tearDown() {
        PermissionBroker.dryRun = false
        PermissionBroker.originVisible = nil
        KeychainGrant.existsOverride = nil
        KeychainGrant.defaults = .standard
        AutomationGate.probe = { PermissionProbe.automation($0) }
        AutomationGate.reset()
    }

    func testRequestOnlyFromVisibleGrantSurface() {
        PermissionBroker.originVisible = { $0 == .onboarding }
        XCTAssertTrue(PermissionBroker.request(.microphone, from: .onboarding))
        XCTAssertFalse(PermissionBroker.request(.screenRecording, from: .settingsGrant), "Settings not on screen")
        XCTAssertEqual(PermissionBroker.fired.map { $0.0 }, [.microphone])
    }

    func testMicrophonePaneUsesCurrentPrivacyExtension() {
        XCTAssertEqual(ACUPermission.microphone.paneURL?.absoluteString,
                       "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone")
    }

    func testEveryConsentHasAGrantRow() {
        // Keychain is a row like the TCC ones; no runtime path asks.
        XCTAssertTrue(ACUPermission.allCases.contains(.keychain))
        XCTAssertEqual(ACUPermission.keychain.label, "Keychain access")
        XCTAssertNil(ACUPermission.keychain.paneURL)
    }

    func testAutomationGateBlocksUnapprovedTargets() {
        var calls = 0
        AutomationGate.probe = { calls += 1; return $0 == "com.apple.Music" ? .granted : .missing }
        XCTAssertTrue(AutomationGate.allowed("com.apple.Music"))
        XCTAssertFalse(AutomationGate.allowed("com.spotify.client"))
        XCTAssertTrue(AutomationGate.allowed("com.apple.Music"))
        XCTAssertEqual(calls, 2, "cached for 10 s")
    }

    func testKeychainStatusFromFlagsAndMetadataOnly() {
        let d = UserDefaults(suiteName: "kc-\(UUID().uuidString)")!
        KeychainGrant.defaults = d
        KeychainGrant.existsOverride = { q in
            q[kSecAttrAccount as String] == "samantha" || q[kSecAttrLabel as String] == "xai-api"
        }
        XCTAssertEqual(KeychainGrant.items().map(\.id), ["island.samantha", "xai"])
        XCTAssertEqual(KeychainGrant.status(), .missing)
        KeychainGrant.markRead("island.samantha")
        XCTAssertEqual(KeychainGrant.status(), .missing)
        KeychainGrant.markRead("xai", label: "xai-api")
        XCTAssertEqual(KeychainGrant.status(), .granted)
    }

    func testTokenReadOncePerProcess() {
        // Memory-cached: repeated reads (tab switches, re-renders) never re-hit the Keychain.
        KeychainTokenStore.forget()
        let store = KeychainTokenStore()
        let src = "lint-\(UUID().uuidString)"   // no such item: a single metadata-free miss
        let before = KeychainTokenStore.dataReads
        for _ in 0..<5 { _ = store.read(source: src) }
        XCTAssertEqual(KeychainTokenStore.dataReads - before, 1)
        XCTAssertFalse(store.exists(source: src))
        KeychainTokenStore.forget()
    }
}
