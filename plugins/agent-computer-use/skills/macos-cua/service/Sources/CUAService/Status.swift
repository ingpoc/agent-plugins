import AVFoundation
import AppKit
import ApplicationServices
import ArgumentParser
import CoreGraphics
import Darwin
import Foundation
import IOKit
import LocalAuthentication
import Security

struct IslandGateStatusKeychain: IslandGateStoring {
    private let service = "com.ingpoc.acu.island-gate"

    func read(_ name: String) throws -> Data? {
        let authenticationContext = LAContext()
        authenticationContext.interactionNotAllowed = true
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: name,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationContext as String: authenticationContext,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw IslandGateFailure(code: "keychain")
        }
        return data
    }

    func write(_ data: Data, name: String) throws { throw IslandGateFailure(code: "read_only") }
    func delete(_ name: String) throws { throw IslandGateFailure(code: "read_only") }
}

struct CUAServiceStatus: Encodable, Equatable {
    struct Permissions: Encodable, Equatable {
        let accessibility: Bool
        let screen_recording: Bool
        let microphone: String
    }

    struct Island: Encodable, Equatable {
        let state: String
        let provider: String?
        let handle: String?
        let expires_at: String?

        private enum CodingKeys: String, CodingKey { case state, provider, handle, expires_at }
        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(state, forKey: .state)
            try container.encode(provider, forKey: .provider)
            try container.encode(handle, forKey: .handle)
            try container.encode(expires_at, forKey: .expires_at)
        }
    }

    let version: String
    let build: String
    let bundleId: String
    let path: String
    let running: Bool
    let permissions: Permissions
    let island: Island
    let ok: Bool

    static func microphoneStatus(_ status: AVAuthorizationStatus) -> String {
        switch status {
        case .authorized: return "granted"
        case .notDetermined: return "not_determined"
        case .denied, .restricted: return "denied"
        @unknown default: return "denied"
        }
    }

    static func isReady(running: Bool, permissions: Permissions) -> Bool {
        running && permissions.accessibility && permissions.screen_recording && permissions.microphone == "granted"
    }

    static func islandStatus(store: IslandGateStoring, now: Date = Date()) -> Island {
        do {
            guard let tokenData = try store.read("entitlement") else {
                if try store.read("claim-id") != nil {
                    return Island(state: "checking", provider: nil, handle: nil, expires_at: nil)
                }
                return Island(state: "locked", provider: nil, handle: nil, expires_at: nil)
            }
            guard
                  let token = String(data: tokenData, encoding: .utf8),
                  let accountData = try store.read("account"),
                  let account = try? JSONDecoder().decode(IslandGateAccount.self, from: accountData) else {
                return Island(state: "unknown", provider: nil, handle: nil, expires_at: nil)
            }
            let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
            guard service != 0 else {
                return Island(state: "unknown", provider: account.provider, handle: account.handle, expires_at: nil)
            }
            defer { IOObjectRelease(service) }
            guard let uuid = IORegistryEntryCreateCFProperty(service, "IOPlatformUUID" as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() as? String else {
                return Island(state: "unknown", provider: account.provider, handle: account.handle, expires_at: nil)
            }
            let deviceID = IslandGateDevice.identifier(platformUUID: uuid)
            guard let payloadPart = token.split(separator: ".", omittingEmptySubsequences: false).first,
                  Data(gateBase64URL: String(payloadPart)) != nil,
                  let claim = try? IslandGateVerifier(publicKey: IslandGateEnvironment.productionKey)
                    .verify(token, deviceID: deviceID, now: now),
                  claim.acct == account.id, claim.prov == account.provider, claim.handle == account.handle else {
                let expiry = Self.unverifiedExpiry(token)
                return Island(state: "unknown", provider: account.provider, handle: account.handle, expires_at: expiry.map(Self.iso8601))
            }
            let expiry = Date(timeIntervalSince1970: claim.exp)
            return Island(state: now < expiry ? "unlocked" : "locked", provider: account.provider, handle: account.handle, expires_at: Self.iso8601(expiry))
        } catch {
            return Island(state: "unknown", provider: nil, handle: nil, expires_at: nil)
        }
    }

    private static func iso8601(_ date: Date) -> String {
        ISO8601DateFormatter().string(from: date)
    }

    private static func unverifiedExpiry(_ token: String) -> Date? {
        guard let part = token.split(separator: ".", omittingEmptySubsequences: false).first,
              let data = Data(gateBase64URL: String(part)),
              let claim = try? JSONDecoder().decode(IslandGateEntitlement.self, from: data) else { return nil }
        return Date(timeIntervalSince1970: claim.exp)
    }

    static func serviceIsRunning(socketPath: String = NSString(string: "~/.cache/macos-cua/cua-service.sock").expandingTildeInPath) -> Bool {
        let fd = Darwin.open(socketPath + ".lock", O_RDONLY)
        guard fd >= 0 else { return false }
        defer { Darwin.close(fd) }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 {
            _ = flock(fd, LOCK_UN)
            return false
        }
        return errno == EWOULDBLOCK
    }

    static func current() -> CUAServiceStatus {
        let info = Bundle.main.infoDictionary ?? [:]
        let bundleID = AppVersion.bundleID
        let running = serviceIsRunning()
        let permissions = Permissions(
            accessibility: AXIsProcessTrusted(),
            screen_recording: CGPreflightScreenCaptureAccess(),
            microphone: microphoneStatus(AVCaptureDevice.authorizationStatus(for: .audio))
        )
        return CUAServiceStatus(
            version: info["CFBundleShortVersionString"] as? String ?? AppVersion.short,
            build: info["CFBundleVersion"] as? String ?? "unknown",
            bundleId: bundleID,
            path: Bundle.main.bundleURL.path,
            running: running,
            permissions: permissions,
            island: islandStatus(store: IslandGateStatusKeychain()),
            ok: isReady(running: running, permissions: permissions)
        )
    }
}

struct CUAServiceStatusCommand: ParsableCommand {
    static let configuration = CommandConfiguration(commandName: "status", abstract: "Read service, permission, and island status")
    @Flag(name: .long, help: "Emit machine-readable JSON") var json = false

    func run() throws {
        let status = CUAServiceStatus.current()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(status)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}
