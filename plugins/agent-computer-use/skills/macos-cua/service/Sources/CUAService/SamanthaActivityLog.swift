import Foundation
import Darwin

/// Structured startup timeline — same jsonl as Python `startup_trace.py`.
enum SamanthaActivityLog {
    private static var activityURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
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
        let text = "\(stamp) \(line)"
        guard let data = text.data(using: .utf8) else { return }
        if FileManager.default.fileExists(atPath: url.path),
           let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url)
        }
    }

    private static func append(_ data: Data, to url: URL) {
        let fd = Darwin.open(url.path, O_WRONLY | O_CREAT | O_APPEND, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { return }
        defer { Darwin.close(fd) }
        data.withUnsafeBytes { buffer in
            guard let base = buffer.baseAddress else { return }
            _ = Darwin.write(fd, base, buffer.count)
        }
    }
}
