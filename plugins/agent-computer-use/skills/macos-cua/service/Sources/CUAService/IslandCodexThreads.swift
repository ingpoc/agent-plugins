import Foundation
import SQLite3

enum IslandCodexThreads {
    struct RolloutState: Equatable {
        var state = "idle"
        var error: String?
        var model: String?
        var effort: String?
        var tokens: Int?
        var contextPercent: Int?
        /// When the current state began (task_started / task_complete / error event timestamp).
        var since: Date?
    }

    private static let stampFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func stamp(_ object: [String: Any]) -> Date? {
        guard let text = object["timestamp"] as? String else { return nil }
        return stampFormatter.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    /// Inspect only event and context records. Aborted turns are idle unless the reason
    /// explicitly identifies a failure; user cancellation is not an error.
    static func rolloutState(_ tail: Data, age: TimeInterval) -> RolloutState {
        var result = RolloutState()
        for line in tail.split(separator: 10) {
            guard line.range(of: Data("\"event_msg\"".utf8)) != nil ||
                    line.range(of: Data("\"turn_context\"".utf8)) != nil,
                  let object = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                  let type = object["type"] as? String,
                  let payload = object["payload"] as? [String: Any] else { continue }
            if type == "turn_context" {
                result.model = payload["model"] as? String ?? result.model
                result.effort = payload["effort"] as? String ?? result.effort
            } else if type == "event_msg", let event = payload["type"] as? String {
                switch event {
                case "task_started": result.state = "working"; result.error = nil; result.since = stamp(object)
                case "task_complete": result.state = "done"; result.error = nil; result.since = stamp(object)
                case "error":
                    result.since = stamp(object)
                    result.state = "error"
                    result.error = (payload["message"] as? String)?.components(separatedBy: .newlines).first.map { String($0.prefix(60)) }
                case "turn_aborted":
                    let reason = (payload["reason"] as? String ?? "").lowercased()
                    result.state = reason.contains("fail") || reason.contains("error") ? "error" : "idle"
                    result.error = result.state == "error" ? String(reason.prefix(60)) : nil
                case "token_count":
                    if let info = payload["info"] as? [String: Any] {
                        result.tokens = (info["total_token_usage"] as? [String: Any])?["total_tokens"] as? Int
                        if let used = (info["last_token_usage"] as? [String: Any])?["total_tokens"] as? Int,
                           let window = info["model_context_window"] as? Int, window > 0 {
                            result.contextPercent = Int((Double(used) * 100 / Double(window)).rounded())
                        }
                    }
                default: break
                }
            }
        }
        if (result.state == "working" && age > 1800) || (result.state == "done" && age > 180) ||
            (result.state == "error" && age > 1800) { result.state = "idle" }
        return result
    }

    static func productModelName(_ model: String?) -> String? {
        guard let part = model?.split(separator: "-").last, !part.isEmpty else { return nil }
        return part.prefix(1).uppercased() + part.dropFirst().lowercased()
    }
    struct ThreadRecord: Equatable {
        var id: String
        var rolloutPath: String
        var title: String?
        var firstUserMessage: String?
        var model: String?
        var effort: String?
        var updatedAt: Date
        var originator: String? = nil
        var cwd: String? = nil
        var branch: String? = nil
        var originURL: String? = nil
        var parentID: String? = nil
        var role: String? = nil
        var codexHome: String? = nil
        var rollout = RolloutState()
    }

    /// A running `codex` process. App-server hosts (the ChatGPT desktop app spawns them from
    /// ChatGPT.app/.../CodexCLI.app) serve many threads and are never shown as a thread.
    struct Host: Equatable {
        var pid: Int
        var executable: String
        var rollouts: [String]
        var isDesktopApp: Bool { executable.contains("/ChatGPT.app/") || executable.contains("/Codex.app/") }
    }

    static func turnContext(_ tail: Data) -> (model: String?, effort: String?)? {
        var latest: (String?, String?)?
        let marker = Data("\"turn_context\"".utf8)
        for line in tail.split(separator: 10) {
            // Decode only turn_context records; other rollout lines (prompts, tool output) are never parsed.
            guard line.range(of: marker) != nil,
                  let object = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
                  object["type"] as? String == "turn_context",
                  let payload = object["payload"] as? [String: Any] else { continue }
            latest = (payload["model"] as? String, payload["effort"] as? String)
        }
        return latest
    }

    static func rolloutPaths(_ output: String) -> [String] {
        output.split(separator: "\n").compactMap { line in
            guard line.first == "n" else { return nil }
            let path = String(line.dropFirst())
            let url = URL(fileURLWithPath: path)
            guard path.hasPrefix("/"), path.contains("/sessions/"),
                  url.lastPathComponent.hasPrefix("rollout-"), url.pathExtension == "jsonl" else { return nil }
            return path
        }
    }

    static let recentWindow: TimeInterval = 1800
    static let workingWindow: TimeInterval = 90

    static func isProbe(_ record: ThreadRecord) -> Bool {
        let cwd = record.cwd ?? ""
        return URL(fileURLWithPath: cwd).lastPathComponent == "empty-probe" || cwd.contains("/acu-release/") ||
            [record.title, record.firstUserMessage].contains { ($0 ?? "").hasPrefix("Call only the agent") }
    }

    static func displayTitle(_ record: ThreadRecord) -> String {
        let title = record.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let prompt = record.firstUserMessage?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        // Codex stores a clipped prompt as the title; prefer the full prompt then.
        let source = !title.isEmpty && (prompt.isEmpty || !prompt.hasPrefix(title)) ? title : (prompt.isEmpty ? title : prompt)
        let short = shortTitle(source)
        return short.isEmpty ? "Untitled thread" : short
    }

    /// "Audit release notes: run …" → "Audit release notes". Cut at the first colon or sentence
    /// end, then at a word boundary (with "…") past `max` characters; never mid-word.
    static func shortTitle(_ text: String, max: Int = 48) -> String {
        var t = (text.components(separatedBy: .newlines).first ?? "").trimmingCharacters(in: .whitespaces)
        if let r = t.range(of: #":(\s|$)"#, options: .regularExpression),
           t.distance(from: t.startIndex, to: r.lowerBound) >= 3 { t = String(t[..<r.lowerBound]) }
        if let r = t.range(of: #"[.!?](\s|$)"#, options: .regularExpression) {
            let mark = t[r.lowerBound]
            t = String(t[..<r.lowerBound]) + (mark == "." ? "" : String(mark))
        }
        t = t.trimmingCharacters(in: .whitespaces)
        guard t.count > max else { return t }
        let cut = t.prefix(max)
        guard let space = cut.lastIndex(of: " ") else { return String(cut) + "…" }
        return String(cut[..<space]).trimmingCharacters(in: CharacterSet(charactersIn: " ,;-–—")) + "…"
    }

    /// Human words for a turn error; the raw message stays in the tooltip and `sessions`.
    static func humanError(_ raw: String?) -> String {
        let s = (raw ?? "").lowercased()
        if s.contains("rate limit") || s.contains("429") { return "Rate limited" }
        if s.contains("context") { return "Ran out of context" }
        if ["unauthorized", "401", "403", "login", "auth"].contains(where: s.contains) { return "Sign-in needed" }
        if ["network", "stream", "connection", "disconnect", "timed out", "timeout"].contains(where: s.contains) { return "Connection lost" }
        return "Turn failed"
    }

    /// A terminal thread whose process is gone (or a ChatGPT-app thread with no app-server) is
    /// "stopped", never working. Live threads quiet for 10 min are idle.
    static let stoppedGrace: TimeInterval = 20
    static func liveState(_ record: ThreadRecord, hosts: [Host], now: Date) -> String {
        let state = record.rollout.state
        guard state == "working" else { return state }
        let quiet = now.timeIntervalSince(record.updatedAt)
        let desktop = ["Codex Desktop", "codex_work_desktop"].contains(record.originator ?? "")
        let alive = desktop ? hosts.contains { $0.isDesktopApp } : hosts.contains { $0.rollouts.contains(record.rolloutPath) }
        if !alive && quiet > stoppedGrace { return "stopped" }
        return quiet > 600 ? "idle" : "working"
    }

    /// Threads shown as working this run; a stopped thread never seen working is dropped.
    private static let seenLock = NSLock()
    nonisolated(unsafe) private static var seenWorking: Set<String> = []
    static func noteWorking(_ id: String) { seenLock.lock(); seenWorking.insert(id); seenLock.unlock() }
    static func wasWorking(_ id: String) -> Bool { seenLock.lock(); defer { seenLock.unlock() }; return seenWorking.contains(id) }

    static func duration(_ seconds: Int) -> String {
        seconds < 60 ? "\(seconds)s" : seconds < 3600 ? "\(seconds / 60)m" : "\(seconds / 3600)h"
    }

    static func repo(_ record: ThreadRecord) -> String? {
        if let url = record.originURL, !url.isEmpty {
            return URL(fileURLWithPath: url).deletingPathExtension().lastPathComponent
        }
        return record.cwd.map { URL(fileURLWithPath: $0).lastPathComponent }
    }

    static func tokensText(_ tokens: Int) -> String {
        tokens >= 1_000_000 ? String(format: "%.1fM", Double(tokens) / 1_000_000)
            : tokens >= 1000 ? "\(tokens / 1000)k" : "\(tokens)"
    }

    /// Hover text: "repo · branch" and "3.1M tokens · 62% of context", known parts only.
    static func tooltip(_ record: ThreadRecord) -> String? {
        let place = [repo(record), record.branch].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        let usage = [record.rollout.tokens.map { tokensText($0) + " tokens" },
                     record.rollout.contextPercent.map { "\($0)% of context" }].compactMap { $0 }.joined(separator: " · ")
        let text = [place, usage].filter { !$0.isEmpty }.joined(separator: "\n")
        return text.isEmpty ? nil : text
    }

    /// Roster rows: working, done (3 min) and error (30 min) threads only; idle threads, helpers
    /// (folded into the parent as "+N helpers") and ACU's own probe runs never show.
    static func rows(records: [ThreadRecord], hosts: [Host], now: Date) -> [IslandAgent] {
        let helpers = Dictionary(grouping: records.filter { $0.parentID != nil }, by: { $0.parentID! })
        return records.filter { $0.parentID == nil && $0.role == nil && !isProbe($0) }.compactMap { record in
            var state = liveState(record, hosts: hosts, now: now)
            if state == "idle" { return nil }
            if state == "working" { noteWorking(record.id) }
            if state == "stopped" {
                guard wasWorking(record.id) else { return nil }
                state = "error"
            }
            let stopped = state == "error" && record.rollout.state == "working"
            let since = stopped ? record.updatedAt : (record.rollout.since ?? record.updatedAt)
            let age = max(0, Int(now.timeIntervalSince(since)))
            var row = IslandAgent(id: "local:codex-thread:\(record.id)", name: "Codex", app: "Codex",
                                  title: displayTitle(record), status: state == "working" ? "running" : state,
                                  elapsed_s: Double(age), last_seen_at: record.updatedAt.timeIntervalSince1970)
            let desktop = ["Codex Desktop", "codex_work_desktop"].contains(record.originator ?? "")
            var parts = [productModelName(record.rollout.model ?? record.model),
                         effortLabel(record.rollout.effort ?? record.effort, desktop: desktop)].compactMap { $0 }
            if let count = helpers[record.id]?.count, count > 0 { parts.append("+\(count) helpers") }
            switch state {
            case "error": row.local_detail = stopped ? "Stopped" : humanError(record.rollout.error)
            case "done": row.local_detail = (parts + ["done \(duration(age))"]).joined(separator: " · ")
            default: row.local_detail = (parts + [duration(age)]).joined(separator: " · ")
            }
            // https://developers.openai.com/codex/app/commands#deep-links: codex://threads/<id> opens a local thread.
            row.link = "codex://threads/\(record.id)"
            let technical = stopped ? "Stopped: the Codex process exited mid-turn" : state == "error" ? record.rollout.error : nil
            row.tooltip = [technical, tooltip(record)].compactMap { $0 }.joined(separator: "\n")
            if row.tooltip?.isEmpty == true { row.tooltip = nil }
            return row
        }
    }

    /// Effort as the thread's own surface names it. The Codex app and IDE label `low` "Light" and
    /// `xhigh` "Extra High" (developers.openai.com/codex: reasoning effort); the CLI shows the raw value.
    static func effortLabel(_ effort: String?, desktop: Bool) -> String? {
        guard let e = effort?.trimmingCharacters(in: .whitespaces).lowercased(), !e.isEmpty else { return nil }
        guard desktop else { return e }
        return ["low": "light", "xhigh": "extra high"][e] ?? e
    }

    static func hostLabel(_ originator: String?) -> String {
        switch originator ?? "" {
        case "codex_exec", "codex_cli_rs": return "terminal"
        case "Codex Desktop", "codex_work_desktop": return "ChatGPT app"
        default: return "host unknown"
        }
    }

    static func openReadOnly(_ path: String, immutable: Bool = false) -> OpaquePointer? {
        var db: OpaquePointer?
        let uri = URL(fileURLWithPath: path).absoluteString + (immutable ? "?mode=ro&immutable=1" : "?mode=ro")
        guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        return db
    }

    /// Read-only. state_5.sqlite is a WAL database: when no Codex process has it open its -wal/-shm
    /// files are gone and a `mode=ro` open cannot create them (SQLITE_CANTOPEN). Nothing is writing
    /// then, so read once with `immutable=1` (no locks, no side files, never writes).
    static func readDatabase(_ path: String) -> [ThreadRecord] {
        if let records = readDatabase(path, immutable: false) { return records }
        return readDatabase(path, immutable: true) ?? []
    }

    static func readDatabase(_ path: String, immutable: Bool) -> [ThreadRecord]? {
        guard let db = openReadOnly(path, immutable: immutable) else { return nil }
        defer { sqlite3_close(db) }
        var columns: Set<String> = []
        var schema: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(threads)", -1, &schema, nil) == SQLITE_OK else { return nil }
        while sqlite3_step(schema) == SQLITE_ROW {
            if let value = sqlite3_column_text(schema, 1) { columns.insert(String(cString: value)) }
        }
        sqlite3_finalize(schema)
        let wanted = ["id", "rollout_path", "title", "first_user_message", "model", "reasoning_effort", "updated_at_ms", "originator", "cwd", "git_branch", "git_origin_url", "attached_thread_parent_id", "agent_role"]
        guard columns.contains("id"), columns.contains("rollout_path"), columns.contains("updated_at_ms") else { return nil }
        let fields = wanted.map { columns.contains($0) ? $0 : "NULL" }.joined(separator: ", ")
        let filter = columns.contains("archived") ? " WHERE archived = 0" : ""
        let sql = "SELECT \(fields) FROM threads\(filter) ORDER BY updated_at_ms DESC LIMIT 500"
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }
        func string(_ index: Int32) -> String? {
            guard let value = sqlite3_column_text(stmt, index) else { return nil }
            return String(cString: value)
        }
        let homeName = URL(fileURLWithPath: path).deletingLastPathComponent().lastPathComponent
        var records: [ThreadRecord] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            guard let id = string(0), let path = string(1), sqlite3_column_type(stmt, 6) != SQLITE_NULL else { continue }
            records.append(ThreadRecord(id: id, rolloutPath: path, title: string(2), firstUserMessage: string(3),
                                        model: string(4), effort: string(5),
                                        updatedAt: Date(timeIntervalSince1970: Double(sqlite3_column_int64(stmt, 6)) / 1000),
                                        originator: string(7), cwd: string(8), branch: string(9), originURL: string(10),
                                        parentID: string(11), role: string(12), codexHome: homeName))
        }
        return step == SQLITE_DONE ? records : nil
    }

