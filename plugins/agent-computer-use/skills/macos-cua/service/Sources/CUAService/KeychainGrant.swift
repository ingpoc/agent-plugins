import Foundation
import Security

/// The Keychain items Agent Computer Use actually reads, and the onboarding "Keychain access" row.
/// Grant reads each once through its real reader so every "Always Allow" prompt (macOS's own
/// dialog — never mirrored) happens up front; status comes from per-item flags + metadata-only
/// lookups, so checking it never reads a secret or prompts.
///
/// Readers:
/// - Island tokens (service `samantha.island`, account samantha/grok/cursor/codex): CUAService
///   in-process (hub request verification, Settings masked token / copy). Also `bin/island-post`
///   and the voice helper's island hub via `/usr/bin/security`, which the item's ACL already trusts.
/// - OpenAI key (label "Open AI Realtime Key" or the catalog's other openai-api labels) and xAI key
///   (label `xai-api`): the voice-cua helper via `/usr/bin/security find-generic-password -w`.
enum KeychainGrant {
    struct Item: Equatable {
        let id: String
        let query: [String: String]   // kSecAttr* match (metadata)
        let viaSecurityCLI: Bool      // read through /usr/bin/security (the helper's reader)
        static func == (a: Item, b: Item) -> Bool { a.id == b.id }
    }

    static let openAILabels = ["Open AI Realtime Key", "VoiceCUA OpenAI API Key", "OpenAI Realtime Key", "OpenAI API Key"]
    static let xaiLabels = ["xai-api"]
    static func islandID(_ source: String) -> String { "island." + source }
    static let flagPrefix = "acu.keychain.read."

    nonisolated(unsafe) static var defaults: UserDefaults = .standard
    /// Tests: replaces metadata lookups (attributes → exists).
    nonisolated(unsafe) static var existsOverride: (([String: String]) -> Bool)?

    /// Names (never values) the Python runtime may read without prompting: voice_cua.catalog
    /// `keychain_get_approved`, island_hub and bin/island-post check this file first.
    nonisolated(unsafe) static var approvedFile = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".cache/macos-cua/keychain-approved.json")

    nonisolated(unsafe) static var approvedFileOverride = false

    static func markRead(_ id: String, label: String? = nil) {
        defaults.set(true, forKey: flagPrefix + id)
        if let label { defaults.set(label, forKey: flagPrefix + id + ".label") }
        writeApproved()
    }

    static func writeApproved() {
        // Only the installed app writes it (never a test runner).
        guard Bundle.main.bundleIdentifier == AppVersion.bundleID || approvedFileOverride else { return }
        let all = defaults.dictionaryRepresentation().keys.filter { $0.hasPrefix(flagPrefix) && !$0.hasSuffix(".label") }
        let ids = all.filter { defaults.bool(forKey: $0) }.map { String($0.dropFirst(flagPrefix.count)) }.sorted()
        let labels = ids.compactMap { defaults.string(forKey: flagPrefix + $0 + ".label") }.sorted()
        guard let data = try? JSONSerialization.data(withJSONObject: ["ids": ids, "labels": labels], options: [.sortedKeys]) else { return }
        try? FileManager.default.createDirectory(at: approvedFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: approvedFile, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: approvedFile.path)
    }
    static func wasRead(_ id: String) -> Bool { defaults.bool(forKey: flagPrefix + id) }

    /// Metadata-only lookup (kSecReturnAttributes, never kSecReturnData): cannot prompt.
    static func exists(_ attrs: [String: String]) -> Bool {
        if let o = existsOverride { return o(attrs) }
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecMatchLimit as String: kSecMatchLimitOne,
                                kSecReturnAttributes as String: true]
        for (k, v) in attrs { q[k] = v }
        return SecItemCopyMatching(q as CFDictionary, nil) == errSecSuccess
    }

    /// The items present on this Mac (first existing label per API key, like the helper).
    static func items() -> [Item] {
        var out: [Item] = AgentSource.allCases.compactMap { s in
            let q = [kSecAttrService as String: KeychainTokenStore.service, kSecAttrAccount as String: s.rawValue]
            return exists(q) ? Item(id: islandID(s.rawValue), query: q, viaSecurityCLI: false) : nil
        }
        for (id, labels) in [("openai", openAILabels), ("xai", xaiLabels)] {
            if let l = labels.first(where: { exists([kSecAttrLabel as String: $0]) }) {
                out.append(Item(id: id, query: [kSecAttrLabel as String: l], viaSecurityCLI: true))
            }
        }
        return out
    }

    /// Granted once every present item has been read successfully by its reader.
    static func status() -> PermissionState {
        items().allSatisfy { wasRead($0.id) } ? .granted : .missing
    }

    /// Grant: one real read per item (the system shows its own prompt if the ACL needs it).
    /// Secrets are discarded immediately; nothing is logged or passed on argv.
    static func readAll() {
        for item in items() {
            let ok: Bool
            if item.viaSecurityCLI, let label = item.query[kSecAttrLabel as String] {
                ok = readViaSecurity(label: label)
            } else if let source = item.query[kSecAttrAccount as String] {
                ok = KeychainTokenStore().reload(source: source) != nil
            } else { ok = false }
            if ok { markRead(item.id, label: item.query[kSecAttrLabel as String]) }
        }
    }

    /// Same command the voice helper runs; output goes to a pipe and is dropped.
    static func readViaSecurity(label: String) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-l", label, "-w"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        _ = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return p.terminationStatus == 0
    }
}
