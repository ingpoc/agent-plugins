import AppKit
import Foundation
import Security
import SwiftUI

struct CredentialRequest: Identifiable, Equatable {
    let id: String
    let catalogID: String
    let label: String
    let service: String
    let account: String
}

/// Only metadata crosses the socket. The entered value stays in the native view and Keychain.
@MainActor
final class CredentialEntryController {
    static let shared = CredentialEntryController()
    private(set) var active: CredentialRequest?
    private var receipt: [String: Any] = [:]
    private var expiry: Task<Void, Never>?
    var onChange: ((CredentialRequest?) -> Void)?
    var write: (CredentialRequest, String, String) throws -> Void = CredentialKeychain.write

    func request(_ fields: [String: Any], timeout: TimeInterval = 120) -> [String: Any] {
        guard active == nil else { return ["ok": false, "error": "credential_entry_busy"] }
        let allowed: Set<String> = ["id", "label", "service", "account", "platform", "env"]
        guard Set(fields.keys).isSubset(of: allowed),
              let id = fields["id"] as? String,
              id.range(of: "^[a-z0-9][a-z0-9._-]{1,63}$", options: .regularExpression) != nil,
              let label = fields["label"] as? String, Self.valid(label),
              let service = fields["service"] as? String, Self.valid(service) else {
            return ["ok": false, "error": "invalid_credential_metadata"]
        }
        let account = fields["account"] as? String ?? ""
        guard account.isEmpty || Self.valid(account) else { return ["ok": false, "error": "invalid_account"] }
        let request = CredentialRequest(id: UUID().uuidString, catalogID: id, label: label, service: service, account: account)
        active = request
        receipt = ["ok": true, "request_id": request.id, "status": "pending"]
        onChange?(request)
        expiry?.cancel()
        expiry = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, timeout) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.cancel(request.id, status: "expired")
        }
        return receipt
    }

    func status(_ id: String) -> [String: Any] {
        guard receipt["request_id"] as? String == id else { return ["ok": false, "error": "unknown_credential_request"] }
        return receipt
    }

    @discardableResult
    func cancel(_ id: String, status: String = "cancelled") -> [String: Any] {
        guard active?.id == id else { return ["ok": false, "error": "unknown_credential_request"] }
        finish(["ok": false, "request_id": id, "status": status, "error": "credential_entry_\(status)"])
        return receipt
    }

    func cancelActive() {
        if let id = active?.id { cancel(id) }
    }

    /// Save is the user's explicit approval to add or replace this service/account's value.
    func save(_ id: String, account: String, value: String) -> String? {
        guard let request = active, request.id == id else { return "This request is no longer active." }
        guard Self.valid(account), !value.isEmpty, value.utf8.count <= 32_768 else { return "Enter an account and credential." }
        do {
            try write(request, account, value)
            finish(["ok": true, "request_id": id, "status": "saved", "stored": true,
                    "id": request.catalogID, "label": request.label, "service": request.service, "account": account])
            return nil
        } catch {
            // Keep the form for a deliberate retry; never forward entered data or an exception string.
            return "Keychain could not save this credential. Try again or cancel."
        }
    }

    private func finish(_ result: [String: Any]) {
        expiry?.cancel(); expiry = nil
        receipt = result
        active = nil
        onChange?(nil)
    }

    static func valid(_ text: String) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.utf8.count <= 256
            && !text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }
}

enum CredentialKeychain {
    static func write(_ request: CredentialRequest, _ account: String, _ value: String) throws {
        var login: SecKeychain?
        var status = SecKeychainOpen(NSHomeDirectory() + "/Library/Keychains/login.keychain-db", &login)
        guard status == errSecSuccess, let login else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                  kSecAttrService as String: request.service, kSecAttrAccount as String: account,
                                  kSecMatchSearchList as String: [login]]
        let data = Data(value.utf8)
        let changes: [String: Any] = [kSecValueData as String: data, kSecAttrLabel as String: request.label]
        status = SecItemUpdate(base as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            guard let access = KeychainTokenStore.access(label: request.label) else {
                throw NSError(domain: NSOSStatusErrorDomain, code: Int(errSecParam))
            }
            var add = base.merging(changes) { _, new in new }
            add.removeValue(forKey: kSecMatchSearchList as String)
            add[kSecUseKeychain as String] = login
            add[kSecAttrAccess as String] = access
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        var query = base
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, result as? Data == data else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status == errSecSuccess ? errSecInternalError : status))
        }
    }
}

struct CredentialEntryView: View {
    let request: CredentialRequest
    let model: IslandModel
    @State private var account = ""
    @State private var value = ""
    @State private var error = ""
    private enum Field { case account, value }
    @FocusState private var focused: Field?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Save credential", systemImage: "lock.fill").font(.system(size: 16, weight: .semibold))
            Text(request.label).font(.system(size: 13, weight: .medium)).lineLimit(1)
            Text("Service: \(request.service)").font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            TextField("Account or username", text: $account).textFieldStyle(.roundedBorder)
                .accessibilityLabel("Credential account").focused($focused, equals: .account)
                .simultaneousGesture(TapGesture().onEnded { model.beginTextEntry(); focused = .account })
            SecureField("Password or API key", text: $value).textFieldStyle(.roundedBorder)
                .focused($focused, equals: .value).privacySensitive().accessibilityLabel("Credential value")
                .simultaneousGesture(TapGesture().onEnded { model.beginTextEntry(); focused = .value })
                .onSubmit(save)
            Text(error.isEmpty ? "Saved in your login Keychain. Saving replaces the value for this service and account." : error)
                .font(.system(size: 11)).foregroundStyle(error.isEmpty ? .secondary : Color.red)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Cancel") { CredentialEntryController.shared.cancel(request.id) }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Save to Keychain", action: save).buttonStyle(.borderedProminent)
                    .disabled(value.isEmpty || !CredentialEntryController.valid(account))
            }
        }
        .padding(20).frame(width: IslandBarLayout.cardSize.width, height: 292, alignment: .topLeading)
        .foregroundStyle(.white).preferredColorScheme(.dark)
        .contentShape(Rectangle())
        .onAppear { account = request.account }
        .onDisappear { value = ""; account = ""; focused = nil }
    }

    private func save() {
        error = CredentialEntryController.shared.save(request.id, account: account, value: value) ?? ""
        if error.isEmpty { value = "" }
    }
}