    static func tail(_ path: String) -> Data? {
        guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
        defer { try? handle.close() }
        guard let size = try? handle.seekToEnd() else { return nil }
        // Rollouts open with ~250 KB of instructions, so a short turn's task_started can sit
        // before the last 256 KB; widen to 4 MB when the tail holds no lifecycle event.
        var start = size > 256 * 1024 ? size - 256 * 1024 : 0
        guard (try? handle.seek(toOffset: start)) != nil, var data = try? handle.readToEnd() else { return nil }
        if start > 0, !["task_started", "task_complete", "turn_aborted"].contains(where: {
            data.range(of: Data("\"\($0)\"".utf8)) != nil }) {
            start = size > 4 * 1024 * 1024 ? size - 4 * 1024 * 1024 : 0
            guard (try? handle.seek(toOffset: start)) != nil, let wider = try? handle.readToEnd() else { return nil }
            data = wider
        }
        if start > 0, let newline = data.firstIndex(of: 10) { return Data(data[data.index(after: newline)...]) }
        return data
    }

    static func lsofPaths(_ pid: Int) -> [String]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-a", "-p", String(pid), "-Fn"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        var pending = Data()
        var paths: [String] = []
        while true {
            let chunk = pipe.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            pending.append(chunk)
            while let end = pending.firstIndex(of: 10) {
                let line = String(decoding: pending[..<end], as: UTF8.self)
                paths += rolloutPaths(line)
                pending.removeSubrange(...end)
            }
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        paths += rolloutPaths(String(decoding: pending, as: UTF8.self))
        return paths
    }

