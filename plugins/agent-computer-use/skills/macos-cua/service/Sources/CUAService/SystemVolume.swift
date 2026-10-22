import Foundation

@MainActor
enum SystemVolume {
    static var onChange: ((IslandHUDState) -> Void)?

    static func adjust(_ direction: String) async -> [String: Any] {
        guard let device = SystemAudio.defaultOutput(), SystemAudio.canSetVolume(device) else {
            return ["ok": false, "error_type": "volume_unavailable", "error": "This output device does not expose system volume control."]
        }
        return await adjust(direction, read: { SystemAudio.volume(device) },
                            write: { SystemAudio.setVolume(device, $0) },
                            muted: { SystemAudio.muted(device) }, unmute: { SystemAudio.setMuted(device, false) })
    }

    static func adjust(_ direction: String, read: () -> Double?, write: (Double) -> Bool,
                       muted: () -> Bool, unmute: () -> Bool) async -> [String: Any] {
        guard ["up", "down", "read"].contains(direction), let before = read(), before.isFinite else {
            return ["ok": false, "error_type": "volume_unavailable"]
        }
        if direction == "read" { return ["ok": true, "level": before, "muted": muted(), "verified": true] }
        let target = max(0, min(1, before + (direction == "up" ? 1.0 / 16 : -1.0 / 16)))
        if direction == "up", muted(), !unmute() { return ["ok": false, "error_type": "volume_write_failed"] }
        guard write(target) else { return ["ok": false, "error_type": "volume_write_failed"] }
        var after = read()
        for _ in 0..<5 {
            if let value = after, abs(value - target) <= 0.005 { break }
            try? await Task.sleep(nanoseconds: 30_000_000)
            after = read()
        }
        guard let after, abs(after - target) <= 0.005, direction != "up" || !muted() else {
            return ["ok": false, "dispatched": true, "verified": false, "error_type": "volume_unverified"]
        }
        onChange?(IslandHUDState(kind: .volume, value: after, muted: muted()))
        return ["ok": true, "verified": true, "before": before, "level": after,
                "at_limit": target == before, "muted": muted()]
    }
}
