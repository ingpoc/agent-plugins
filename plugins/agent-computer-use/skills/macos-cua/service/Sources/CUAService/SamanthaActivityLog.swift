import Foundation
import Darwin

/// Structured startup timeline — same jsonl as Python `startup_trace.py`.
enum SamanthaActivityLog {
    private static var activityURL: URL {
        // Unit tests drive real controllers; keep their rows out of the user's live timeline.
        if NSClassFromString("XCTestCase") != nil {
            return FileManager.default.temporaryDirectory.appendingPathComponent("cua-tests-samantha-activity.jsonl")
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache/macos-cua/samantha-activity.jsonl")
    }

    static func startup(
        phase: String,
        status: String = "ok",
        sessionId: String = "",
        detail: String = "",
        elapsedMs: Int? = nil,
        stepMs: Int? = nil,
        voiceLogURL: URL? = nil
    ) {
        var payload: [String: Any] = [
            "ts": Date().timeIntervalSince1970,
            "kind": "startup",
            "phase": phase,
            "status": status,
            "source": "cuaservice",
        ]
        if !sessionId.isEmpty { payload["session_id"] = sessionId }
        if !detail.isEmpty { payload["detail"] = detail }
        if let elapsedMs { payload["elapsed_ms"] = elapsedMs }
        if let stepMs { payload["step_ms"] = stepMs }
        appendJSON(payload, to: activityURL)
        var line = "[startup] phase=\(phase) status=\(status)"
        if !sessionId.isEmpty { line += " session_id=\(sessionId)" }
        if let elapsedMs { line += " elapsed_ms=\(elapsedMs)" }
        if let stepMs { line += " step_ms=\(stepMs)" }
        if !detail.isEmpty { line += " detail=\(detail)" }
        line += "\n"
        if let voiceLogURL {
            appendVoiceLog(line, to: voiceLogURL)
        }
    }

    /// Voice supervisor session id (set at supervisor_begin) so island rows join Samantha's session.
    nonisolated(unsafe) static var currentSessionId = ""

    /// One `island_layout` row (rectangles only; built by IslandLayoutLogger).
    static func islandLayout(_ payload: [String: Any]) {
        appendJSON(payload, to: activityURL)
    }

    private static func appendJSON(_ obj: [String: Any], to url: URL) {
        guard JSONSerialization.isValidJSONObject(obj),
              let data = try? JSONSerialization.data(withJSONObject: obj),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        let dir = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        append(line.data(using: .utf8) ?? Data(), to: url)
    }

    private static func appendVoiceLog(_ line: String, to url: URL) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        guard let data = "\(stamp) \(line)".data(using: .utf8) else { return }
        CUALogRotation.append(data, to: url)
    }

    private static func append(_ data: Data, to url: URL) {
        CUALogRotation.append(data, to: url)   // O_APPEND under a shared lock; rotates at 50 MB
    }
}
