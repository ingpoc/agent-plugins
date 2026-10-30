import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Settings: schema, persistence, gating, tokens (MemoryTokenStore only — never the real Keychain).
@MainActor
final class IslandSettingsTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!

    override func setUp() {
        suiteName = "cua.island.settings.test.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        IslandModel.peekDone = 4
        IslandModel.peekError = 6
        IslandModel.doneSettle = 4
    }

    private func store() -> IslandSettingsStore {
        IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
    }

    /// Voice settings moved from the old Samantha Settings window into Settings → Samantha:
    /// same settings.json keys, so stored values survive and unknown keys are preserved.
    func testVoiceRowsReadAndWriteTheSharedSettingsFile() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("voice-settings-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let saved = VoiceSettingsStore.path
        defer { VoiceSettingsStore.path = saved }
        VoiceSettingsStore.path = dir.appendingPathComponent("settings.json")
        try JSONSerialization.data(withJSONObject: ["realtime_model": "gpt-realtime-2.1-mini", "realtime_voice": "nope", "extra": "kept"])
            .write(to: VoiceSettingsStore.path)
        let defaults = UserDefaults(suiteName: "voice-\(UUID().uuidString)")!
        let s = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore(), voiceInFile: true)
        XCTAssertEqual(s.string(IslandSettingsSchema.voiceModel), "gpt-live-1", "retired Realtime id maps to GPT-Live-1")
        s.set("ash", for: IslandSettingsSchema.voiceVoice)
        let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: VoiceSettingsStore.path)) as? [String: Any]
        XCTAssertEqual(obj?["realtime_voice"] as? String, "ash")
        XCTAssertEqual(obj?["realtime_model"] as? String, "gpt-live-1")
        XCTAssertEqual(obj?["extra"] as? String, "kept")
        XCTAssertNil(defaults.object(forKey: IslandSettingsSchema.voiceVoice), "voice rows never land in UserDefaults")
        // Every stored value is one of the popup's options (no orphaned selection after the move).
        for key in IslandSettingsSchema.voiceKeys {
            guard case .popup(let o)? = IslandSettingsSchema.def(key)?.kind else { return XCTFail("\(key) is a popup") }
            XCTAssertTrue(o.contains { $0.value == s.string(key) }, key)
        }
    }

    /// Model picker: GPT-Live-1 (default), Grok Voice 3.0 (same ids as voice_settings.MODELS). Realtime removed.
    /// The Bulk / Intelligence pickers show only while GPT-Live-1 is selected.
    func testModelPickerAndLiveBackendRows() {
        XCTAssertEqual(VoiceSettingsStore.models.map(\.value),
                       ["gpt-live-1", "grok-voice-latest"])
        XCTAssertEqual(VoiceSettingsStore.Settings.default.realtimeModel, "gpt-live-1")
        XCTAssertEqual(VoiceSettingsStore.normalizedModel("gpt-realtime-2.1"), "gpt-live-1")
        XCTAssertEqual(VoiceSettingsStore.backendModels.map(\.value), ["gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"])
        let saved = VoiceSettingsStore.xaiKeyAvailable
        defer { VoiceSettingsStore.xaiKeyAvailable = saved }
        VoiceSettingsStore.xaiKeyAvailable = { false }
        XCTAssertEqual(VoiceSettingsStore.models.first { $0.value == "grok-voice-latest" }?.label, "Grok Voice 3.0 (needs API key)")
        VoiceSettingsStore.xaiKeyAvailable = { true }
        XCTAssertEqual(VoiceSettingsStore.models.first { $0.value == "grok-voice-latest" }?.label, "Grok Voice 3.0")

        let s = store()
        func liveRows() -> [String] {
            IslandSettingsRoot.visibleDefs(.samantha, store: s).map(\.key)
                .filter { $0 == IslandSettingsSchema.voiceLiveBulk || $0 == IslandSettingsSchema.voiceLiveIntelligence }
        }
        s.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        XCTAssertEqual(liveRows(), [IslandSettingsSchema.voiceLiveBulk, IslandSettingsSchema.voiceLiveIntelligence])
        XCTAssertEqual(s.string(IslandSettingsSchema.voiceLiveBulk), "gpt-6-luna")
        XCTAssertEqual(s.string(IslandSettingsSchema.voiceLiveIntelligence), "gpt-6.1-sol")
        s.set("grok-voice-latest", for: IslandSettingsSchema.voiceModel)
        XCTAssertEqual(liveRows(), [])
        // Voice picks the GPT-Live voice; Listening / Microphone rows are gone with Realtime.
        func shown() -> Set<String> { Set(IslandSettingsRoot.visibleDefs(.samantha, store: s).map(\.key)) }
        XCTAssertFalse(shown().contains(IslandSettingsSchema.voiceVoice))
        XCTAssertTrue(shown().isDisjoint(with: [IslandSettingsSchema.voiceEagerness, IslandSettingsSchema.voiceMic]))
        s.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        XCTAssertTrue(shown().contains(IslandSettingsSchema.voiceVoice))
    }

    func testLiveBackendKeysRoundTripTheSharedFile() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("voice-live-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let saved = VoiceSettingsStore.path
        defer { VoiceSettingsStore.path = saved }
        VoiceSettingsStore.path = dir.appendingPathComponent("settings.json")
        let s = IslandSettingsStore(defaults: UserDefaults(suiteName: "voice-\(UUID().uuidString)")!,
                                    tokens: MemoryTokenStore(), voiceInFile: true)
        s.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        s.set("gpt-6-astra", for: IslandSettingsSchema.voiceLiveIntelligence)
        let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: VoiceSettingsStore.path)) as? [String: Any]
        XCTAssertEqual(obj?["realtime_model"] as? String, "gpt-live-1")
        XCTAssertEqual(obj?["live_intelligence_model"] as? String, "gpt-6-astra")
        XCTAssertEqual(obj?["live_bulk_model"] as? String, "gpt-6-luna")
        let env = VoiceSettingsStore.applyToEnvironment(VoiceSettingsStore.load())
        XCTAssertEqual(env["VOICE_CUA_LIVE_INTELLIGENCE_MODEL"], "gpt-6-astra")
    }

    func testSamanthaEnabledRowAndTaskModelsSection() {
        let rows = IslandSettingsSchema.defs(for: .samantha)
        XCTAssertEqual(rows.first?.label, "Enabled")
        guard case .toggle = rows.first?.kind else { return XCTFail("Samantha's top row is a plain toggle (no icon)") }
        XCTAssertEqual(rows.filter { $0.visibleWhen == IslandSettingsSchema.whenLive }.map(\.section), ["Voice", "Task models", "Task models"])
        XCTAssertFalse(IslandSettingsSchema.all.contains { $0.section == "GPT-Live-1 Tasks" })
        XCTAssertEqual(IslandSettingsSchema.def(AgentSource.grokBot.keyPrefix + "enabled")?.label, AgentSource.grokBot.title, "other sources keep their master row")
    }

    func testComputerUsePaneIsThePermissionRows() {
        let rows = IslandSettingsSchema.defs(for: .computerUse)
        XCTAssertEqual(rows.map(\.key), ACUPermission.allCases.map(\.key), "same rows as onboarding")
        XCTAssertEqual(Array(rows.prefix(2)).map(\.key), [IslandSettingsSchema.cuaAccessibility, IslandSettingsSchema.cuaScreenRecording])
        XCTAssertTrue(rows.allSatisfy { if case .permission = $0.kind { return true }; return false })
        XCTAssertTrue(SettingsTab.panes.contains(.computerUse))
        XCTAssertNil(IslandSettingsSchema.def("island.agent.samantha.voiceSettings"), "old Voice Settings button is gone")
    }

    func testSchemaCoversEveryTabWithUniqueKeys() {
        for tab in SettingsTab.allCases {
            XCTAssertFalse(IslandSettingsSchema.defs(for: tab).isEmpty || tab == .about && false, "\(tab) has rows")
        }
        let keys = IslandSettingsSchema.all.map(\.key)
        XCTAssertEqual(Set(keys).count, keys.count, "unique keys")
        XCTAssertTrue(keys.allSatisfy { $0.hasPrefix("island.") || $0.hasPrefix("voice.") || $0.hasPrefix("cua.") })
        // AI Agents contains the individual sources and shared roster; About is pinned.
        let agents = SettingsTab.sidebar.first { $0.0 == "AI Agents" }?.1
        XCTAssertEqual(agents, [.samantha, .grokBot, .cursor, .codex, .agentRoster])
        XCTAssertFalse(SettingsTab.sidebar.contains { $0.0 == "Samantha" }, "no Samantha header")
        XCTAssertFalse(SettingsTab.sidebar.flatMap(\.1).contains(.about))
        XCTAssertEqual(SettingsTab.panes.last, .about)
        XCTAssertEqual(Set(SettingsTab.panes), Set(SettingsTab.allCases), "every pane is reachable")
    }

    /// Unbuilt (Soon) rows and per-agent durations are gone from the UI and their keys are not in the schema.
    func testRetiredRowsAreGoneAndTheirValuesDropped() {
        let keys = Set(IslandSettingsSchema.all.map(\.key))
        for k in IslandSettingsSchema.retiredKeys { XCTAssertFalse(keys.contains(k), k) }
        for p in IslandSettingsSchema.retiredPrefixes { XCTAssertFalse(keys.contains { $0.hasPrefix(p) }, p) }
        XCTAssertEqual(IslandSettingsSchema.all.filter { $0.section == "Durations" }.map(\.tab), [.general, .general, .general],
                       "one global Durations card")
        let s = store()
        defaults.set(9, forKey: "island.agent.cursor.needsYouPeek")
        defaults.set(true, forKey: "island.lockScreen.widget.weather")
        defaults.set(false, forKey: "island.general.launchAtLogin")
        defaults.set("all", forKey: IslandSettingsSchema.displayOn)
        defaults.set(5, forKey: IslandSettingsSchema.resultPeek)
        s.dropRetiredSettings()
        XCTAssertNil(defaults.object(forKey: "island.agent.cursor.needsYouPeek"))
        XCTAssertNil(defaults.object(forKey: "island.lockScreen.widget.weather"))
        XCTAssertNil(defaults.object(forKey: "island.general.launchAtLogin"))
        XCTAssertEqual(s.string(IslandSettingsSchema.displayOn), "main", "All displays (never built) → main")
        XCTAssertEqual(s.double(IslandSettingsSchema.resultPeek), 5, "live settings untouched")
    }

    /// At most one "i" per card; captions replace the master rows' info.
    func testAtMostOneInfoGlyphPerCard() {
        for t in SettingsTab.allCases {
            for g in IslandSettingsRoot.group(IslandSettingsSchema.defs(for: t)) {
                let n = g.rows.filter { r in r.info != nil && { if case .token = r.kind { return false }; return true }() }.count
                XCTAssertLessThanOrEqual(n, 1, "\(t) \(g.section ?? "-")")
            }
        }
        XCTAssertEqual(IslandSettingsSchema.def(IslandSettingsSchema.fileTrayEnabled)?.caption, "Drag files onto the island to hold them here.")
    }

    func testDefaultsAndPersistenceRoundTrip() {
        let s = store()
        XCTAssertTrue(s.bool(IslandSettingsSchema.hideInFullscreen))
        XCTAssertEqual(s.string(IslandSettingsSchema.idleActivity), "duo")
        XCTAssertEqual(s.double(IslandSettingsSchema.errorPeek), 6)
        s.set(false, for: IslandSettingsSchema.hideInFullscreen)
        s.set(7.5, for: IslandSettingsSchema.errorPeek)
        s.set("none", for: IslandSettingsSchema.idleActivity)
        let again = store()
        XCTAssertFalse(again.bool(IslandSettingsSchema.hideInFullscreen))
        XCTAssertEqual(again.double(IslandSettingsSchema.errorPeek), 7.5)
        XCTAssertEqual(again.string(IslandSettingsSchema.idleActivity), "none")
    }

    func testObserverFiresOnSet() {
        let s = store()
        var n = 0
        s.addObserver { _ in n += 1 }
        s.set(true, for: IslandSettingsSchema.expandOnHover)
        XCTAssertEqual(n, 1)
    }

    func testAgentGatingDefaults() {
        let s = store()
        XCTAssertTrue(s.sourceEnabled(.samantha))
        XCTAssertTrue(s.mayRaiseApproval(.cursor))
        XCTAssertTrue(s.mayRaiseApproval(.grokBot))
        XCTAssertFalse(s.mayRaiseApproval(.codex), "Codex defaults to info cap, no approvals")
        s.set("ambient", for: AgentSource.cursor.keyPrefix + "cap")
        XCTAssertFalse(s.mayRaiseApproval(.cursor), "cap below attention cannot raise cards")
        XCTAssertEqual(AgentSource.match("Grok Bot"), .grokBot)
        XCTAssertEqual(AgentSource.match("Cursor Agent"), .cursor)
        XCTAssertNil(AgentSource.match("Terminal"))
        XCTAssertEqual(AgentSource.match("Comet"), .grokBot)
        s.set("garbage", for: AgentSource.codex.keyPrefix + "cap")
        XCTAssertEqual(s.cap(.codex), .info, "corrupt prefs fall back to the per-source default")
    }

    func testTokenRotateAndMaskNeverShowsFullToken() throws {
        let mem = MemoryTokenStore()
        let s = IslandSettingsStore(defaults: defaults, tokens: mem)
        XCTAssertNil(s.maskedToken(.cursor), "no token → Create token")
        try s.rotateToken(.cursor)
        let t1 = try XCTUnwrap(mem.tokens["cursor"])
        XCTAssertGreaterThanOrEqual(t1.count, 40)
        XCTAssertEqual(s.maskedToken(.cursor), "••••" + t1.suffix(4))
        XCTAssertFalse(s.maskedToken(.cursor)?.contains(t1.prefix(8)) ?? true)
        try s.rotateToken(.cursor)
        let t2 = try XCTUnwrap(mem.tokens["cursor"])
        XCTAssertNotEqual(t1, t2, "rotate replaces the old token")
        let pb = NSPasteboard.withUniqueName()
        XCTAssertTrue(s.copyToken(.cursor, pasteboard: pb))
        XCTAssertEqual(pb.string(forType: .string), t2)
        XCTAssertNotNil(pb.types?.first { $0.rawValue == "org.nspasteboard.ConcealedType" }, "concealed")
        XCTAssertFalse(s.copyToken(.codex, pasteboard: pb))
        pb.releaseGlobally()
        XCTAssertTrue(t2.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }, "base64url")
    }

    func testModelFiltersDisabledSourcesAndApprovals() {
        let m = IslandModel()
        m.agentAllowed = { AgentSource.match($0.name) != .codex }
        m.approvalAllowed = { AgentSource.match($0.agent) != .cursor }
        _ = m.apply(IslandSnapshot(kind: "ready", agents: [
            IslandAgent(id: "c", name: "Codex", app: "Codex", status: "running"),
            IslandAgent(id: "g", name: "Grok Bot", app: "Comet", status: "running"),
        ], agent_approval: IslandAgentApproval(id: "x", agent: "Cursor", app: "Cursor", title: "Run?")))
        XCTAssertEqual(m.agents.map(\.id), ["g"])
        XCTAssertNil(m.agentApproval, "approval dropped when the source may not raise cards")
        // Settings change applies immediately, without waiting for the next snapshot.
        m.agentAllowed = { _ in true }
        m.approvalAllowed = { _ in true }
        m.reapplyFilters()
        XCTAssertEqual(m.agents.map(\.id), ["c", "g"])
        XCTAssertNotNil(m.agentApproval)
    }

    func testIdleActivityNoneHidesPillAndHitRects() {
        let m = IslandModel()
        _ = m.apply(IslandSnapshot(kind: "ready"))
        XCTAssertFalse(m.hitRects.isEmpty)
        m.idleActivity = "none"
        XCTAssertTrue(m.idleHidden)
        XCTAssertTrue(m.hitRects.isEmpty)
        _ = m.apply(IslandSnapshot(kind: "listening", title: "Listening"))
        XCTAssertFalse(m.idleHidden, "live states always show")
        m.idleActivity = "agents"
        _ = m.apply(IslandSnapshot(kind: "ready"))
        XCTAssertTrue(m.idleHidden, "agents-only: hidden with no agents")
        _ = m.apply(IslandSnapshot(kind: "ready", agents: [IslandAgent(id: "a", name: "Cursor", app: "Cursor", status: "running")]))
        XCTAssertFalse(m.idleHidden)
    }

    func testIdleActivitySamanthaHidesAgentCircleWhenIdle() {
        let m = IslandModel()
        m.idleActivity = "samantha"
        _ = m.apply(IslandSnapshot(kind: "ready", agents: [IslandAgent(id: "a", name: "Cursor", app: "Cursor", status: "running")]))
        XCTAssertFalse(m.showAgentCircle)
        m.idleActivity = "duo"
        XCTAssertTrue(m.showAgentCircle)
    }

    func testSamanthaOffKeepsVoiceStatesIdleButStillShowsConfirm() {
        let m = IslandModel()
        m.samanthaEnabled = false
        _ = m.apply(IslandSnapshot(kind: "listening", title: "Listening"))
        XCTAssertTrue(m.isIdlePresentation)
        _ = m.apply(IslandSnapshot(kind: "confirm", title: "Send?", confirm_prompt: "Send the email?"))
        XCTAssertEqual(m.presentation, .needsYou)
    }

    func testDurationsAreSettable() {
        IslandModel.doneSettle = 2
        let since = Date()
        XCTAssertEqual(IslandModel.presentation(kind: "done", stepTotal: 0, approval: nil, since: since,
                                                now: since.addingTimeInterval(2.5)), .doneSettled)
        IslandModel.doneSettle = 4
        XCTAssertEqual(IslandModel.presentation(kind: "done", stepTotal: 0, approval: nil, since: since,
                                                now: since.addingTimeInterval(2.5)), .done)
    }

    func testDisplayChoiceFallsBackToMain() {
        guard let main = NSScreen.screens.first else { return }
        XCTAssertEqual(IslandDisplayChoice.pick(choice: "main", screens: NSScreen.screens, selectedUUID: nil, main: main), main)
        XCTAssertEqual(IslandDisplayChoice.pick(choice: "all", screens: NSScreen.screens, selectedUUID: nil, main: main), main)
        XCTAssertEqual(IslandDisplayChoice.pick(choice: "builtIn", screens: NSScreen.screens, selectedUUID: nil, main: main).frame.isEmpty, false)
    }

    func testSliderFormat() {
        XCTAssertEqual(SettingRow.format(1.5, step: 0.1, unit: "s"), "1.5 s")
        XCTAssertEqual(SettingRow.format(40, step: 1, unit: "%"), "40 %")
    }

    /// Offscreen renders of every tab (no window, no focus). Env-gated:
    /// CUA_SETTINGS_RENDER_DIR=<dir> swift test --filter IslandSettingsTests/testRenderTabs
    func testRenderTabs() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_SETTINGS_RENDER_DIR"] else {
            throw XCTSkip("set CUA_SETTINGS_RENDER_DIR to render")
        }
        let s = store()
        try s.rotateToken(.grokBot)
        try s.rotateToken(.cursor)
        let tabs = SettingsTab.allCases
        for (i, t) in tabs.enumerated() {
            let view = IslandSettingsRoot(store: s, tab: t, rendering: true)
            let r = ImageRenderer(content: view)
            r.scale = 2
            guard let img = r.cgImage else { XCTFail("render \(t)"); continue }
            let rep = NSBitmapImageRep(cgImage: img)
            let url = URL(fileURLWithPath: dir).appendingPathComponent(String(format: "%02d_%@.png", i, t.rawValue))
            try rep.representation(using: .png, properties: [:])?.write(to: url)
        }
    }
}
