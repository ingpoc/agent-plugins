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
    private let islandGate = IslandGate.shared
    private var signalSources: [DispatchSourceSignal] = []

    init(socketPath: String, logger: Logger) {
        self.socketPath = socketPath
        self.logger = logger
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        islandGate.start()
        cursorOverlay = CursorOverlay()
        islandController = IslandController(socketPath: IslandHubSocketServer.defaultPath(), startsNowPlaying: true, gate: islandGate)
        islandController?.start()  // island + hub live from launch, Samantha or not
        voiceSupervisor = VoiceSupervisor()
        statusBar = StatusBarController()
        if let voiceSupervisor, let islandController {
            islandController.controlToken = { [weak voiceSupervisor] in voiceSupervisor?.currentControlToken ?? "" }
            islandController.onStopListening = { [weak voiceSupervisor] in voiceSupervisor?.stop() }
            statusBar?.wire(voice: voiceSupervisor, island: islandController)
        }
        statusBar?.install()
        // Consents: no launch-time system prompts. Onboarding lists what is missing (first launch or a
        // revoked grant); the island warns; the agent API fails fast with PERMISSION_MISSING:<name>.
        var lastMissing: Set<ACUPermission>?   // nil until the launch check (showIfNeeded owns that one)
        PermissionCenter.shared.onChange.append { [weak self] _ in
            let missing = PermissionCenter.missingRequired()
            self?.islandController?.setPermissionWarning(missing.first)
            GrantResume.shared.update(PermissionCenter.shared.states)
            // A required consent newly went missing (revoked / signature change): re-present onboarding once.
            if let prev = lastMissing, OnboardingModel.shouldReopen(previous: prev, now: Set(missing)) {
                OnboardingWindowController.shared.model.step = .permissions
                OnboardingWindowController.shared.show()
            }
            lastMissing = Set(missing)
        }
        OnboardingWindowController.showIfNeeded()
        GrantResume.shared.restoreAtLaunch()   // relaunched mid-grant → same pane / onboarding rows
        PermissionCenter.shared.startPolling()
        installTerminationSignals()
        if SamanthaResume.consume(), let voice = voiceSupervisor {
            // Samantha was on when the previous service instance was stopped.
            SamanthaActivityLog.startup(phase: "samantha_resume", status: "ok", detail: "after service restart")
            Task { @MainActor in _ = await voice.start() }
        }

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
        islandController?.setCursorOverlay(cursorOverlay!)

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

    /// AppKit exits on SIGTERM without `applicationWillTerminate`, which used to
    /// orphan the voice helper (old code, mic open, no island). Route SIGTERM /
    /// SIGINT / SIGHUP through a normal terminate so the helper is stopped, and
    /// leave a resume marker when Samantha was on.
    private func installTerminationSignals() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { [weak self] in
                MainActor.assumeIsolated { self?.terminateFromSignal(sig) }
            }
            src.resume()
            signalSources.append(src)
        }
    }

    private func terminateFromSignal(_ sig: Int32) {
        if voiceSupervisor?.running == true {
            SamanthaResume.write()
        }
        logger.info("CUAService signal \(sig) → terminate")
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        GrantResume.shared.terminating = true
        voiceSupervisor?.stop()
        islandController?.stopStreaming()
        islandController?.shutdown()
        cursorOverlay?.hide()
        server?.stop()
        try? FileManager.default.removeItem(atPath: socketPath)
        logger.info("CUAService stopped")
    }
}
