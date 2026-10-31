import AppKit
import ServiceManagement
import XCTest
@testable import CUAService

@MainActor
final class IslandSettingsRedesignTests: XCTestCase {
    typealias K = IslandSettingsSchema
    private var suites: [String] = []

    private func store(login: IslandLoginItem? = nil) -> IslandSettingsStore {
        let name = "acu.settings.redesign.\(UUID().uuidString)"
        suites.append(name)
        return IslandSettingsStore(defaults: TestDefaults.make(name)!, tokens: MemoryTokenStore(), loginItem: login,
                                   screens: { [.init(uuid: "external", name: "Studio Display", builtIn: false)] })
    }

    override func tearDown() {
        for name in suites { TestDefaults.make(name)?.removePersistentDomain(forName: name) }
        suites = []
        IslandModel.peekDone = 4; IslandModel.peekError = 6; IslandModel.peekDwell = 4; IslandModel.doneSettle = 4
    }

    func testMigrationIsIdempotentAndPreservesLaterChoices() {
        let s = store(), d = s.defaults
        d.set("ambient", forKey: AgentSource.cursor.keyPrefix + "cap")
        d.set("ambient", forKey: AgentSource.samantha.keyPrefix + "cap")
        d.set(true, forKey: K.expandOnHover)
        d.set(0, forKey: K.hoverDuration)
        d.set(20, forKey: K.resultPeek)
        d.set(9, forKey: K.errorPeek)
        d.set(7, forKey: K.doneSettle)
        d.set(5, forKey: K.peekDuration)
        d.set(true, forKey: K.mutePromptSound)
        d.set(false, forKey: AgentSource.codex.keyPrefix + "enabled")
        d.set("all", forKey: K.displayOn)
        s.dropRetiredSettings()
        XCTAssertEqual(s.cap(.cursor), .info)
        XCTAssertEqual(s.cap(.samantha), .attention)
        XCTAssertEqual(s.string(K.openOn), "hover")
        XCTAssertEqual(s.double(K.hoverDuration), 0.1)
        XCTAssertEqual(s.double(K.alertDuration), 8)
        XCTAssertFalse(s.bool(K.promptSound))
        XCTAssertTrue(s.sourceEnabled(.codex))
        XCTAssertTrue(d.bool(forKey: "island.migrated.0264"))
        XCTAssertEqual(s.string(K.displayOn), "main")
        for key in [K.expandOnHover, K.resultPeek, K.errorPeek, K.doneSettle, K.peekDuration, K.mutePromptSound] {
            XCTAssertNil(d.object(forKey: key), key)
        }
        s.set(false, for: AgentSource.codex.keyPrefix + "enabled")
        s.set(5, for: K.alertDuration)
        s.set("click", for: K.openOn)
        s.set(true, for: K.promptSound)
        s.dropRetiredSettings()
        XCTAssertFalse(s.sourceEnabled(.codex), "the one-time migration never resets a later off")
        XCTAssertEqual(s.double(K.alertDuration), 5)
        XCTAssertEqual(s.string(K.openOn), "click")
        XCTAssertTrue(s.bool(K.promptSound))
    }

    func testMigrationBoundsAndNewKeysWin() {
        let s = store(), d = s.defaults
        d.set("info", forKey: AgentSource.samantha.keyPrefix + "cap")
        d.set(2, forKey: K.resultPeek)
        d.set(1, forKey: K.hoverDuration)
        s.dropRetiredSettings()
        XCTAssertEqual(s.cap(.samantha), .attention)
        XCTAssertEqual(s.double(K.alertDuration), 3)
        XCTAssertEqual(s.double(K.hoverDuration), 0.5)
        d.set(8, forKey: K.resultPeek); d.set(5.5, forKey: K.alertDuration)
        d.set(true, forKey: K.expandOnHover); d.set("click", forKey: K.openOn)
        d.set(true, forKey: K.mutePromptSound); d.set(true, forKey: K.promptSound)
        s.dropRetiredSettings()
        XCTAssertEqual(s.double(K.alertDuration), 5.5)
        XCTAssertEqual(s.string(K.openOn), "click")
        XCTAssertTrue(s.bool(K.promptSound))
    }

