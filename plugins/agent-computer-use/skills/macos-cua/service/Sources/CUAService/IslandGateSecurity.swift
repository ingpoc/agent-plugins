import CryptoKit
import Foundation
import IOKit
import Security

extension Data {
    var gateBase64URL: String { base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "") }
    init?(gateBase64URL: String) {
        let s = gateBase64URL.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        self.init(base64Encoded: s + String(repeating: "=", count: (4 - s.count % 4) % 4))
    }
}

struct IslandGateEnvironment {
    static let productionURL = URL(string: "https://acu-gate.onrender.com")!
    // SET AT RELEASE: fail closed until the production Ed25519 public key is pinned.
    static let productionKey = Data(repeating: 0, count: 32)
    let baseURL: URL
    let publicKey: Data

    init(defaults: UserDefaults = UserDefaults(suiteName: "com.ingpoc.cua-service") ?? .standard, environment: [String: String] = ProcessInfo.processInfo.environment,
         allowDevelopment: Bool = IslandGateEnvironment.isDebug) {
        var url = Self.productionURL
        var key = Self.productionKey
        #if DEBUG
        if allowDevelopment && environment["ACU_GATE_DEV"] == "1" {
            if let value = defaults.string(forKey: "ACUGateDevBaseURL"), let candidate = URL(string: value),
               ["http", "https"].contains(candidate.scheme ?? ""), candidate.host != nil { url = candidate }
            let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/acu-gate/dev-public-key.txt")
            let value = defaults.string(forKey: "ACUGateDevPublicKey") ?? (try? String(contentsOf: file, encoding: .utf8))
            if let value, let data = Data(base64Encoded: value.trimmingCharacters(in: .whitespacesAndNewlines)), data.count == 32 { key = data }
        }
        #endif
        baseURL = url; publicKey = key
    }
    static var isDebug: Bool {
        #if DEBUG
        true
        #else
        false
        #endif
    }
}

struct IslandGateEntitlement: Codable, Equatable {
    let v: Int
    let acct: String
    let prov: String
    let handle: String
    let device_id: String
    let iat: Double
    let exp: Double
}

struct IslandGateVerifier {
    let publicKey: Data
    func verify(_ token: String, deviceID: String, now: Date = Date()) throws -> IslandGateEntitlement {
        let parts = token.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 2, publicKey != IslandGateEnvironment.productionKey,
              let payload = Data(gateBase64URL: String(parts[0])), let signature = Data(gateBase64URL: String(parts[1])),
              let key = try? Curve25519.Signing.PublicKey(rawRepresentation: publicKey),
              key.isValidSignature(signature, for: Data(parts[0].utf8)) else { throw IslandGateFailure(code: "invalid_entitlement") }
        let claim = try JSONDecoder().decode(IslandGateEntitlement.self, from: payload)
        guard claim.v == 1, claim.device_id == deviceID, ["x", "linkedin"].contains(claim.prov),
              claim.iat <= now.timeIntervalSince1970 + 86400, claim.iat < claim.exp,
              now.timeIntervalSince1970 < claim.exp else { throw IslandGateFailure(code: "invalid_entitlement") }
        return claim
    }
}

protocol IslandGateStoring {
    func read(_ name: String) throws -> Data?
    func write(_ data: Data, name: String) throws
    func delete(_ name: String) throws
}

struct IslandGateKeychain: IslandGateStoring {
    let service: String
    init(service: String = "com.ingpoc.acu.island-gate") { self.service = service }
    private func query(_ name: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: name]
    }
    func read(_ name: String) throws -> Data? {
        var q = query(name); q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw IslandGateFailure(code: "keychain") }
        return data
    }
    func write(_ data: Data, name: String) throws {
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        let status = SecItemUpdate(query(name) as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            let q = query(name).merging(attributes) { _, new in new }
            guard SecItemAdd(q as CFDictionary, nil) == errSecSuccess else { throw IslandGateFailure(code: "keychain") }
        } else if status != errSecSuccess { throw IslandGateFailure(code: "keychain") }
    }
    func delete(_ name: String) throws {
        let status = SecItemDelete(query(name) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw IslandGateFailure(code: "keychain") }
    }
}

struct IslandGateDevice {
    let id: String
    let key: P256.Signing.PrivateKey
    var publicKey: String { key.publicKey.x963Representation.base64EncodedString() }
    static func identifier(platformUUID: String) -> String {
        SHA256.hash(data: Data(("acu-island-gate|" + platformUUID).utf8)).map { String(format: "%02x", $0) }.joined()
    }
    static func load(store: IslandGateStoring) throws -> Self {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        guard service != 0 else { throw IslandGateFailure(code: "device_identity") }
        defer { IOObjectRelease(service) }
        guard let uuid = IORegistryEntryCreateCFProperty(service, "IOPlatformUUID" as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() as? String else { throw IslandGateFailure(code: "device_identity") }
        let key: P256.Signing.PrivateKey
        if let data = try store.read("device-key") { key = try P256.Signing.PrivateKey(rawRepresentation: data) }
        else { key = P256.Signing.PrivateKey(); try store.write(key.rawRepresentation, name: "device-key") }
        return Self(id: identifier(platformUUID: uuid), key: key)
    }
    static func message(action: String, deviceID: String, timestamp: Int64) -> String { "acu-gate-\(action)|\(deviceID)|\(timestamp)" }
    func signedBody(action: String, timestamp: Int64 = Int64(Date().timeIntervalSince1970)) throws -> [String: Any] {
        let message = Self.message(action: action, deviceID: id, timestamp: timestamp)
        return ["device_id": id, "ts": timestamp, "sig": try key.signature(for: Data(message.utf8)).derRepresentation.base64EncodedString()]
    }
}

struct IslandGatePKCE {
    let verifier: String
    var challenge: String { Data(SHA256.hash(data: Data(verifier.utf8))).gateBase64URL }
    init() throws {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw IslandGateFailure(code: "random") }
        verifier = Data(bytes).gateBase64URL
    }
}
