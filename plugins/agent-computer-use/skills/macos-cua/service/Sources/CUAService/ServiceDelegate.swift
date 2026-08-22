import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import Logging

@MainActor
final class ServiceDelegate: NSObject, NSApplicationDelegate {
    let socketPath: String
    let logger: Logger
    private var server: SocketServer?
    private var router: MethodRouter?
    private var cursorOverlay: CursorOverlay?
    private var statusBar: StatusBarController?
    private var voiceSupervisor: VoiceSupervisor?
    private var islandController: IslandController?

    init(socketPath: String, logger: Logger) {
        self.socketPath = socketPath
        self.logger = logger
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        promptTCC()
        cursorOverlay = CursorOverlay()
        islandController = IslandController()
        islandController?.prepareNotch()
        voiceSupervisor = VoiceSupervisor()
        statusBar = StatusBarController()
        if let voiceSupervisor, let islandController {
            statusBar?.wire(voice: voiceSupervisor, island: islandController)
        }
        statusBar?.install()

        let appResolver = AppResolver(logger: logger)
        let axTree = AXTreeWalker(logger: logger)
        let screenshotCapture = ScreenshotCapture(logger: logger)
        let settleEngine = SettleEngine(logger: logger)
        let inputActions = InputActions(logger: logger)
        inputActions.axTree = axTree

        router = MethodRouter(
            appResolver: appResolver,
            axTree: axTree,
            screenshotCapture: screenshotCapture,
            settleEngine: settleEngine,
            cursorOverlay: cursorOverlay!,
            inputActions: inputActions,
            logger: logger
        )

        server = SocketServer(socketPath: socketPath, logger: logger) { [router] request in
            guard let router else {
                return JSONRPCResponse(
                    id: request.id,
                    error: .init(code: -32603, message: "Service not ready")
                )
            }
            return await router.handle(request)
        }
        server?.start()
        logger.info("CUAService ready", metadata: ["socket": "\(socketPath)"])
    }

    /// Ad-hoc re-sign drops TCC. Prompt so Accessibility / Screen Recording
    /// reappear instead of silently returning AXUnknown.
    private func promptTCC() {
        if !AXIsProcessTrusted() {
            let prompt = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            AXIsProcessTrustedWithOptions([prompt: true] as CFDictionary)
        }
        if !CGPreflightScreenCaptureAccess() {
            _ = CGRequestScreenCaptureAccess()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        voiceSupervisor?.stop()
        islandController?.stopStreaming()
        server?.stop()
        try? FileManager.default.removeItem(atPath: socketPath)
        logger.info("CUAService stopped")
    }
}
