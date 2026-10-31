import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Layout gate for the Island Settings window (dark-only): every pane in default (no tokens, permissions
/// missing), toggled (tokens set, permissions granted) and worst-case (long copy, max slider values) states,
/// token and permission states, and keyboard focus on each control kind. Fails on overlap,
/// clipping, cut-off text, 8 pt insets, and form rules (one leading / trailing column per card, 42 pt rows,
/// label and control sharing a centre line). Tokens live in MemoryTokenStore only.
///
/// Renders: CUA_ISLAND_RENDER_DIR=<dir> swift test --filter IslandSettingsLayoutGateTests/testRenderSettings
@MainActor
final class IslandSettingsLayoutGateTests: XCTestCase {
    private var suites: [String] = []

    override func setUp() {
        IslandLayoutRecorder.enabled = true
        BrandTests.injectApp()   // About: app icon + the app's Info.plist version
    }
    override func tearDown() {
        IslandLayoutRecorder.enabled = false
        IslandLayoutRecorder.shared.reset()
        for s in suites { TestDefaults.make(s)?.removePersistentDomain(forName: s) }
        suites = []
    }

    struct State { var name: String; var view: IslandSettingsRoot }

    private func store() -> IslandSettingsStore {
        let s = "cua.island.settings.gate.\(UUID().uuidString)"
        suites.append(s)
        let store = IslandSettingsStore(defaults: TestDefaults.make(s)!, tokens: MemoryTokenStore())
        store.screens = {
            [.init(uuid: "builtin", name: "Built-in Retina Display", builtIn: true),
             .init(uuid: "external", name: String(repeating: "A long connected display name ", count: 4), builtIn: false)]
        }
        store.seedLoginItem()
        return store
    }

    static let panes = SettingsTab.panes

    /// Every bool flipped, last tile / popup option, sliders at max, tokens set.
    private func toggled(_ s: IslandSettingsStore) {
        for d in IslandSettingsSchema.all {
            switch d.kind {
            case .master, .toggle, .disclosure:
                if case .bool(let b) = d.defaultValue { s.set(!b, for: d.key) }
            case .tiles(let o): if let v = o.last?.value { s.set(v, for: d.key) }
            case .popup(let o): if let v = (d.dynamicOptions?(s) ?? o).last?.value { s.set(v, for: d.key) }
            case .slider(_, let hi, _, _): s.set(hi, for: d.key)
            default: break
            }
        }
        for src in AgentSource.allCases { try? s.rotateToken(src) }
    }

    /// Worst-case copy: long labels, sections, tile / popup option names and button titles.
    static func worstDefs(_ t: SettingsTab) -> [SettingDef] {
        IslandSettingsSchema.defs(for: t).map { d in
            var d = d
            d.label += " with an unusually long translated label"
            d.section = d.section.map { $0 + " and a long translated section heading that runs on" }
            switch d.kind {
            case .tiles(let o): d.kind = .tiles(o.map { ($0.value, $0.label + " Extended", $0.symbol) })
            case .popup(let o): d.kind = .popup(o.map { ($0.value, $0.label + " (long translated name)") })
            case .button(let b): d.kind = .button(b + " in System Settings")
            case .permission(let b): d.kind = .permission(b + " in System Settings")
            default: break
            }
            d.caption = d.caption.map { $0 + " A second translated sentence that keeps going so the caption has to wrap." }
            return d
        }
    }

    static let granted: (String) -> Bool = { _ in true }
    static let missing: (String) -> Bool = { _ in false }

    func states() -> [State] {
        var out: [State] = []
        for t in Self.panes {
            out.append(State(name: "default-\(t.rawValue)", view: IslandSettingsRoot(store: store(), tab: t, rendering: true, permissions: Self.missing)))
            let s = store(); toggled(s)
            out.append(State(name: "toggled-\(t.rawValue)", view: IslandSettingsRoot(store: s, tab: t, rendering: true, permissions: Self.granted)))
            let w = store(); toggled(w)
            out.append(State(name: "worst-\(t.rawValue)", view: IslandSettingsRoot(store: w, tab: t, rendering: true, permissions: Self.missing, defs: Self.worstDefs)))
        }
        let more = store(); more.set(true, for: IslandSettingsSchema.moreExpanded)
        out.append(State(name: "more-expanded", view: IslandSettingsRoot(store: more, rendering: true)))
        let info = store()
        info.set("info", for: AgentSource.cursor.keyPrefix + "cap")
        out.append(State(name: "list-only-agents", view: IslandSettingsRoot(store: info, tab: .agents, rendering: true)))
        // Token and permission states the renders show by name.
        out.append(State(name: "default-grokBot-notoken", view: IslandSettingsRoot(store: store(), tab: .agents, rendering: true)))
        let tk = store(); tk.set(true, for: IslandSettingsSchema.tokensExpanded); try? tk.rotateToken(.grokBot)
        out.append(State(name: "default-grokBot-token", view: IslandSettingsRoot(store: tk, tab: .agents, rendering: true)))
        out.append(State(name: "calendar-granted", view: IslandSettingsRoot(store: store(), tab: .general, rendering: true, permissions: Self.granted)))
        let live = store(); live.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        out.append(State(name: "live-samantha", view: IslandSettingsRoot(store: live, tab: .samantha, rendering: true)))
        let liveW = store(); liveW.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        out.append(State(name: "worst-live-samantha", view: IslandSettingsRoot(store: liveW, tab: .samantha, rendering: true,
                                                                              permissions: Self.missing, defs: Self.worstDefs)))
        // Computer Use: the onboarding rows + amber summary (mixed) / no summary (all granted).
        out.append(State(name: "mixed-computerUse", view: IslandSettingsRoot(store: store(), tab: .computerUse, rendering: true,
                                                                            permissions: OnboardingTests.mixed)))
        out.append(State(name: "granted-computerUse", view: IslandSettingsRoot(store: store(), tab: .computerUse, rendering: true,
                                                                              permissions: Self.granted)))
        out.append(State(name: "calendar-missing", view: IslandSettingsRoot(store: store(), tab: .general, rendering: true, permissions: Self.missing)))
        let focus: [(String, SettingsTab, String)] = [
            ("toggle", .general, IslandSettingsSchema.showIsland),
            ("popup", .general, IslandSettingsSchema.displayOn),
            ("tiles", .general, IslandSettingsSchema.idleActivity),
            ("slider", .agents, IslandSettingsSchema.alertDuration),
            ("master", .general, IslandSettingsSchema.soundEnabled),
            ("permission", .general, IslandSettingsSchema.calendarAccess),
            ("button", .about, "island.about.openLog"),
            ("voice-popup", .samantha, IslandSettingsSchema.voiceModel),
            ("disclosure", .general, IslandSettingsSchema.moreExpanded),
            ("source", .agents, AgentSource.cursor.keyPrefix + "cap"),
            ("cua-permission", .computerUse, IslandSettingsSchema.cuaScreenRecording),
        ]
        for (n, t, k) in focus {
            let fs = store(); fs.set(true, for: IslandSettingsSchema.moreExpanded)
            if n == "live-popup" { fs.set("gpt-live-1", for: IslandSettingsSchema.voiceModel) }  // rows show only for GPT-Live-1
            out.append(State(name: "focus-\(n)", view: IslandSettingsRoot(store: fs, nav: SettingsNav(tab: t, focus: k), rendering: true,
                                                                         permissions: Self.missing)))
        }
        return out
    }

    private func measure(_ v: IslandSettingsRoot, scheme: ColorScheme) -> ([IslandLayoutItem], [String: IslandLayoutContainer], CGImage?) {
        IslandLayoutRecorder.shared.reset()
        let r = ImageRenderer(content: v.environment(\.colorScheme, scheme))
        r.scale = 2
        let img = r.cgImage
        let (i, c) = IslandLayoutRecorder.shared.snapshot()
        return (i, c, img)
    }

    func testSettingsLayoutGateEveryPaneAndState() throws {
        let states = states()
        var issues: [String] = [], checked: [String] = []
        for st in states {
            for (tag, scheme) in [("dark", ColorScheme.dark)] {   // the window is dark-only
                let (items, containers, _) = measure(st.view, scheme: scheme)
                let name = "settings-\(st.name)-\(tag)"
                let rows = containers.keys.filter { $0.hasPrefix("row|") }.count
                checked.append("\(name): \(items.count) probes, \(rows) rows")
                if rows == 0 && st.view.nav.tab != .about { issues.append("\(name): no rows recorded") }
                if st.name.hasPrefix("focus-"), !items.contains(where: { $0.kind == .ring }) { issues.append("\(name): no focus ring drawn") }
                if st.name == "mixed-computerUse", !items.contains(where: { $0.id == "summary" }) { issues.append("\(name): no Required summary") }
                if st.name == "granted-computerUse", items.contains(where: { $0.id == "summary" }) { issues.append("\(name): summary shown with nothing missing") }
                issues += IslandLayoutGate.issues(state: name, items: items, containers: containers)
                issues += IslandLayoutGate.formIssues(state: name, items: items, containers: containers,
                                                      rowHeight: SettingsStyle.rowHeight, tokenRowHeight: SettingsStyle.tokenRowHeight)
            }
        }
        if let path = ProcessInfo.processInfo.environment["CUA_SETTINGS_GATE_REPORT"] {
            try (["# Settings layout gate", "states checked: \(checked.count)", ""] + checked + ["", "issues: \(issues.count)"] + issues)
                .joined(separator: "\n").appending("\n").write(toFile: path, atomically: true, encoding: .utf8)
        }
        XCTAssertEqual(checked.count, states.count)
        XCTAssertGreaterThanOrEqual(states.count, Self.panes.count * 3 + 11)
        XCTAssertTrue(issues.isEmpty, "settings layout gate (\(issues.count)):\n" + issues.prefix(60).joined(separator: "\n"))
    }

    /// Dark-only review renders (the window is dark-only): the states the UX review asked for, plus the sidebar.
    func testRenderSettings() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        IslandLayoutRecorder.enabled = false
        let wanted: Set<String> = ["default-general", "default-agents", "toggled-agents", "worst-agents",
                                   "more-expanded", "list-only-agents", "default-grokBot-token",
                                   "default-samantha", "default-computerUse", "mixed-computerUse", "granted-computerUse",
                                   "focus-tiles", "focus-voice-popup", "focus-source", "worst-general"]
        func save(_ img: CGImage, _ name: String) throws {
            let url = URL(fileURLWithPath: dir).appendingPathComponent("settings-\(name)-dark.png")
            XCTAssertFalse(FileManager.default.fileExists(atPath: url.path), "never overwrite \(url.lastPathComponent)")
            try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: url)
        }
        var rendered = Set<String>()
        for st in states() where wanted.contains(st.name) {
            let r = ImageRenderer(content: st.view.environment(\.colorScheme, .dark))
            r.scale = 2
            guard let img = r.cgImage else { XCTFail("render \(st.name)"); continue }
            try save(img, st.name)
            rendered.insert(st.name)
            if st.name == "default-general", let side = img.cropping(to: CGRect(x: 0, y: 0, width: Int((SettingsStyle.sidebarWidth + 1) * 2), height: img.height)) {
                try save(side, "sidebar")
            }
        }
        XCTAssertEqual(rendered, wanted)
    }

    /// Item 9: General must fit one screen (~800 pt of content). Measures General with and without the
    func testGeneralFitsOneScreen() {
        let s = store()
        let collapsed = IslandSettingsRoot.visibleDefs(.general, store: s)
        XCTAssertFalse(collapsed.contains { $0.key == IslandSettingsSchema.haptics })
        let renderer = ImageRenderer(content: IslandSettingsRoot(store: s, rendering: true))
        renderer.scale = 1
        XCTAssertLessThanOrEqual(renderer.cgImage?.height ?? Int.max, Int(SettingsStyle.size.height))
        s.set(true, for: IslandSettingsSchema.moreExpanded)
        let expanded = IslandSettingsRoot.visibleDefs(.general, store: s)
        XCTAssertTrue(expanded.contains { $0.key == IslandSettingsSchema.haptics })
        XCTAssertGreaterThan(expanded.count, collapsed.count)
    }

    // MARK: Keyboard model

    func testKeyboardFocusMovesActivatesAndSteps() {
        let s = store(), nav = SettingsNav(tab: .general)
        let defs = IslandSettingsRoot.visibleDefs(.general, store: s)
        let f = SettingsKeyboard.focusable(defs)
        func press(_ k: SettingsKey) -> (Bool, String?) {
            let r = SettingsKeyboard.handle(k, nav: nav, store: s, defs: IslandSettingsRoot.visibleDefs(nav.tab, store: s), panes: Self.panes)
            return (r.handled, r.action)
        }
        XCTAssertFalse(press(.activate).0, "nothing focused: Space passes through")
        _ = press(.next); XCTAssertEqual(nav.focus, f.first?.key)
        _ = press(.previous); XCTAssertEqual(nav.focus, f.last?.key, "wraps")
        nav.focus = IslandSettingsSchema.showIsland
        let before = s.bool(IslandSettingsSchema.showIsland)
        _ = press(.activate); XCTAssertEqual(s.bool(IslandSettingsSchema.showIsland), !before)
        _ = press(.increase); XCTAssertTrue(s.bool(IslandSettingsSchema.showIsland))
        _ = press(.decrease); XCTAssertFalse(s.bool(IslandSettingsSchema.showIsland))
        nav.tab = .agents
        nav.focus = IslandSettingsSchema.alertDuration
        s.set(7.5, for: IslandSettingsSchema.alertDuration)
        _ = press(.increase); _ = press(.increase)
        XCTAssertEqual(s.double(IslandSettingsSchema.alertDuration), 8, "clamped at max")
        nav.tab = .general; s.set(true, for: IslandSettingsSchema.moreExpanded)
        nav.focus = IslandSettingsSchema.idleActivity
        s.set("agents", for: IslandSettingsSchema.idleActivity)
        _ = press(.increase); XCTAssertEqual(s.string(IslandSettingsSchema.idleActivity), "duo")
        _ = press(.activate); XCTAssertEqual(s.string(IslandSettingsSchema.idleActivity), "none", "Space cycles")
        s.screens = { [.init(uuid: "builtin", name: "Built-in", builtIn: true)] }
        nav.focus = IslandSettingsSchema.displayOn
        _ = press(.activate); XCTAssertEqual(s.string(IslandSettingsSchema.displayOn), "builtIn")
        XCTAssertTrue(press(.escape).0); XCTAssertNil(nav.focus)
        XCTAssertFalse(press(.escape).0, "Esc passes through when nothing is focused")
        _ = press(.nextPane); XCTAssertEqual(nav.tab, .agents); XCTAssertNil(nav.focus)
        nav.tab = .general; nav.focus = IslandSettingsSchema.calendarAccess
        XCTAssertEqual(press(.activate).1, IslandSettingsSchema.calendarAccess, "missing permission: Allow… runs")
        let granted = SettingsKeyboard.handle(.activate, nav: nav, store: s, defs: IslandSettingsRoot.visibleDefs(.general, store: s),
                                              panes: Self.panes, permissions: { _ in true })
        XCTAssertEqual(granted.handled, true); XCTAssertNil(granted.action, "allowed: nothing to request")
    }

    func testKeyboardSkipsTokenRowsAndMapsKeys() {
        let defs = IslandSettingsSchema.defs(for: .agents)
        XCTAssertFalse(SettingsKeyboard.focusable(defs).contains { if case .token = $0.kind { return true }; return false })
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 48, flags: []), .next)
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 48, flags: .shift), .previous)
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 48, flags: .control), .nextPane)
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 49, flags: []), .activate)
        XCTAssertNil(SettingsKeyboard.key(keyCode: 49, flags: .command), "⌘ shortcuts pass through")
    }
}
