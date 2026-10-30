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
    }

    static func turnContext(_ tail: Data) -> (model: String?, effort: String?)? {
        var latest: (String?, String?)?
        for line in tail.split(separator: 10) {
            guard let object = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any],
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

    static func rows(records: [ThreadRecord], pidPaths: [Int: [String]], now: Date) -> [IslandAgent] {
        let byPath = Dictionary(grouping: records, by: \.rolloutPath)
        var result: [IslandAgent] = []
        var mapped = Set<String>()
        var hasUnmappedPID = false
        for pid in pidPaths.keys.sorted() {
            let paths = Array(Set(pidPaths[pid] ?? []))
            if paths.count == 1, let matches = byPath[paths[0]], matches.count == 1, let record = matches.first {
                mapped.insert(record.rolloutPath)
                result.append(threadRow(record, id: "local:codex:\(pid)", now: now))
            } else {
                hasUnmappedPID = true
                var row = IslandAgent(id: "local:codex:\(pid)", name: paths.isEmpty ? "Codex app-server" : "Codex", app: "Codex",
                                      title: "PID \(pid)", status: "process")
                row.local_detail = paths.isEmpty ? "no open thread" : "open thread unknown"
                result.append(row)
            }
        }
        if hasUnmappedPID {
            for record in records where !mapped.contains(record.rolloutPath) && now.timeIntervalSince(record.updatedAt) <= 1800 {
                result.append(threadRow(record, id: "local:codex-thread:\(record.rolloutPath)", now: now))
            }
        }
        return result
    }

    private static func threadRow(_ record: ThreadRecord, id: String, now: Date) -> IslandAgent {
        let firstLine = record.firstUserMessage?.components(separatedBy: .newlines).first
        let title = [record.title, firstLine].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty } ?? "Untitled thread"
        let shortTitle = title.count > 48 ? String(title.prefix(47)) + "…" : title
        let seconds = max(0, Int(now.timeIntervalSince(record.updatedAt)))
        let age = seconds < 60 ? "\(seconds)s" : seconds < 3600 ? "\(seconds / 60)m" : "\(seconds / 3600)h"
        var row = IslandAgent(id: id, name: "Codex", app: "Codex", title: shortTitle, status: "process")
        row.local_detail = "\(record.model ?? "model unknown") · \(record.effort ?? "effort unknown") · \(seconds < 90 ? "working" : "idle") · \(age) ago"
        return row
    }

    static func openReadOnly(_ path: String) -> OpaquePointer? {
        var db: OpaquePointer?
        let uri = URL(fileURLWithPath: path).absoluteString + "?mode=ro"
        guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        return db
    }

    static func readDatabase(_ path: String) -> [ThreadRecord] {
        guard let db = openReadOnly(path) else { return [] }
        defer { sqlite3_close(db) }
        let sql = "SELECT id, rollout_path, title, first_user_message, model, reasoning_effort, updated_at_ms, archived, source, cwd FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC LIMIT 20"
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        func string(_ index: Int32) -> String? {
            guard let value = sqlite3_column_text(stmt, index) else { return nil }
            return String(cString: value)
        }
        var records: [ThreadRecord] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let id = string(0), let path = string(1), sqlite3_column_type(stmt, 6) != SQLITE_NULL else { continue }
            records.append(ThreadRecord(id: id, rolloutPath: path, title: string(2), firstUserMessage: string(3),
                                        model: string(4), effort: string(5),
                                        updatedAt: Date(timeIntervalSince1970: Double(sqlite3_column_int64(stmt, 6)) / 1000)))
        }
        return records
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

    static func scan(pids: [Int], home: URL = FileManager.default.homeDirectoryForCurrentUser,
                     readDB: (String) -> [ThreadRecord] = readDatabase,
                     readTail: (String) -> Data? = tail,
                     lsof: (Int) -> [String]? = lsofPaths, now: Date = Date()) -> [IslandAgent] {
        guard !pids.isEmpty else { return [] }
        let directories = (try? FileManager.default.contentsOfDirectory(at: home, includingPropertiesForKeys: nil)) ?? []
        let homes = directories.filter { $0.lastPathComponent == ".codex" || $0.lastPathComponent.hasPrefix(".codex-") }
        var records = homes.flatMap { readDB($0.appendingPathComponent("state_5.sqlite").path) }
            .sorted { $0.updatedAt > $1.updatedAt }
        records = Array(records.prefix(20))
        for index in records.indices {
            if let data = readTail(records[index].rolloutPath), let context = turnContext(data) {
                records[index].model = context.model
                records[index].effort = context.effort
            }
        }
        let paths = Dictionary(uniqueKeysWithValues: pids.map { ($0, lsof($0) ?? [""]) })
        return rows(records: records, pidPaths: paths, now: now)
    }
}
