import AppKit
import ArgumentParser
import Darwin
import Foundation
import Logging

struct CUAServiceCLI: ParsableCommand {
    static let configuration = CommandConfiguration(
        commandName: "CUAService",
        abstract: "Unified macOS Computer Use service — JSON-RPC over Unix socket"
    )

    @Option(name: .long, help: "Unix domain socket path")
    var socketPath: String = NSString(
        string: "~/.cache/macos-cua/cua-service.sock"
    ).expandingTildeInPath

    @Option(name: .long, help: "Log level: trace, debug, info, warning, error")
    var logLevel: String = "info"

    func run() throws {
        LoggingSystem.bootstrap { label in
            var handler = StreamLogHandler.standardError(label: label)
            handler.logLevel = Logger.Level(rawValue: logLevel) ?? .info
            return handler
        }
        let logger = Logger(label: "cua-service")
        logger.info("Starting CUAService", metadata: ["socket": "\(socketPath)"])

        let lockPath = socketPath + ".lock"
        try FileManager.default.createDirectory(
            at: URL(fileURLWithPath: lockPath).deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let lockFD = Darwin.open(lockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard lockFD >= 0 else {
            throw ValidationError("Cannot open CUAService instance lock: \(lockPath)")
        }
        if flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
            let lockError = errno
            Darwin.close(lockFD)
            if lockError == EWOULDBLOCK {
                logger.info("CUAService instance already running; exiting")
                return
            }
            throw ValidationError(
                "Cannot lock CUAService instance: \(lockPath) (errno \(lockError))"
            )
        }
        defer {
            _ = flock(lockFD, LOCK_UN)
            Darwin.close(lockFD)
        }

        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let socketPath = self.socketPath
        MainActor.assumeIsolated {
            let delegate = ServiceDelegate(socketPath: socketPath, logger: logger)
            app.delegate = delegate
            _ = delegate // prevent dealloc
        }
        app.run()
    }
}

CUAServiceCLI.main()