    func testSourceTableDefaultsAndDisabledControls() {
        let s = store()
        XCTAssertEqual(K.tableSources, [.samantha, .grokBot, .codex, .claude, .cursor])
        XCTAssertFalse(s.bool(K.naturalMovement), "the existing behaviour default is preserved")
        for source in AgentSource.allCases {
            XCTAssertTrue(s.sourceEnabled(source))
            XCTAssertEqual(s.cap(source), source == .agentRoster ? .info : .attention)
            XCTAssertEqual(s.mayRaiseApproval(source), source.canDecide && source != .agentRoster)
        }
        let codexApprove = AgentSource.codex.keyPrefix + "canRaiseApprovals"
        s.set(true, for: codexApprove)
        XCTAssertFalse(s.mayRaiseApproval(.codex))
        XCTAssertFalse(s.sourceControlEnabled(codexApprove))
        s.set("info", for: AgentSource.samantha.keyPrefix + "cap")
        XCTAssertEqual(s.string(AgentSource.samantha.keyPrefix + "cap"), "attention")
        s.defaults.set("ambient", forKey: AgentSource.samantha.keyPrefix + "cap")
        XCTAssertEqual(s.cap(.samantha), .attention)
        s.set("info", for: AgentSource.cursor.keyPrefix + "cap")
        XCTAssertTrue(s.sourceControlEnabled(AgentSource.cursor.keyPrefix + "canRaiseApprovals"), "Approve is independent of the alert level")
        s.set(false, for: AgentSource.grokBot.keyPrefix + "enabled")
        XCTAssertFalse(s.sourceControlEnabled(AgentSource.grokBot.keyPrefix + "cap"))
        XCTAssertFalse(s.sourceControlEnabled(AgentSource.grokBot.keyPrefix + "canRaiseApprovals"))
    }

    func testDisplayOptionsAreComputedFromInjectedScreensAtRenderTime() {
        let s = store()
        let hook = K.def(K.displayOn)!.dynamicOptions!
        XCTAssertEqual(hook(s).map(\.value), ["main"])
        s.screens = { [.init(uuid: "a", name: "Built-in Retina", builtIn: true)] }
        XCTAssertEqual(hook(s).map(\.value), ["main", "builtIn"])
        s.screens = { [.init(uuid: "a", name: "Left", builtIn: false), .init(uuid: "b", name: "Right", builtIn: false)] }
        XCTAssertEqual(hook(s).map(\.label), ["Display with menu bar", "Left", "Right"])
        s.set("selected:b", for: K.displayOn)
        XCTAssertEqual(s.string(K.displayOn), "selected")
        XCTAssertEqual(s.string(K.selectedDisplay), "b")
        XCTAssertEqual(s.displaySelection(), "selected:b")
        s.screens = { [.init(uuid: "a", name: "Left", builtIn: false)] }
        XCTAssertEqual(s.displaySelection(), "main")
        XCTAssertEqual(s.string(K.selectedDisplay), "b", "disconnect fallback preserves the UUID")
        s.screens = { [.init(uuid: "a", name: "Left", builtIn: false), .init(uuid: "b", name: "Right", builtIn: false)] }
        XCTAssertEqual(s.displaySelection(), "selected:b", "the original choice returns on reconnect")
        s.set("builtIn", for: K.displayOn)
        s.screens = { [] }
        XCTAssertEqual(hook(s).map(\.value), ["main", "builtIn"], "a stored built-in choice stays available")
    }

    func testLoginItemSeedsOnceOffSticksAndUsesLiveStatus() {
        let login = MemoryIslandLoginItem(), s = store(login: login)
        XCTAssertFalse(s.bool(K.openAtLogin))
        s.seedLoginItem(); s.seedLoginItem()
        XCTAssertEqual(login.registrations, 1)
        XCTAssertTrue(s.bool(K.openAtLogin))
        s.set(false, for: K.openAtLogin)
        s.seedLoginItem()
        XCTAssertEqual(login.registrations, 1)
        XCTAssertEqual(login.unregistrations, 1)
        XCTAssertFalse(s.bool(K.openAtLogin))
        let restarted = IslandSettingsStore(defaults: s.defaults, tokens: MemoryTokenStore(), loginItem: login)
        restarted.seedLoginItem()
        XCTAssertFalse(restarted.bool(K.openAtLogin))
        XCTAssertEqual(login.registrations, 1)
        login.status = .requiresApproval
        XCTAssertTrue(restarted.bool(K.openAtLogin))
    }

    func testLoginItemApprovalAndAlreadyRegisteredStatus() {
        let login = MemoryIslandLoginItem()
        login.statusAfterRegister = .requiresApproval
        let s = store(login: login)
        s.seedLoginItem()
        XCTAssertTrue(s.bool(K.openAtLogin))
        XCTAssertEqual(login.settingsOpened, 1)
        let enabled = MemoryIslandLoginItem(); enabled.status = .enabled
        let existing = store(login: enabled)
        existing.seedLoginItem()
        XCTAssertEqual(enabled.registrations, 0)
        XCTAssertTrue(existing.bool(K.openAtLogin))
    }

