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
        defaults = TestDefaults.make(suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
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
        let defaults = TestDefaults.make("voice-\(UUID().uuidString)")!
        let s = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore(), voiceInFile: true)
        XCTAssertEqual(s.string(IslandSettingsSchema.voiceModel), "gpt-live-1", "retired Realtime id maps to GPT-Live-1")
        s.set("ash", for: IslandSettingsSchema.voiceVoice)
        let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: VoiceSettingsStore.path)) as? [String: Any]
        XCTAssertEqual(obj?["realtime_voice"] as? String, "ash")
        XCTAssertEqual(obj?["realtime_model"] as? String, "gpt-live-1")
        XCTAssertEqual(obj?["extra"] as? String, "kept")
        XCTAssertNil(defaults.object(forKey: IslandSettingsSchema.voiceVoice), "voice rows never land in UserDefaults")
        // Only Voice and Model are schema rows. Voice backend choices persist in the file without UI defaults.
        for key in [IslandSettingsSchema.voiceVoice, IslandSettingsSchema.voiceModel] {
            guard case .popup(let o)? = IslandSettingsSchema.def(key)?.kind else { return XCTFail("\(key) is a popup") }
            XCTAssertTrue(o.contains { $0.value == s.string(key) }, key)
        }
        XCTAssertNil(IslandSettingsSchema.def(IslandSettingsSchema.voiceLiveBulk))
        XCTAssertNil(IslandSettingsSchema.def(IslandSettingsSchema.voiceLiveIntelligence))
    }

    /// Model picker: GPT-Live-1 (default), Grok Voice 3.0 (same ids as voice_settings.MODELS). Realtime removed.
    /// Samantha exposes only Voice and Model; backend keys remain file-backed metadata.
    func testModelPickerAndSamanthaRows() {
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

        let rows = IslandSettingsSchema.defs(for: .samantha)
        XCTAssertEqual(rows.map(\.key), [IslandSettingsSchema.voiceVoice, IslandSettingsSchema.voiceModel])
        XCTAssertNil(IslandSettingsSchema.def(IslandSettingsSchema.voiceLiveBulk))
        XCTAssertNil(IslandSettingsSchema.def(IslandSettingsSchema.voiceLiveIntelligence))
        XCTAssertTrue(IslandSettingsRoot.visibleDefs(.samantha, store: store()).allSatisfy {
            $0.key == IslandSettingsSchema.voiceVoice || $0.key == IslandSettingsSchema.voiceModel
        })
    }

    func testLiveBackendKeysRoundTripTheSharedFile() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("voice-live-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let saved = VoiceSettingsStore.path
        defer { VoiceSettingsStore.path = saved }
        VoiceSettingsStore.path = dir.appendingPathComponent("settings.json")
        let s = IslandSettingsStore(defaults: TestDefaults.make("voice-\(UUID().uuidString)")!,
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

    func testSamanthaSourceSettingsAreMetadataNotPaneRows() {
        let rows = IslandSettingsSchema.defs(for: .samantha)
        XCTAssertEqual(rows.map(\.key), [IslandSettingsSchema.voiceVoice, IslandSettingsSchema.voiceModel])
        let enabled = AgentSource.samantha.keyPrefix + "enabled"
        XCTAssertTrue(IslandSettingsSchema.all.contains { $0.key == enabled }, "source table metadata remains in the schema")
        XCTAssertFalse(IslandSettingsSchema.defs(for: .agents).contains { $0.key == enabled },
                       "the source table expands controls without individual settings rows")
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
            XCTAssertFalse(IslandSettingsSchema.defs(for: tab).isEmpty, "\(tab) has rows")
        }
        let keys = IslandSettingsSchema.all.map(\.key)
        XCTAssertEqual(Set(keys).count, keys.count, "unique keys")
        XCTAssertTrue(keys.allSatisfy { $0.hasPrefix("island.") || $0.hasPrefix("voice.") || $0.hasPrefix("cua.") })
        let sourceKeys = AgentSource.allCases.flatMap { IslandSettingsSchema.agent($0).map(\.key) }
        XCTAssertTrue(Set(keys).isSuperset(of: sourceKeys), "per-source controls remain available as source-table metadata")
        let agentRows = IslandSettingsSchema.defs(for: .agents)
        XCTAssertTrue(Set(agentRows.map(\.key)).isDisjoint(with: sourceKeys), "source rows are expanded by the table")
        XCTAssertTrue(agentRows.contains { d in
            guard d.key == IslandSettingsSchema.sourceTable else { return false }
            if case .sourceTable = d.kind { return true }
            return false
        })
        XCTAssertEqual(SettingsTab.allCases, [.general, .agents, .samantha, .computerUse, .about])
        XCTAssertEqual(SettingsTab.sidebar.flatMap(\.1), [.general, .agents, .samantha, .computerUse])
        XCTAssertEqual(SettingsTab.sidebar.map(\.0), [nil, "System"])
        XCTAssertFalse(SettingsTab.sidebar.flatMap(\.1).contains(.about))
        XCTAssertEqual(SettingsTab.panes.last, .about)
        XCTAssertEqual(Set(SettingsTab.panes), Set(SettingsTab.allCases), "every pane is reachable")
    }

    /// Unbuilt (Soon) rows and per-agent durations are gone from the UI and their keys are not in the schema.
    func testRetiredRowsAreGoneAndTheirValuesDropped() {
        let keys = Set(IslandSettingsSchema.all.map(\.key))
        for k in IslandSettingsSchema.retiredKeys { XCTAssertFalse(keys.contains(k), k) }
        for p in IslandSettingsSchema.retiredPrefixes { XCTAssertFalse(keys.contains { $0.hasPrefix(p) }, p) }
        XCTAssertTrue(IslandSettingsSchema.all.filter { $0.visibleWhen == IslandSettingsSchema.moreExpanded }
            .allSatisfy { $0.tab == .general }, "expanded More rows live under General")
        let s = store()
        defaults.set(9, forKey: "island.agent.cursor.needsYouPeek")
        defaults.set(true, forKey: "island.lockScreen.widget.weather")
        defaults.set(false, forKey: "island.general.launchAtLogin")
        defaults.set("all", forKey: IslandSettingsSchema.displayOn)
        defaults.set(5, forKey: IslandSettingsSchema.resultPeek)
        defaults.set(7, forKey: IslandSettingsSchema.errorPeek)
        s.dropRetiredSettings()
        XCTAssertNil(defaults.object(forKey: "island.agent.cursor.needsYouPeek"))
        XCTAssertNil(defaults.object(forKey: "island.lockScreen.widget.weather"))
        XCTAssertNil(defaults.object(forKey: "island.general.launchAtLogin"))
        XCTAssertEqual(s.string(IslandSettingsSchema.displayOn), "main", "All displays (never built) → main")
        XCTAssertEqual(s.double(IslandSettingsSchema.alertDuration), 5, "legacy result duration migrates to Alerts")
        XCTAssertNil(defaults.object(forKey: IslandSettingsSchema.resultPeek))
        XCTAssertNil(defaults.object(forKey: IslandSettingsSchema.errorPeek))
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
        XCTAssertEqual(s.double(IslandSettingsSchema.alertDuration), 4)
        XCTAssertEqual(s.string(IslandSettingsSchema.openOn), "click")
        XCTAssertEqual(s.double(IslandSettingsSchema.hoverDuration), 0.2, accuracy: 0.001)
        s.set(false, for: IslandSettingsSchema.hideInFullscreen)
        s.set(7.5, for: IslandSettingsSchema.alertDuration)
        s.set("hover", for: IslandSettingsSchema.openOn)
        s.set(0.3, for: IslandSettingsSchema.hoverDuration)
        s.set("none", for: IslandSettingsSchema.idleActivity)
        let again = store()
        XCTAssertFalse(again.bool(IslandSettingsSchema.hideInFullscreen))
        XCTAssertEqual(again.double(IslandSettingsSchema.alertDuration), 7.5)
        XCTAssertEqual(again.string(IslandSettingsSchema.openOn), "hover")
        XCTAssertEqual(again.double(IslandSettingsSchema.hoverDuration), 0.3, accuracy: 0.001)
        XCTAssertEqual(again.string(IslandSettingsSchema.idleActivity), "none")
        XCTAssertTrue(IslandSettingsRoot.visibleDefs(.general, store: again).contains { $0.key == IslandSettingsSchema.hoverDuration })
    }

    func testObserverFiresOnSet() {
        let s = store()
        var n = 0
        s.addObserver { _ in n += 1 }
        s.set(true, for: IslandSettingsSchema.moreExpanded)
        XCTAssertEqual(n, 1)
    }

    func testAgentGatingDefaults() {
        let s = store()
        XCTAssertTrue(s.sourceEnabled(.samantha))
        XCTAssertTrue(s.sourceEnabled(.codex))
        XCTAssertEqual(IslandSettingsSchema.tableSources.map { s.cap($0) }, Array(repeating: .attention, count: IslandSettingsSchema.tableSources.count))
        XCTAssertTrue(s.mayRaiseApproval(.cursor))
        XCTAssertTrue(s.mayRaiseApproval(.grokBot))
        XCTAssertFalse(s.mayRaiseApproval(.codex), "Codex is enabled but cannot raise approvals")
        XCTAssertTrue(IslandSettingsSchema.tableSources.filter { $0 != .codex }.allSatisfy { s.mayRaiseApproval($0) })
        for lowCap in ["ambient", "info"] {
            s.set(lowCap, for: AgentSource.samantha.keyPrefix + "cap")
            XCTAssertEqual(s.cap(.samantha), .attention, "Samantha cannot be capped below attention")
        }
        s.set("ambient", for: AgentSource.cursor.keyPrefix + "cap")
        XCTAssertFalse(s.mayRaiseApproval(.cursor), "cap below attention cannot raise cards")
        XCTAssertEqual(AgentSource.match("Grok Bot"), .grokBot)
        XCTAssertEqual(AgentSource.match("Cursor Agent"), .cursor)
        XCTAssertNil(AgentSource.match("Terminal"))
        XCTAssertEqual(AgentSource.match("Comet"), .grokBot)
        s.set("garbage", for: AgentSource.codex.keyPrefix + "cap")
        XCTAssertEqual(s.cap(.codex), .attention, "corrupt prefs fall back to the per-source default")
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

    func testAlertDurationAndOpenOnControls() {
        let s = store()
        guard let duration = IslandSettingsSchema.def(IslandSettingsSchema.alertDuration) else {
            return XCTFail("agent alert duration row exists")
        }
        guard case .slider(let min, let max, _, let unit) = duration.kind else {
            return XCTFail("agent alert duration is a slider")
        }
        XCTAssertEqual(min, 3)
        XCTAssertEqual(max, 8)
        XCTAssertEqual(unit, "s")
        XCTAssertEqual(duration.defaultValue, .double(4))
        XCTAssertFalse(IslandSettingsRoot.visibleDefs(.general, store: s).contains { $0.key == IslandSettingsSchema.hoverDuration })
        s.set("hover", for: IslandSettingsSchema.openOn)
        XCTAssertTrue(IslandSettingsRoot.visibleDefs(.general, store: s).contains { $0.key == IslandSettingsSchema.hoverDuration })
        s.set("click", for: IslandSettingsSchema.openOn)
        XCTAssertFalse(IslandSettingsRoot.visibleDefs(.general, store: s).contains { $0.key == IslandSettingsSchema.hoverDuration })
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
