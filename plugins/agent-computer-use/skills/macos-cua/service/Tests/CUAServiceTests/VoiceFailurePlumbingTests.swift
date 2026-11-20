import AppKit
import XCTest
@testable import CUAService

@MainActor
final class VoiceFailurePlumbingTests: XCTestCase {
    func testMicrophoneDenialFlowsFromSupervisorToControlIslandAndMenu() async {
        let voice = supervisor(microphoneAccess: { false }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        let handler = ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})

        let result = await handler("start")

        XCTAssertEqual(result["ok"] as? Bool, false)
        XCTAssertEqual(result["failure_reason"] as? String, "Microphone access denied")
        XCTAssertEqual(island.failureReason, "Microphone access denied")
        XCTAssertMenuShowsFailure(menu, "Microphone access denied")
    }

    func testMissingHelperFlowsFromSupervisorToControlIslandAndMenu() async {
        let voice = supervisor(microphoneAccess: { true }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        let handler = ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})

        let result = await handler("start")

        XCTAssertEqual(result["ok"] as? Bool, false)
        XCTAssertEqual(result["failure_reason"] as? String, "Voice helper is missing")
        XCTAssertEqual(island.failureReason, "Voice helper is missing")
        XCTAssertMenuShowsFailure(menu, "Voice helper is missing")
    }

    func testUnknownModelErrorSurvivesGatewayHealthAndReachesControlStatusIslandAndMenu() async {
        let voice = supervisor(microphoneAccess: { true }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        let rawReason = "setup_error: Unknown voice model 'invalid-model'"
        let reason = "Voice model unavailable"
        let handler = ServiceDelegate.voiceControlHandler(
            voice: voice,
            island: island,
            ensureSamanthaToken: {},
            startVoice: { voice in
                island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed", detail: rawReason))
                voice.onRunningChanged?(true)
                return true
            }
        )

        let start = await handler("start")
        let status = await handler("status")

        XCTAssertEqual(island.failureReason, reason)
        XCTAssertEqual(island.debugModel.title, reason)
        XCTAssertEqual(start["ok"] as? Bool, false)
        XCTAssertEqual(start["failure_reason"] as? String, reason)
        XCTAssertEqual(status["failure_reason"] as? String, reason)
        XCTAssertMenuShowsFailure(menu, reason, isRunning: true)

        island.hub.ingestSamantha(IslandSnapshot(kind: "listening", title: "Listening"))
        XCTAssertNil(island.failureReason)
        XCTAssertMenuShowsNoFailure(menu)
    }

    func testUnsupportedRetiredPrefixModelFailsStartAndStatusBeforeHelperLaunch() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let settingsPath = directory.appendingPathComponent("settings.json")
        try JSONSerialization.data(withJSONObject: ["live_model": "gpt-realtime-unknown"]).write(to: settingsPath)
        let oldPath = VoiceSettingsStore.path
        let oldKeyCheck = VoiceSettingsStore.xaiKeyAvailable
        VoiceSettingsStore.path = settingsPath
        VoiceSettingsStore.xaiKeyAvailable = { false }
        defer {
            VoiceSettingsStore.path = oldPath
            VoiceSettingsStore.xaiKeyAvailable = oldKeyCheck
        }

        var resolvedHelper = false
        let voice = supervisor(microphoneAccess: { true }, launchResolver: {
            resolvedHelper = true
            return nil
        }, settingsLoader: { VoiceSettingsStore.load() })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        let handler = ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})

        let start = await handler("start")
        let status = await handler("status")
        let reason = "Voice model unavailable: gpt-realtime-unknown"

        XCTAssertFalse(start["ok"] as? Bool ?? true)
        XCTAssertEqual(start["failure_reason"] as? String, reason)
        XCTAssertEqual(status["failure_reason"] as? String, reason)
        XCTAssertEqual(island.failureReason, reason)
        XCTAssertFalse(resolvedHelper)
        XCTAssertMenuShowsFailure(menu, reason)
    }

    func testSupportedSettingsModelPassesModelPreflight() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let settingsPath = directory.appendingPathComponent("settings.json")
        try JSONSerialization.data(withJSONObject: ["live_model": VoiceSettingsStore.liveModel]).write(to: settingsPath)
        let oldPath = VoiceSettingsStore.path
        let oldKeyCheck = VoiceSettingsStore.xaiKeyAvailable
        VoiceSettingsStore.path = settingsPath
        VoiceSettingsStore.xaiKeyAvailable = { false }
        defer {
            VoiceSettingsStore.path = oldPath
            VoiceSettingsStore.xaiKeyAvailable = oldKeyCheck
        }

        let voice = supervisor(microphoneAccess: { false }, launchResolver: { nil }, settingsLoader: { VoiceSettingsStore.load() })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let result = await ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})("start")

        XCTAssertEqual(result["failure_reason"] as? String, "Microphone access denied")
        XCTAssertNotEqual(island.failureReason, "Voice model unavailable")
    }

    func testRetryClearsOldErrorAndShowsTheNewSupervisorFailure() async {
        let voice = supervisor(microphoneAccess: { false }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed", detail: "Old setup error"))

        _ = await ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})("start")

        XCTAssertEqual(island.failureReason, "Microphone access denied")
        XCTAssertMenuShowsFailure(menu, "Microphone access denied")
    }

    // ACU-18: a failed-start card clears when Samantha is turned off, the model changes, or Retry starts again.
    func testStopClearsAFailedStartCard() async {
        let voice = supervisor(microphoneAccess: { false }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        let handler = ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})
        _ = await handler("start")
        XCTAssertEqual(island.failureReason, "Microphone access denied")

        let stopped = await handler("stop")

        XCTAssertNil(island.failureReason)
        XCTAssertTrue(stopped["failure_reason"] is NSNull)
        let status = await handler("status")
        XCTAssertTrue(status["failure_reason"] is NSNull)
        XCTAssertMenuShowsNoFailure(menu)
    }

    func testVoiceModelChangeClearsAFailedStartCard() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let oldPath = VoiceSettingsStore.path
        VoiceSettingsStore.path = directory.appendingPathComponent("settings.json")
        defer { VoiceSettingsStore.path = oldPath }
        let (island, _) = controllers()
        island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed",
                                                 detail: "setup_error: Voice model unavailable: acu01-unknown-model"))
        XCTAssertEqual(island.failureReason, "Voice model unavailable: acu01-unknown-model")

        VoiceSettingsStore.set(VoiceSettingsStore.grokModel, for: "voice.live_model")

        XCTAssertNil(island.failureReason, "cleared before set returns, so a start queued after the change keeps its own failure")
        XCTAssertNotEqual(island.hub.samantha?.kind, "error")
    }

    func testVoiceConfigChangeOtherThanModelClearsAModelFailure() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let oldPath = VoiceSettingsStore.path
        VoiceSettingsStore.path = directory.appendingPathComponent("settings.json")
        defer { VoiceSettingsStore.path = oldPath }
        let (island, _) = controllers()
        island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed",
                                                 detail: "setup_error: Voice model unavailable: acu01-unknown-model"))

        VoiceSettingsStore.set("ara", for: "voice.live_voice")

        XCTAssertNil(island.failureReason)
    }

    func testMenuSwitchOffClearsAFailedStartWhileTheHelperIsUp() async {
        let voice = supervisor(microphoneAccess: { true }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        let menu = menuBar.makeMenu()
        let handler = ServiceDelegate.voiceControlHandler(
            voice: voice, island: island, ensureSamanthaToken: {},
            startVoice: { voice in
                island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed",
                                                         detail: "setup_error: Voice model unavailable: acu01-unknown-model"))
                voice.onRunningChanged?(true)
                return true
            }
        )
        _ = await handler("start")
        XCTAssertNotNil(island.failureReason)

        menuBar.turnSamanthaOff()

        XCTAssertNil(island.failureReason)
        XCTAssertNil(voice.startFailure)
        XCTAssertNotEqual(island.hub.samantha?.kind, "error")
        XCTAssertMenuShowsNoFailure(menu)
    }

    func testRetryOnAFailedStartCardStartsSamanthaAgain() async {
        let (island, _) = controllers()
        var retried = 0
        island.onRetryVoiceStart = { retried += 1; return true }
        island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed",
                                                 detail: "setup_error: Voice model unavailable: acu01-unknown-model"))

        await island.debugHandle(.retry)

        XCTAssertEqual(retried, 1, "Retry on a failed start runs a fresh start instead of posting to a stopped gateway")
        XCTAssertNil(island.failureReason, "the old reason is gone as soon as Retry starts again")
    }

    func testVoiceModelChangeKeepsAMicrophoneDenial() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let oldPath = VoiceSettingsStore.path
        VoiceSettingsStore.path = directory.appendingPathComponent("settings.json")
        defer { VoiceSettingsStore.path = oldPath }
        let voice = supervisor(microphoneAccess: { false }, launchResolver: { nil })
        let (island, menuBar) = controllers()
        menuBar.wire(voice: voice, island: island)
        _ = await ServiceDelegate.voiceControlHandler(voice: voice, island: island, ensureSamanthaToken: {})("start")

        VoiceSettingsStore.set(VoiceSettingsStore.grokModel, for: "voice.live_model")
        try await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertEqual(island.failureReason, "Microphone access denied")
    }

    func testDismissClearsAFailedStartCard() async {
        let (island, _) = controllers()
        island.hub.ingestSamantha(IslandSnapshot(kind: "error", title: "Setup failed", detail: "Old setup error"))
        XCTAssertNotNil(island.failureReason)

        await island.debugHandle(.dismiss)

        XCTAssertNil(island.failureReason)
    }

    private func supervisor(
        microphoneAccess: @escaping () async -> Bool,
        launchResolver: @escaping () -> VoiceSupervisor.LaunchSpec?,
        settingsLoader: (() -> VoiceSettingsStore.Settings)? = nil
    ) -> VoiceSupervisor {
        VoiceSupervisor(
            gatewayPort: 1,
            logURL: FileManager.default.temporaryDirectory.appendingPathComponent("voice-failure-\(UUID().uuidString).log"),
            microphoneAccess: microphoneAccess,
            launchResolver: launchResolver,
            settingsLoader: settingsLoader ?? { .default }
        )
    }

    private func controllers() -> (IslandController, StatusBarController) {
        let defaults = TestDefaults.make("VoiceFailurePlumbingTests-\(UUID().uuidString)")!
        let store = IslandSettingsStore(defaults: defaults)
        let island = IslandController(gatewayPort: 1, store: store, hubPort: nil, startsSystem: false)
        let menu = StatusBarController(gate: IslandGate(fixtureState: .locked()), bar: NSStatusBar())
        return (island, menu)
    }

    private func XCTAssertMenuShowsFailure(
        _ menu: NSMenu,
        _ reason: String,
        isRunning: Bool = false,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let row = menu.items.compactMap { $0.view as? MenuSwitchRowView }.first
        XCTAssertEqual(row?.label.stringValue, "Samantha — Failed", file: file, line: line)
        XCTAssertEqual(row?.toggle.state, isRunning ? .on : .off, file: file, line: line)
        XCTAssertTrue(menu.items.contains { !$0.isHidden && $0.title == "Samantha failed — \(reason)" }, file: file, line: line)
    }

    private func XCTAssertMenuShowsNoFailure(
        _ menu: NSMenu,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let row = menu.items.compactMap { $0.view as? MenuSwitchRowView }.first
        XCTAssertEqual(row?.label.stringValue, "Samantha", file: file, line: line)
        XCTAssertFalse(menu.items.contains { !$0.isHidden && $0.title.hasPrefix("Samantha failed —") }, file: file, line: line)
    }
}
