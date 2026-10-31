#if DEBUG
import AppKit
import ArgumentParser
import CryptoKit
import Foundation

private final class DemoStore: IslandGateStoring {
    private var values: [String: Data] = [:]
    func read(_ name: String) throws -> Data? { values[name] }
    func write(_ data: Data, name: String) throws { values[name] = data }
    func delete(_ name: String) throws { values.removeValue(forKey: name) }
}

struct IslandGateDemoCommand: ParsableCommand {
    static let configuration = CommandConfiguration(commandName: "gate-demo")
    @Option var baseUrl = "http://127.0.0.1:8790"
    @Option var publicKeyFile = "~/.config/acu-gate/dev-public-key.txt"
    @Option var pollSeconds: Double = 5
    @Option var captureDir: String?
    @Option var timeoutSeconds: Double = 900

    static func requireDev(_ environment: [String: String]) throws {
        guard environment["ACU_GATE_DEV"] == "1" else { throw ValidationError("gate-demo requires ACU_GATE_DEV=1") }
    }
    func validate() throws {
        try Self.requireDev(ProcessInfo.processInfo.environment)
        guard let url = URL(string: baseUrl), ["http", "https"].contains(url.scheme ?? ""), url.host != nil else { throw ValidationError("Invalid base URL") }
        guard pollSeconds.isFinite, pollSeconds > 0, pollSeconds <= 86400, timeoutSeconds.isFinite, timeoutSeconds > 0 else { throw ValidationError("Polling and timeout seconds must be positive finite values") }
    }
    func run() throws {
        let keyText = try String(contentsOfFile: NSString(string: publicKeyFile).expandingTildeInPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let key = Data(base64Encoded: keyText), key.count == 32 else { throw ValidationError("Public key file must contain a base64-encoded 32-byte Ed25519 key") }
        let directory = captureDir.map { URL(fileURLWithPath: NSString(string: $0).expandingTildeInPath, isDirectory: true) }
        if let directory { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        MainActor.assumeIsolated {
            let device = IslandGateDevice(id: SHA256.hash(data: Data("acu-island-gate|demo-\(UUID().uuidString)".utf8)).map { String(format: "%02x", $0) }.joined(), key: P256.Signing.PrivateKey())
            let gate = IslandGate(store: DemoStore(), client: IslandGateClient(baseURL: URL(string: baseUrl)!), publicKey: key, device: device)
            gate.debugPollInterval = pollSeconds
            let controller = IslandGateWindowController(gate: gate)
            let start = ProcessInfo.processInfo.systemUptime
            var last: IslandGateState?
            var sequence = 0
            var finishing = false
            @MainActor func record() {
                let state = gate.state
                guard state != last else { return }
                last = state
                let name: String
                switch state {
                case .locked: name = "locked"
                case .submitting: name = "submitting"
                case .waking: name = "waking"
                case .checking: name = "checking"
                case .unlocked: name = "unlocked"
                case .rejected: name = "rejected"
                case .conflict: name = "conflict"
                case .error: name = "error"
                }
                let elapsed = Int((ProcessInfo.processInfo.systemUptime - start) * 1000)
                FileHandle.standardError.write(Data("\(name)\n".utf8))
                if let directory, let window = controller.window {
                    sequence += 1
                    let filename = String(format: "%02d-%@.png", sequence, name)
                    do {
                        controller.fitContent()
                        try IslandGateWindowCapture.capture(window: window, to: directory.appendingPathComponent(filename))
                        let row: [String: Any] = ["t_ms": elapsed, "state": name, "file": filename]
                        var data = try JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])
                        data.append(0x0a)
                        let timeline = directory.appendingPathComponent("timeline.jsonl")
                        if !FileManager.default.fileExists(atPath: timeline.path) { _ = FileManager.default.createFile(atPath: timeline.path, contents: nil) }
                        let file = try FileHandle(forWritingTo: timeline)
                        defer { try? file.close() }
                        try file.seekToEnd(); try file.write(contentsOf: data)
                    } catch {
                        FileHandle.standardError.write(Data("Capture failed: \(error)\n".utf8))
                        Darwin.exit(1)
                    }
                }
                if case .unlocked = state, !finishing {
                    finishing = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { Darwin.exit(0) }
                }
            }
            gate.onChange.append { MainActor.assumeIsolated { record() } }
            controller.showWindow(nil)
            controller.window?.center()
            controller.window?.makeKeyAndOrderFront(nil)
            app.activate(ignoringOtherApps: true)
            record()
            gate.opened()
            DispatchQueue.main.asyncAfter(deadline: .now() + timeoutSeconds) { if !finishing { Darwin.exit(1) } }
            withExtendedLifetime((gate, controller)) { app.run() }
        }
    }
}
#endif
