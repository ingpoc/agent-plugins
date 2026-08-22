import AppKit
import Foundation

/// Supervises the bundled or dev voice stack child. Stopping voice does not touch cua-service.sock.
@MainActor
final class VoiceSupervisor {
    private var process: Process?
    private var healthTimer: Timer?
    private(set) var running = false
    var onRunningChanged: ((Bool) -> Void)?

    private let gatewayPort: Int
    private let logURL: URL

    init(gatewayPort: Int = 8765) {
        self.gatewayPort = gatewayPort
        let cache = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache/macos-cua", isDirectory: true)
        try? FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
        logURL = cache.appendingPathComponent("voice.log")
    }

    var gatewayURL: URL {
        URL(string: "http://127.0.0.1:\(gatewayPort)")!
    }

    func start() {
        guard !running else { return }
        guard let launch = resolveLaunch() else {
            appendLog("voice start failed: no bundled helper or dev voice-cua-agent tree\n")
            return
        }

        let proc = Process()
        proc.executableURL = launch.executable
        proc.arguments = launch.arguments
        var env = ProcessInfo.processInfo.environment
        for (key, value) in launch.extraEnv {
            env[key] = value
        }
        env["VOICE_CUA_PORT"] = String(gatewayPort)
        env["VOICE_CUA_GATEWAY"] = gatewayURL.absoluteString
        env.removeValue(forKey: "VOICE_CUA_REMOTE_ISLAND")
        proc.environment = env

        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            proc.standardOutput = handle
            proc.standardError = handle
        } else {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            if let handle = try? FileHandle(forWritingTo: logURL) {
                proc.standardOutput = handle
                proc.standardError = handle
            }
        }

        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.handleTermination()
            }
        }

        appendLog("voice start → \(launch.executable.path)\n")
        do {
            try proc.run()
            process = proc
            setRunning(true)
            startHealthTimer()
        } catch {
            appendLog("voice start error: \(error.localizedDescription)\n")
            setRunning(false)
        }
    }

    func stop() {
        healthTimer?.invalidate()
        healthTimer = nil
        guard let proc = process, proc.isRunning else {
            process = nil
            setRunning(false)
            return
        }
        appendLog("voice stop requested\n")
        proc.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) { [weak proc] in
            if let proc, proc.isRunning {
                proc.interrupt()
            }
        }
    }

    func isHealthy() -> Bool {
        guard running, let proc = process, proc.isRunning else { return false }
        guard let url = URL(string: "/health", relativeTo: gatewayURL) else { return false }
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: url) { _, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return ok
    }

    private struct LaunchSpec {
        let executable: URL
        let arguments: [String]
        let extraEnv: [String: String]
    }

    private func resolveLaunch() -> LaunchSpec? {
        let bundleRoot = Bundle.main.bundleURL
        let helper = bundleRoot
            .appendingPathComponent("Contents/Helpers/voice-cua.app/Contents/MacOS/voice-cua")
        if FileManager.default.isExecutableFile(atPath: helper.path) {
            return LaunchSpec(executable: helper, arguments: [], extraEnv: [:])
        }

        let flatHelper = bundleRoot.appendingPathComponent("Contents/MacOS/voice-cua")
        if FileManager.default.isExecutableFile(atPath: flatHelper.path) {
            return LaunchSpec(executable: flatHelper, arguments: [], extraEnv: [:])
        }

        for root in Self.devVoiceRoots() {
            let module = root.appendingPathComponent("python/voice_cua/voice_stack.py")
            guard FileManager.default.fileExists(atPath: module.path) else { continue }
            let python = URL(fileURLWithPath: "/usr/bin/python3")
            return LaunchSpec(
                executable: python,
                arguments: ["-m", "voice_cua.voice_stack"],
                extraEnv: ["PYTHONPATH": root.appendingPathComponent("python").path]
            )
        }
        return nil
    }

    private static func devVoiceRoots() -> [URL] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return [
            home.appendingPathComponent(
                "Documents/remote-claude/active/apps/voice-cua-agent",
                isDirectory: true
            ),
            URL(fileURLWithPath: NSString(string: "~/voice-cua-agent").expandingTildeInPath),
        ]
    }

    private func handleTermination() {
        healthTimer?.invalidate()
        healthTimer = nil
        process = nil
        setRunning(false)
        appendLog("voice process exited\n")
    }

    private func setRunning(_ on: Bool) {
        guard running != on else { return }
        running = on
        onRunningChanged?(on)
    }

    private func startHealthTimer() {
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.running else { return }
                if let proc = self.process, !proc.isRunning {
                    self.handleTermination()
                    return
                }
                if !self.isHealthy() {
                    self.appendLog("voice health check failed (pid alive but /health down)\n")
                }
            }
        }
    }

    private func appendLog(_ line: String) {
        guard let data = line.data(using: .utf8) else { return }
        if FileManager.default.fileExists(atPath: logURL.path),
           let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: logURL)
        }
    }
}
