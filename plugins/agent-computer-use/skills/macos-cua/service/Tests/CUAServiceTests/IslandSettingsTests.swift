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

    func testSchemaCoversEveryTabWithUniqueKeys() {
        for tab in SettingsTab.allCases {
            XCTAssertFalse(IslandSettingsSchema.defs(for: tab).isEmpty || tab == .about && false, "\(tab) has rows")
        }
        let keys = IslandSettingsSchema.all.map(\.key)
        XCTAssertEqual(Set(keys).count, keys.count, "unique keys")
        XCTAssertTrue(keys.allSatisfy { $0.hasPrefix("island.") })
        // Agents section lists the four sources; Battery only with an internal battery.
        let agents = SettingsTab.sidebar(hasBattery: false).first { $0.0 == "AI Agents" }?.1
        XCTAssertEqual(agents, [.samantha, .grokBot, .cursor, .codex])
        XCTAssertFalse(SettingsTab.sidebar(hasBattery: false).flatMap(\.1).contains(.battery))
        XCTAssertTrue(SettingsTab.sidebar(hasBattery: true).flatMap(\.1).contains(.battery))
        // Unwired features are visibly "Soon".
        for t in [SettingsTab.lockScreen] {
            XCTAssertTrue(IslandSettingsSchema.defs(for: t).allSatisfy { !$0.wired }, "\(t) is Soon")
        }
        XCTAssertTrue(IslandSettingsSchema.defs(for: .general).filter(\.wired).count >= 10)
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
        XCTAssertEqual(s.maskedToken(.cursor), "Not set")
        try s.rotateToken(.cursor)
        let t1 = try XCTUnwrap(mem.tokens["cursor"])
        XCTAssertGreaterThanOrEqual(t1.count, 40)
        XCTAssertEqual(s.maskedToken(.cursor), "••••" + t1.suffix(3))
        XCTAssertFalse(s.maskedToken(.cursor).contains(t1.prefix(8)))
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
            let view = IslandSettingsRoot(store: s, tab: t, rendering: true, hasBattery: t == .battery)
            let r = ImageRenderer(content: view)
            r.scale = 2
            guard let img = r.cgImage else { XCTFail("render \(t)"); continue }
            let rep = NSBitmapImageRep(cgImage: img)
            let url = URL(fileURLWithPath: dir).appendingPathComponent(String(format: "%02d_%@.png", i, t.rawValue))
            try rep.representation(using: .png, properties: [:])?.write(to: url)
        }
    }
}