    func testRestoreAgentsLeavesTokensGeneralAndVoiceUntouched() throws {
        let s = store()
        for source in AgentSource.allCases {
            try s.rotateToken(source)
            s.set(false, for: source.keyPrefix + "enabled")
            s.set("critical", for: source.keyPrefix + "cap")
            s.set(false, for: source.keyPrefix + "canRaiseApprovals")
        }
        let tokens = (s.tokens as! MemoryTokenStore).tokens
        s.set(8, for: K.alertDuration); s.set(false, for: K.promptSound)
        for key in [K.respectFocus, K.hideWhileSharing, K.hideInFullscreen] { s.set(false, for: key) }
        s.set(true, for: K.hideFromScreenCapture)
        s.set("hover", for: K.openOn); s.set("kept", for: K.voiceLiveBulk)
        var observed = false; s.addObserver { _ in observed = true }
        s.restoreAgentDefaults()
        XCTAssertTrue(observed)
        XCTAssertEqual((s.tokens as! MemoryTokenStore).tokens, tokens)
        for source in AgentSource.allCases {
            XCTAssertTrue(s.sourceEnabled(source)); XCTAssertEqual(s.cap(source), source == .agentRoster ? .info : .attention)
            XCTAssertEqual(s.mayRaiseApproval(source), source.canDecide && source != .agentRoster)
        }
        XCTAssertEqual(s.double(K.alertDuration), 4); XCTAssertTrue(s.bool(K.promptSound))
        for key in [K.respectFocus, K.hideWhileSharing, K.hideInFullscreen] { XCTAssertTrue(s.bool(key)) }
        XCTAssertFalse(s.bool(K.hideFromScreenCapture))
        XCTAssertEqual(s.string(K.openOn), "hover"); XCTAssertEqual(s.string(K.voiceLiveBulk), "kept")
    }

    func testAlertDurationAndOpenOnAreWired() {
        let s = store()
        let controller = IslandController(gatewayPort: 1, store: s, hubPort: nil, startsSystem: false)
        s.set(7.5, for: K.alertDuration)
        controller.applySettings(s)
        XCTAssertEqual(IslandModel.peekDone, 7.5)
        XCTAssertEqual(IslandModel.peekError, 9.5)
        XCTAssertEqual(IslandModel.peekDwell, 7.5)
        XCTAssertEqual(IslandModel.doneSettle, 4)
        let panel = SamanthaMenuBarIslandPanel()
        s.set("hover", for: K.openOn); s.set(0.3, for: K.hoverDuration)
        panel.applySettings(s)
        XCTAssertTrue(panel.expandOnHover)
        XCTAssertEqual(panel.hoverDelay, 0.3)
        s.set("click", for: K.openOn)
        panel.applySettings(s)
        XCTAssertFalse(panel.expandOnHover)
    }

    func testLastPaneAndLegacyAliases() {
        let s = store()
        let nav = SettingsNav(defaults: s.defaults)
        nav.tab = .agents
        XCTAssertEqual(s.defaults.string(forKey: SettingsNav.lastPaneKey), "agents")
        XCTAssertEqual(SettingsNav.restoredTab(s.defaults), .agents)
        s.defaults.set("removed", forKey: SettingsNav.lastPaneKey)
        XCTAssertEqual(SettingsNav.restoredTab(s.defaults), .general)
        for name in ["interaction", "display", "sound", "connectivity", "now-playing", "calendar", "file_tray"] {
            XCTAssertEqual(SettingsTab.forPane(name)!, .general)
            XCTAssertTrue(SettingsTab.opensMore(name))
        }
        for name in ["grokbot", "cursor", "codex", "agent roster"] { XCTAssertEqual(SettingsTab.forPane(name)!, .agents) }
        XCTAssertNil(SettingsTab.forPane("unknown"))
    }

    func testDisclosureAndSourceTableKeyboardModel() {
        let s = store(), nav = SettingsNav()
        nav.focus = K.moreExpanded
        _ = SettingsKeyboard.handle(.activate, nav: nav, store: s, defs: IslandSettingsRoot.visibleDefs(.general, store: s), panes: SettingsTab.panes)
        XCTAssertTrue(s.bool(K.moreExpanded))
        XCTAssertTrue(IslandSettingsRoot.visibleDefs(.general, store: s).contains { $0.key == K.haptics })
        nav.tab = .agents
        let defs = IslandSettingsRoot.visibleDefs(.agents, store: s)
        let controls = SettingsKeyboard.focusable(defs).map(\.key)
        for source in K.tableSources {
            for suffix in ["enabled", "cap", "canRaiseApprovals"] { XCTAssertTrue(controls.contains(source.keyPrefix + suffix)) }
        }
        nav.focus = AgentSource.codex.keyPrefix + "canRaiseApprovals"
        _ = SettingsKeyboard.handle(.activate, nav: nav, store: s, defs: defs, panes: SettingsTab.panes)
        XCTAssertFalse(s.bool(nav.focus!))
        nav.focus = AgentSource.samantha.keyPrefix + "cap"
        _ = SettingsKeyboard.handle(.decrease, nav: nav, store: s, defs: defs, panes: SettingsTab.panes)
        XCTAssertEqual(s.cap(.samantha), .attention)
        XCTAssertFalse(IslandSettingsRoot.visibleDefs(.agents, store: s).contains { $0.kind == .token })
        nav.focus = K.tokensExpanded
        _ = SettingsKeyboard.handle(.activate, nav: nav, store: s, defs: defs, panes: SettingsTab.panes)
        XCTAssertEqual(IslandSettingsRoot.visibleDefs(.agents, store: s).filter { $0.kind == .token }.count, 5)
    }
}