    static func scanData(processes: [(pid: Int, executable: String)], home: URL = FileManager.default.homeDirectoryForCurrentUser,
                     readDB: (String) -> [ThreadRecord] = readDatabase,
                     readTail: (String) -> Data? = tail,
                     lsof: (Int) -> [String]? = lsofPaths, now: Date = Date()) -> ([ThreadRecord], [Host]) {
        let directories = (try? FileManager.default.contentsOfDirectory(at: home, includingPropertiesForKeys: nil)) ?? []
        let homes = directories.filter { $0.lastPathComponent == ".codex" || $0.lastPathComponent.hasPrefix(".codex-") }
        var records = homes.flatMap { readDB($0.appendingPathComponent("state_5.sqlite").path) }
            .sorted { $0.updatedAt > $1.updatedAt }
        // lsof only for terminal codex processes; app-server hosts never hold one thread's rollout.
        let hosts = processes.map { process in
            Host(pid: process.pid, executable: process.executable,
                 rollouts: Host(pid: process.pid, executable: process.executable, rollouts: []).isDesktopApp ? [] : (lsof(process.pid) ?? []))
        }
        for index in records.indices {
            let path = records[index].rolloutPath
            guard now.timeIntervalSince(records[index].updatedAt) <= recentWindow + 300 else { continue }
            let modified = (try? FileManager.default.attributesOfItem(atPath: path)[.modificationDate]) as? Date
            let updated = max(records[index].updatedAt, modified ?? records[index].updatedAt)
            records[index].updatedAt = updated
            // Older threads are idle by definition (error rows last 30 min): never tail them.
            if now.timeIntervalSince(updated) <= recentWindow, let data = readTail(path) {
                records[index].rollout = rolloutState(data, age: max(0, now.timeIntervalSince(updated)))
            }
        }
        return (records, hosts)
    }

    static func scan(processes: [(pid: Int, executable: String)], home: URL = FileManager.default.homeDirectoryForCurrentUser,
                     readDB: (String) -> [ThreadRecord] = readDatabase, readTail: (String) -> Data? = tail,
                     lsof: (Int) -> [String]? = lsofPaths, now: Date = Date()) -> [IslandAgent] {
        let (records, hosts) = scanData(processes: processes, home: home, readDB: readDB, readTail: readTail, lsof: lsof, now: now)
        return rows(records: records, hosts: hosts, now: now)
    }
}
