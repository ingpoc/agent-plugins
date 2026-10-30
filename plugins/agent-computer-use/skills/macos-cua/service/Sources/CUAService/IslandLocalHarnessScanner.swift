import Foundation

enum IslandLocalHarnessScanner {
    struct Harness: Equatable {
        var processName: String
        var displayName: String
    }

    static let harnesses = [
        Harness(processName: "codex", displayName: "Codex"),
        Harness(processName: "Codex", displayName: "Codex"),
        Harness(processName: "Cursor", displayName: "Cursor"),
        Harness(processName: "claude", displayName: "Claude Code"),
        Harness(processName: "Claude", displayName: "Claude"),
        Harness(processName: "opencode", displayName: "OpenCode"),
        Harness(processName: "gemini", displayName: "Gemini CLI"),
        Harness(processName: "Comet", displayName: "Comet"),
        Harness(processName: "Samantha", displayName: "Samantha"),
        Harness(processName: "aider", displayName: "Aider"),
    ]

    static func rows(processList: String) -> [IslandAgent] {
        let names = Dictionary(uniqueKeysWithValues: harnesses.map { ($0.processName, $0.displayName) })
        return processList.split(whereSeparator: { $0 == "\n" }).compactMap { line in
            let fields = line.split(whereSeparator: \.isWhitespace)
            guard fields.count >= 2, let pid = Int(fields[0]), pid > 0 else { return nil }
            let processName = URL(fileURLWithPath: String(fields[1])).lastPathComponent
            guard let displayName = names[processName] else { return nil }
            return IslandAgent(id: "local:\(processName):\(pid)", name: displayName,
                               app: displayName, title: "PID \(pid)", status: "process")
        }
    }

    /// `ps -o pid,comm` gives the executable path (no arguments): tells the ChatGPT app's
    /// app-server hosts apart from terminal `codex` processes.
    static func codexProcesses(processList: String) -> [(pid: Int, executable: String)] {
        processList.split(whereSeparator: { $0 == "\n" }).compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard let space = trimmed.firstIndex(where: \.isWhitespace), let pid = Int(trimmed[..<space]) else { return nil }
            let executable = trimmed[space...].trimmingCharacters(in: .whitespaces)
            guard URL(fileURLWithPath: executable).lastPathComponent == "codex" else { return nil }
            return (pid, executable)
        }
    }

    static func processListing() -> String {
        let pattern = harnesses.map(\.processName).joined(separator: "|")
        guard let pgrep = run("/usr/bin/pgrep", arguments: ["-x", pattern]), pgrep.status == 0 else { return "" }
        let pids = String(decoding: pgrep.data, as: UTF8.self).split(whereSeparator: { $0 == "\n" }).compactMap { Int($0) }
        guard !pids.isEmpty, let ps = run("/bin/ps", arguments: ["-p", pids.map(String.init).joined(separator: ","), "-o", "pid,comm"]) else { return "" }
        return String(decoding: ps.data, as: UTF8.self)
    }

    static func scan() -> [IslandAgent] {
        IslandCodexThreads.scan(processes: codexProcesses(processList: processListing()))
    }

    static func sessions(includeIdle: Bool, includeProbes: Bool, limit: Int) -> [String: Any] {
        let now = Date()
        let listing = processListing()
        let (records, hosts) = IslandCodexThreads.scanData(processes: codexProcesses(processList: listing), now: now)
        let formatter = ISO8601DateFormatter()
        let helpers = Dictionary(grouping: records.filter { $0.parentID != nil }, by: { $0.parentID! })
        var result: [[String: Any]] = records.filter { (includeIdle || $0.rollout.state != "idle") && (includeProbes || !IslandCodexThreads.isProbe($0)) }.map { record in
            let host = hosts.first { $0.rollouts.contains(record.rolloutPath) }
            let appHost = host == nil && ["Codex Desktop", "codex_work_desktop"].contains(record.originator ?? "")
            let model = record.rollout.model ?? record.model
            var item: [String: Any] = ["kind": "codex_thread", "app": "Codex", "thread_id": record.id,
                "title": IslandCodexThreads.displayTitle(record), "state": record.rollout.state,
                "updated_at": formatter.string(from: record.updatedAt), "age_s": max(0, Int(now.timeIntervalSince(record.updatedAt))),
                "host": host != nil ? "terminal" : appHost ? "chatgpt_app" : "unknown",
                "helpers": helpers[record.id]?.count ?? 0, "probe": IslandCodexThreads.isProbe(record),
                "deep_link": "codex://threads/\(record.id)"]
            if let v = host?.pid { item["pid"] = v }
            if let v = model { item["model"] = v; item["model_name"] = IslandCodexThreads.productModelName(v) }
            if let v = record.rollout.effort ?? record.effort { item["effort"] = v.lowercased() }
            if let v = record.cwd { item["cwd"] = v }
            if let v = record.branch { item["branch"] = v }
            if let v = record.codexHome { item["codex_home"] = v }
            if let v = record.parentID { item["parent_thread_id"] = v }
            if let v = record.rollout.tokens { item["tokens"] = v }
            if let v = record.rollout.contextPercent { item["context_pct"] = v }
            if let v = record.originURL, !v.isEmpty { item["repo"] = URL(fileURLWithPath: v).deletingPathExtension().lastPathComponent }
            else if let v = record.cwd { item["repo"] = URL(fileURLWithPath: v).lastPathComponent }
            return item
        }
        result += hosts.filter(\.isDesktopApp).map { ["kind": "host", "app": "Codex", "host": "chatgpt_app", "pid": $0.pid, "probe": false] }
        result += rows(processList: listing).filter { $0.app != "Codex" }.compactMap { row in
            guard let pid = Int(row.title.replacingOccurrences(of: "PID ", with: "")) else { return nil }
            return ["kind": "process", "app": row.app, "pid": pid, "probe": false]
        }
        return ["ok": true, "generated_at": formatter.string(from: now), "sessions": Array(result.prefix(max(1, min(limit, 500))))]
    }

    static func run(_ path: String, arguments: [String]) -> (status: Int32, data: Data)? {
        // Read only process names and PIDs; never inspect process arguments or environment.
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, data)
    }
}
