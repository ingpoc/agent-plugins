import Foundation
import SQLite3

enum IslandCodexThreads {
    struct ThreadRecord: Equatable {
        var id: String
        var rolloutPath: String
        var title: String?
        var firstUserMessage: String?
        var model: String?
        var effort: String?
        var updatedAt: Date
        var originator: String? = nil
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

    /// Threads grouped under their host. A thread is "working" only when its rollout updated in
    /// the last 90 s; app-server processes appear only as one small host line per host kind.
    static func rows(records: [ThreadRecord], hosts: [Host], now: Date) -> [IslandAgent] {
        var terminalPID: [String: Int] = [:]
        for host in hosts where !host.isDesktopApp {
            for path in Set(host.rollouts) { terminalPID[path] = host.pid }
        }
        var result: [IslandAgent] = []
        let appHosts = hosts.filter(\.isDesktopApp).map(\.pid).sorted()
        if !appHosts.isEmpty {
            var row = IslandAgent(id: "local:codex-host:app", name: "Codex host", app: "Codex",
                                  title: "ChatGPT app · PID " + appHosts.map(String.init).joined(separator: ", "),
                                  status: "process")
            row.local_detail = "app-server; threads listed below"
            result.append(row)
        }
        let shown = records.filter { terminalPID[$0.rolloutPath] != nil || now.timeIntervalSince($0.updatedAt) <= recentWindow }
        for record in shown {
            let pid = terminalPID[record.rolloutPath]
            let group = pid != nil ? "terminal" : hostLabel(record.originator)
            result.append(threadRow(record, id: "local:codex-thread:\(record.id)", group: group, pid: pid, now: now))
        }
        return result
    }

    static func hostLabel(_ originator: String?) -> String {
        switch originator ?? "" {
        case "codex_exec", "codex_cli_rs": return "terminal"
        case "Codex Desktop", "codex_work_desktop": return "ChatGPT app"
        case "": return "host unknown"
        default: return originator!
        }
    }

    private static func threadRow(_ record: ThreadRecord, id: String, group: String, pid: Int?, now: Date) -> IslandAgent {
        let firstLine = record.firstUserMessage?.components(separatedBy: .newlines).first
        let title = [record.title, firstLine].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty } ?? "Untitled thread"
        let shortTitle = title.count > 48 ? String(title.prefix(47)) + "…" : title
        let seconds = max(0, Int(now.timeIntervalSince(record.updatedAt)))
        let age = seconds < 60 ? "\(seconds)s" : seconds < 3600 ? "\(seconds / 60)m" : "\(seconds / 3600)h"
        var row = IslandAgent(id: id, name: "Codex · " + group, app: "Codex", title: shortTitle, status: "process")
        let activity = Double(seconds) < workingWindow ? "working" : "idle"
        row.local_detail = "\(record.model ?? "model unknown") · \(record.effort ?? "effort unknown") · \(activity) · \(age) ago"
            + (pid.map { " · PID \($0)" } ?? "")
        return row
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
        let sql = "SELECT id, rollout_path, title, first_user_message, model, reasoning_effort, updated_at_ms, originator FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC LIMIT 20"
        var stmt: OpaquePointer?
        // Older state DBs have no originator column; read the rest and leave the host unknown.
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK
            || sqlite3_prepare_v2(db, sql.replacingOccurrences(of: ", originator FROM", with: ", NULL FROM"), -1, &stmt, nil) == SQLITE_OK
        else { return nil }
        defer { sqlite3_finalize(stmt) }
        func string(_ index: Int32) -> String? {
            guard let value = sqlite3_column_text(stmt, index) else { return nil }
            return String(cString: value)
        }
        var records: [ThreadRecord] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            guard let id = string(0), let path = string(1), sqlite3_column_type(stmt, 6) != SQLITE_NULL else { continue }
            records.append(ThreadRecord(id: id, rolloutPath: path, title: string(2), firstUserMessage: string(3),
                                        model: string(4), effort: string(5),
                                        updatedAt: Date(timeIntervalSince1970: Double(sqlite3_column_int64(stmt, 6)) / 1000),
                                        originator: string(7)))
        }
        return step == SQLITE_DONE ? records : nil
    }

    static func tail(_ path: String) -> Data? {
        guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
        defer { try? handle.close() }
        guard let size = try? handle.seekToEnd() else { return nil }
        let start = size > 256 * 1024 ? size - 256 * 1024 : 0
        guard (try? handle.seek(toOffset: start)) != nil, let data = try? handle.readToEnd() else { return nil }
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

    static func scan(processes: [(pid: Int, executable: String)], home: URL = FileManager.default.homeDirectoryForCurrentUser,
                     readDB: (String) -> [ThreadRecord] = readDatabase,
                     readTail: (String) -> Data? = tail,
                     lsof: (Int) -> [String]? = lsofPaths, now: Date = Date()) -> [IslandAgent] {
        guard !processes.isEmpty else { return [] }
        let directories = (try? FileManager.default.contentsOfDirectory(at: home, includingPropertiesForKeys: nil)) ?? []
        let homes = directories.filter { $0.lastPathComponent == ".codex" || $0.lastPathComponent.hasPrefix(".codex-") }
        var records = homes.flatMap { readDB($0.appendingPathComponent("state_5.sqlite").path) }
            .sorted { $0.updatedAt > $1.updatedAt }
        records = Array(records.prefix(20))
        // lsof only for terminal codex processes; app-server hosts never hold one thread's rollout.
        let hosts = processes.map { process in
            Host(pid: process.pid, executable: process.executable,
                 rollouts: Host(pid: process.pid, executable: process.executable, rollouts: []).isDesktopApp ? [] : (lsof(process.pid) ?? []))
        }
        let open = Set(hosts.flatMap(\.rollouts))
        // Tail only rollouts that can become rows: open in a terminal process, or updated in the last 30 min.
        for index in records.indices where open.contains(records[index].rolloutPath)
            || now.timeIntervalSince(records[index].updatedAt) <= recentWindow {
            if let data = readTail(records[index].rolloutPath), let context = turnContext(data) {
                records[index].model = context.model ?? records[index].model
                records[index].effort = context.effort ?? records[index].effort
            }
        }
        return rows(records: records, hosts: hosts, now: now)
    }
}
