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

    static func scan() -> [IslandAgent] {
        let pattern = harnesses.map(\.processName).joined(separator: "|")
        guard let pgrep = run("/usr/bin/pgrep", arguments: ["-x", pattern]) else { return [] }
        let pids = String(decoding: pgrep.data, as: UTF8.self).split(whereSeparator: { $0 == "\n" })
            .compactMap { Int($0) }
        guard pgrep.status == 0, !pids.isEmpty,
              let ps = run("/bin/ps", arguments: ["-p", pids.map(String.init).joined(separator: ","), "-o", "pid,comm"]) else {
            return []
        }
        let processes = rows(processList: String(decoding: ps.data, as: UTF8.self))
        let codex = processes.filter { $0.id.hasPrefix("local:codex:") }
        let codexPIDs = codex.compactMap { Int($0.id.split(separator: ":").last ?? "") }
        var others = processes.filter { !$0.id.hasPrefix("local:codex:") }
        for index in others.indices where others[index].app == "Cursor" {
            others[index].local_detail = "model unknown · activity unknown"
        }
        return others + IslandCodexThreads.scan(pids: codexPIDs)

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
