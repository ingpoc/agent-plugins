import Foundation

/// Shared Samantha preferences — same file as Python `voice_settings.py`.
@MainActor
enum VoiceSettingsStore {
    static let path: URL = {
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

    static let models: [(id: String, label: String)] = [
        ("gpt-realtime-2", "Intelligent — gpt-realtime-2"),
        ("gpt-realtime-2.1-mini", "Balanced — gpt-realtime-2.1-mini"),
    ]

    static let voices: [(id: String, label: String)] = [
        ("alloy", "Alloy"),
        ("ash", "Ash"),
        ("ballad", "Ballad"),
        ("coral", "Coral"),
        ("echo", "Echo"),
        ("sage", "Sage"),
        ("shimmer", "Shimmer"),
        ("verse", "Verse"),
    ]

    static let eagernessLevels: [(id: String, label: String)] = [
        ("polite", "Polite — ignore background noise"),
        ("balanced", "Balanced — clear speech only"),
        ("eager", "Eager — barge in anytime"),
    ]

    static let micProfiles: [(id: String, label: String)] = [
        ("near_field", "Close mic — headset / AirPods"),
        ("far_field", "Far mic — laptop / room"),
    ]

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
        let payload: [String: String] = [
            "realtime_model": settings.realtimeModel,
            "realtime_voice": settings.realtimeVoice,
            "eagerness": settings.eagerness,
            "mic_profile": settings.micProfile,
        ]
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
