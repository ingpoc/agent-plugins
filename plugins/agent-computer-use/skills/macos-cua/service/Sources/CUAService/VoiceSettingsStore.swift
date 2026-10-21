import Foundation

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

        static let `default` = Settings(
            realtimeModel: "gpt-realtime-2",
            realtimeVoice: "alloy",
            eagerness: "balanced",
            micProfile: "near_field"
        )
    }

    static let models: [(value: String, label: String)] = [
        ("gpt-realtime-2", "gpt-realtime-2"),
        ("gpt-realtime-2.1-mini", "gpt-realtime-2.1 mini"),
    ]

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
        ]
    }
}
