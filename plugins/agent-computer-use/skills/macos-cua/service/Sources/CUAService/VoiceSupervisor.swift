import AppKit
import AVFoundation
import Darwin
import Foundation

/// Supervises the bundled voice stack child. Stopping voice does not touch cua-service.sock.
@MainActor
final class VoiceSupervisor {
    private var process: Process?
    private var healthTimer: Timer?
    private var restartTask: Task<Void, Never>?
    private var recentRestarts: [Date] = []
    private var stopping = false
    private(set) var running = false
    var onRunningChanged: ((Bool) -> Void)?

    private let gatewayPort: Int
    private let logURL: URL
    private var startupSessionId = ""
    private var startupStartedAt: Date?
    private var controlToken = ""

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

    func start(automatic: Bool = false) async -> Bool {
        guard !running else { return true }
        if !automatic {
            recentRestarts.removeAll()
        }
        stopping = false
        startupSessionId = UUID().uuidString.prefix(8).lowercased()
        startupStartedAt = Date()
        logStartup(phase: "supervisor_begin", detail: "port=\(gatewayPort)")
        guard await ensureMicrophoneAccess() else {
            logStartup(phase: "microphone_permission", status: "fail", detail: "denied")
            appendLog("voice start failed: microphone permission denied\n")
            return false
        }
        logStartup(phase: "microphone_permission", status: "ok")
        clearStaleVoiceStack(reason: "pre-start", signal: SIGTERM)
        if Self.isPortListening(gatewayPort) {
            logStartup(phase: "stale_gateway_shutdown")
            requestGatewayShutdown()
            clearStaleVoiceStack(reason: "pre-start-kill", signal: SIGKILL)
        }
        guard let launch = resolveLaunch() else {
            logStartup(phase: "resolve_launch", status: "fail", detail: "no voice helper")
            appendLog("voice start failed: bundled voice helper missing\n")
            return false
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
        controlToken = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        env["VOICE_CUA_CONTROL_TOKEN"] = controlToken
        env.removeValue(forKey: "VOICE_CUA_REMOTE_ISLAND")
        for (key, value) in VoiceSettingsStore.applyToEnvironment(VoiceSettingsStore.load()) {
            env[key] = value
        }
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

        proc.terminationHandler = { [weak self] terminated in
            Task { @MainActor in
                self?.handleTermination(terminated)
            }
        }

        appendLog("voice start → \(launch.executable.path)\n")
        logStartup(phase: "process_launch", detail: launch.executable.lastPathComponent)
        do {
            try proc.run()
            process = proc
            logStartup(phase: "process_started")
            guard await waitForGatewayHealth(timeout: 45) else {
                logStartup(phase: "gateway_health", status: "fail", detail: "timeout 45s")
                appendLog("voice start failed: gateway health timeout\n")
                proc.terminate()
                process = nil
                clearStaleVoiceStack(reason: "health-timeout", signal: SIGTERM)
                setRunning(false)
                return false
            }
            logStartup(phase: "gateway_health", status: "ok")
            setRunning(true)
            startHealthTimer()
            logStartup(phase: "supervisor_ready", status: "ok")
            return true
        } catch {
            logStartup(phase: "process_launch", status: "fail", detail: error.localizedDescription)
            appendLog("voice start error: \(error.localizedDescription)\n")
            setRunning(false)
            return false
        }
    }

    private func ensureMicrophoneAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            return await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    continuation.resume(returning: granted)
                }
            }
        default:
            return false
        }
    }

    func stop() {
        stopping = true
        restartTask?.cancel()
        restartTask = nil
        healthTimer?.invalidate()
        healthTimer = nil
        logStartup(phase: "supervisor_stop")
        appendLog("voice stop requested\n")
        requestGatewayShutdown()
        if let proc = process, proc.isRunning {
            proc.terminate()
        }
        process = nil
        clearStaleVoiceStack(reason: "stop", signal: SIGTERM)
        if Self.isPortListening(gatewayPort) {
            Thread.sleep(forTimeInterval: 0.6)
            clearStaleVoiceStack(reason: "stop-kill", signal: SIGKILL)
        }
        setRunning(false)
    }

    func isHealthy() -> Bool {
        guard running, let proc = process, proc.isRunning else { return false }
        return checkGatewayHealth(timeout: 1.5)
    }

    private func waitForGatewayHealth(timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let proc = process, !proc.isRunning { return false }
            if checkGatewayHealth(timeout: 1.0) { return true }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        return false
    }

    private func checkGatewayHealth(timeout: TimeInterval) -> Bool {
        guard let url = URL(string: "/health", relativeTo: gatewayURL) else { return false }
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.5)
        return ok
    }

    // MARK: - Stale stack cleanup (PyInstaller parent/child survives orphan)

    private func requestGatewayShutdown() {
        guard let url = URL(string: "/api/shutdown", relativeTo: gatewayURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 2
        if !controlToken.isEmpty {
            req.setValue(controlToken, forHTTPHeaderField: "X-Voice-CUA-Control")
        }
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, error in
            defer { sem.signal() }
            guard error == nil, let http = resp as? HTTPURLResponse, http.statusCode == 200 else { return }
            Task { @MainActor [weak self] in
                self?.appendLog("voice shutdown acknowledged via gateway\n")
            }
        }.resume()
        _ = sem.wait(timeout: .now() + 2.5)
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            if !Self.isPortListening(gatewayPort) { return }
            Thread.sleep(forTimeInterval: 0.1)
        }
    }

    /// Terminate any voice-cua listener on our gateway port and bundled helper PIDs.
    private func clearStaleVoiceStack(reason: String, signal: Int32) {
        let targets = Set(Self.voiceStackPIDs(on: gatewayPort))
        if targets.isEmpty {
            if reason.hasPrefix("stop") && Self.isPortListening(gatewayPort) {
                appendLog("voice \(reason): gateway still listening; pgrep/lsof returned no pids\n")
            }
            return
        }
        appendLog(
            "voice \(reason): signaling \(targets.count) pid(s) on :\(gatewayPort)\n"
        )
        Self.signal(targets, sig: signal)
    }

    private static func isPortListening(_ port: Int) -> Bool {
        !pidsListening(on: port).isEmpty
    }

    private static func voiceStackPIDs(on port: Int) -> [pid_t] {
        let script = """
        /usr/sbin/lsof -tiTCP:\(port) -sTCP:LISTEN 2>/dev/null || true
        /usr/bin/pgrep -f voice-cua.app/Contents/MacOS/voice-cua 2>/dev/null || true
        /usr/bin/pgrep -f voice_cua.voice_stack 2>/dev/null || true
        """
        return runLines(executable: "/bin/bash", arguments: ["-c", script])
            .compactMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
            .filter { $0 > 1 }
    }

    private static func pidsListening(on port: Int) -> [pid_t] {
        runLines(
            executable: "/usr/sbin/lsof",
            arguments: ["-tiTCP:\(port)", "-sTCP:LISTEN"]
        ).compactMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
    }

    private static func pidsForVoiceHelper() -> [pid_t] {
        runLines(
            executable: "/usr/bin/pgrep",
            arguments: ["-f", "voice-cua.app/Contents/MacOS/voice-cua"]
        ).compactMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
    }

    private static func pidsForDevVoiceStack() -> [pid_t] {
        let patterns = ["voice_cua.voice_stack", "python3 -m voice_cua.gateway"]
        var out: [pid_t] = []
        for pattern in patterns {
            out.append(contentsOf: runLines(
                executable: "/usr/bin/pgrep",
                arguments: ["-f", pattern]
            ).compactMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) })
        }
        return out
    }

    private static func runLines(executable: String, arguments: [String]) -> [String] {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: executable)
        task.arguments = arguments
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do {
            try task.run()
            task.waitUntilExit()
        } catch {
            return []
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8), !text.isEmpty else { return [] }
        return text.split(separator: "\n").map(String.init)
    }

    private static func signal(_ pids: some Sequence<pid_t>, sig: Int32) {
        for pid in Set(pids) where pid > 1 {
            kill(pid, sig)
        }
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

        return nil
    }

    private func handleTermination(_ terminated: Process) {
        if let current = process, current !== terminated {
            return
        }
        healthTimer?.invalidate()
        healthTimer = nil
        process = nil
        let unexpected = !stopping
        logStartup(
            phase: "process_exit",
            status: unexpected ? "fail" : "ok",
            detail: unexpected ? "voice process exited" : "voice process stopped"
        )
        setRunning(false)
        appendLog(unexpected ? "voice process exited\n" : "voice process stopped\n")
        if unexpected {
            scheduleRestart()
        }
    }

    private func scheduleRestart() {
        let now = Date()
        recentRestarts = recentRestarts.filter { now.timeIntervalSince($0) < 60 }
        guard recentRestarts.count < 3 else {
            logStartup(
                phase: "restart_suppressed",
                status: "fail",
                detail: "3 unexpected exits in 60s"
            )
            appendLog("voice restart suppressed after 3 unexpected exits in 60s\n")
            return
        }
        recentRestarts.append(now)
        let delay = min(pow(2.0, Double(recentRestarts.count - 1)), 8.0)
        logStartup(phase: "restart_scheduled", detail: "delay=\(Int(delay))s")
        restartTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, !Task.isCancelled, !self.stopping else { return }
            _ = await self.start(automatic: true)
        }
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
                    self.handleTermination(proc)
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

    private func logStartup(phase: String, status: String = "ok", detail: String = "") {
        let elapsed: Int?
        if let start = startupStartedAt {
            elapsed = Int(Date().timeIntervalSince(start) * 1000)
        } else {
            elapsed = nil
        }
        SamanthaActivityLog.startup(
            phase: phase,
            status: status,
            sessionId: startupSessionId,
            detail: detail,
            elapsedMs: elapsed,
            voiceLogURL: logURL
        )
    }
}
