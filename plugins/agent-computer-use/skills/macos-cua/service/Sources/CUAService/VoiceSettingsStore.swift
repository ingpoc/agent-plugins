import Foundation
import Security

/// Shared Samantha preferences — same file as Python `voice_settings.py`.
/// Edited in Settings → Samantha → Voice (the separate Samantha Settings window is gone).
enum VoiceSettingsStore {
    nonisolated(unsafe) static var path: URL = {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".config/voice-cua/settings.json")
    }()

    struct Settings: Equatable {
        var realtimeModel: String
        var realtimeVoice: String
        var eagerness: String
        var micProfile: String
        var liveBulkModel: String = "gpt-6-luna"
        var liveIntelligenceModel: String = "gpt-6.1-sol"

        static let `default` = Settings(
            realtimeModel: "gpt-realtime-2",
            realtimeVoice: "alloy",
            eagerness: "balanced",
            micProfile: "near_field"
        )
    }

    static let liveModel = "gpt-live-1"
    static let grokModel = "grok-voice-latest"

    /// Same ids as `voice_settings.MODELS`. Choosing one switches the backend (providers.py).
    static var models: [(value: String, label: String)] {
        [
            ("gpt-realtime-2", "gpt-realtime-2"),
            ("gpt-realtime-2.1", "gpt-realtime-2.1"),
            ("gpt-realtime-2.1-mini", "gpt-realtime-2.1 mini"),
            (grokModel, xaiKeyAvailable() ? "Grok Voice 3.0" : "Grok Voice 3.0 (needs API key)"),
            (liveModel, "GPT-Live-1"),
        ]
    }

    /// GPT-Live-1 delegation backends (Responses model ids, developers.openai.com).
    static let backendModels: [(value: String, label: String)] = [
        ("gpt-6-luna", "GPT-6 Luna"),
        ("gpt-6-sol", "GPT-6 Sol"),
        ("gpt-6.1-sol", "GPT-6.1 Sol"),
        ("gpt-6-astra", "GPT-6 Astra"),
    ]

    /// Metadata-only check for the xAI key (label `xai-api` in the login Keychain, or XAI_API_KEY):
    /// asks for attributes, never the secret, so it can't raise a Keychain prompt.
    nonisolated(unsafe) static var xaiKeyAvailable: () -> Bool = {
        if let v = ProcessInfo.processInfo.environment["XAI_API_KEY"], !v.isEmpty { return true }
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrLabel as String: "xai-api",
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnAttributes as String: true,
        ]
        return SecItemCopyMatching(q as CFDictionary, nil) == errSecSuccess
    }

    static let voices: [(value: String, label: String)] = [
        ("alloy", "Alloy"),
        ("ash", "Ash"),
        ("ballad", "Ballad"),
        ("coral", "Coral"),
        ("echo", "Echo"),
        ("sage", "Sage"),
        ("shimmer", "Shimmer"),
        ("verse", "Verse"),
    ]

    static let eagernessLevels: [(value: String, label: String)] = [
        ("polite", "Polite"),
        ("balanced", "Balanced"),
        ("eager", "Eager"),
    ]

    static let micProfiles: [(value: String, label: String)] = [
        ("near_field", "Close (headset)"),
        ("far_field", "Far (laptop, room)"),
    ]

    /// Settings-window bridge: `voice.<json key>` ↔ settings.json (unknown keys keep their stored value).
    static func value(for key: String) -> String {
        let s = load()
        switch key {
        case "voice.realtime_model": return s.realtimeModel
        case "voice.realtime_voice": return s.realtimeVoice
        case "voice.eagerness": return s.eagerness
        case "voice.mic_profile": return s.micProfile
        case "voice.live_bulk_model": return s.liveBulkModel
        case "voice.live_intelligence_model": return s.liveIntelligenceModel
        default: return ""
        }
    }

    static func set(_ value: String, for key: String) {
        var s = load()
        switch key {
        case "voice.realtime_model": s.realtimeModel = value
        case "voice.realtime_voice": s.realtimeVoice = value
        case "voice.eagerness": s.eagerness = value
        case "voice.mic_profile": s.micProfile = value
        case "voice.live_bulk_model": s.liveBulkModel = value
        case "voice.live_intelligence_model": s.liveIntelligenceModel = value
        default: return
        }
        try? save(s)
    }

    static func load() -> Settings {
        guard FileManager.default.fileExists(atPath: path.path),
              let data = try? Data(contentsOf: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return .default
        }
        var s = Settings.default
        if let v = obj["realtime_model"] as? String, !v.isEmpty { s.realtimeModel = v }
        if let v = obj["realtime_voice"] as? String, !v.isEmpty { s.realtimeVoice = v }
        if let v = obj["eagerness"] as? String, !v.isEmpty { s.eagerness = v }
        if let v = obj["mic_profile"] as? String, !v.isEmpty { s.micProfile = v }
        if let v = obj["live_bulk_model"] as? String, !v.isEmpty { s.liveBulkModel = v }
        if let v = obj["live_intelligence_model"] as? String, !v.isEmpty { s.liveIntelligenceModel = v }
        return s
    }

    static func save(_ settings: Settings) throws {
        let dir = path.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // Merge: keys this app doesn't know (written by voice_settings.py) are kept.
        var payload: [String: Any] = (try? Data(contentsOf: path))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        payload["realtime_model"] = settings.realtimeModel
        payload["realtime_voice"] = settings.realtimeVoice
        payload["eagerness"] = settings.eagerness
        payload["mic_profile"] = settings.micProfile
        payload["live_bulk_model"] = settings.liveBulkModel
        payload["live_intelligence_model"] = settings.liveIntelligenceModel
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: path, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
    }

    static func applyToEnvironment(_ settings: Settings) -> [String: String] {
        [
            "VOICE_CUA_REALTIME_MODEL": settings.realtimeModel,
            "VOICE_CUA_REALTIME_VOICE": settings.realtimeVoice,
            "VOICE_CUA_EAGERNESS": settings.eagerness,
            "VOICE_CUA_MIC_PROFILE": settings.micProfile,
            "VOICE_CUA_LIVE_BULK_MODEL": settings.liveBulkModel,
            "VOICE_CUA_LIVE_INTELLIGENCE_MODEL": settings.liveIntelligenceModel,
        ]
    }
}
