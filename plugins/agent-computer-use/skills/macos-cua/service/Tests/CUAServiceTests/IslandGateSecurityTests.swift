import XCTest
import CryptoKit
@testable import CUAService

final class IslandGateSecurityTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)
    private let device = String(repeating: "a", count: 64)
    private let key = Curve25519.Signing.PrivateKey()
    private func token(device: String? = nil, iat: Double? = nil, exp: Double? = nil) throws -> String {
        let claim = IslandGateEntitlement(v: 1, acct: "account", prov: "x", handle: "friend", device_id: device ?? self.device, iat: iat ?? now.timeIntervalSince1970, exp: exp ?? now.timeIntervalSince1970 + 14 * 86400)
        let payload = try JSONEncoder().encode(claim).gateBase64URL
        return payload + "." + (try key.signature(for: Data(payload.utf8))).gateBase64URL
    }
    private var verifier: IslandGateVerifier { .init(publicKey: key.publicKey.rawRepresentation) }
    func testValidEntitlement() throws { XCTAssertEqual(try verifier.verify(token(), deviceID: device, now: now).acct, "account") }
    func testTamperedPayload() throws {
        let original = try token().split(separator: ".")
        let payload = String(data: Data(gateBase64URL: String(original[0]))!, encoding: .utf8)!.replacingOccurrences(of: "friend", with: "attacker")
        XCTAssertThrowsError(try verifier.verify(Data(payload.utf8).gateBase64URL + "." + original[1], deviceID: device, now: now))
    }
    func testWrongDevice() throws { XCTAssertThrowsError(try verifier.verify(token(), deviceID: "other", now: now)) }
    func testExpired() throws { XCTAssertThrowsError(try verifier.verify(token(exp: now.timeIntervalSince1970 - 1), deviceID: device, now: now)) }
    func testExactlyAtExpiryLocks() throws { XCTAssertThrowsError(try verifier.verify(token(exp: now.timeIntervalSince1970), deviceID: device, now: now)) }
    func testFourteenDayOfflineGrace() throws {
        let value = try token()
        XCTAssertNoThrow(try verifier.verify(value, deviceID: device, now: now.addingTimeInterval(14 * 86400 - 1)))
        XCTAssertThrowsError(try verifier.verify(value, deviceID: device, now: now.addingTimeInterval(14 * 86400 + 1)))
    }
    func testFutureIssuedAtRejected() throws { XCTAssertThrowsError(try verifier.verify(token(iat: now.timeIntervalSince1970 + 86401), deviceID: device, now: now)) }
    func testProductionPlaceholderFailsClosed() throws { XCTAssertThrowsError(try IslandGateVerifier(publicKey: IslandGateEnvironment.productionKey).verify(token(), deviceID: device, now: now)) }
    func testProductionKeyPlaceholderDocumentedForReleaseChecklist() {
        XCTAssertEqual(IslandGateEnvironment.productionKey, Data(repeating: 0, count: 32))
    }
    func testReleaseOverridesIgnored() {
        let name = "IslandGateTests.\(UUID())"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set("http://localhost:19000", forKey: "ACUGateDevBaseURL")
        defaults.set(key.publicKey.rawRepresentation.base64EncodedString(), forKey: "ACUGateDevPublicKey")
        let config = IslandGateEnvironment(defaults: defaults, environment: ["ACU_GATE_DEV": "1"], allowDevelopment: false)
        XCTAssertEqual(config.baseURL, IslandGateEnvironment.productionURL)
        XCTAssertEqual(config.publicKey, IslandGateEnvironment.productionKey)
        let noOptIn = IslandGateEnvironment(defaults: defaults, environment: [:])
        XCTAssertEqual(noOptIn.baseURL, IslandGateEnvironment.productionURL)
    }
    func testPKCE() throws {
        let pkce = try IslandGatePKCE()
        XCTAssertEqual(Data(gateBase64URL: pkce.verifier)?.count, 32)
        XCTAssertEqual(pkce.challenge, Data(SHA256.hash(data: Data(pkce.verifier.utf8))).gateBase64URL)
        XCTAssertNotEqual(pkce.verifier, try IslandGatePKCE().verifier)
    }
    func testDeviceIdentifier() {
        XCTAssertEqual(IslandGateDevice.identifier(platformUUID: "test"), SHA256.hash(data: Data("acu-island-gate|test".utf8)).map { String(format: "%02x", $0) }.joined())
    }
    func testDeviceSignaturesUseExactMessagesAndDER() throws {
        let device = IslandGateDevice(id: self.device, key: P256.Signing.PrivateKey())
        for action in ["refresh", "rebind"] {
            let body = try device.signedBody(action: action, timestamp: 1800000000)
            let signature = try P256.Signing.ECDSASignature(derRepresentation: Data(base64Encoded: body["sig"] as! String)!)
            let expected = Data("acu-gate-\(action)|\(self.device)|1800000000".utf8)
            XCTAssertTrue(device.key.publicKey.isValidSignature(signature, for: expected))
            XCTAssertFalse(device.key.publicKey.isValidSignature(signature, for: Data("wrong".utf8)))
            XCTAssertEqual(body["device_id"] as? String, self.device)
        }
    }
    func testKeychainRoundTrip() throws {
        let store = IslandGateKeychain(service: "com.ingpoc.acu.island-gate.test.\(UUID())")
        defer { try? store.delete("test") }
        XCTAssertNil(try store.read("test"))
        try store.write(Data("first".utf8), name: "test")
        XCTAssertEqual(try store.read("test"), Data("first".utf8))
        try store.write(Data("second".utf8), name: "test")
        XCTAssertEqual(try store.read("test"), Data("second".utf8))
        try store.delete("test")
        XCTAssertNil(try store.read("test"))
    }
}
