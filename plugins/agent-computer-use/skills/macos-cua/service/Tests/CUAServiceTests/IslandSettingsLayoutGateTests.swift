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

    override func setUp() { IslandLayoutRecorder.enabled = true }
    override func tearDown() {
        IslandLayoutRecorder.enabled = false
        IslandLayoutRecorder.shared.reset()
        for s in suites { UserDefaults(suiteName: s)?.removePersistentDomain(forName: s) }
        suites = []
    }

    struct State { var name: String; var view: IslandSettingsRoot }

    private func store() -> IslandSettingsStore {
        let s = "cua.island.settings.gate.\(UUID().uuidString)"
        suites.append(s)
        return IslandSettingsStore(defaults: UserDefaults(suiteName: s)!, tokens: MemoryTokenStore())
    }

    static let panes = SettingsTab.panes

    /// Every bool flipped, last tile / popup option, sliders at max, tokens set.
    private func toggled(_ s: IslandSettingsStore) {
        for d in IslandSettingsSchema.all {
            switch d.kind {
            case .master, .toggle:
                if case .bool(let b) = d.defaultValue { s.set(!b, for: d.key) }
            case .tiles(let o): if let v = o.last?.value { s.set(v, for: d.key) }
            case .popup(let o): if let v = o.last?.value { s.set(v, for: d.key) }
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
        // Token and permission states the renders show by name.
        out.append(State(name: "default-grokBot-notoken", view: IslandSettingsRoot(store: store(), tab: .grokBot, rendering: true)))
        let tk = store(); try? tk.rotateToken(.grokBot)
        out.append(State(name: "default-grokBot-token", view: IslandSettingsRoot(store: tk, tab: .grokBot, rendering: true)))
        out.append(State(name: "calendar-granted", view: IslandSettingsRoot(store: store(), tab: .calendar, rendering: true, permissions: Self.granted)))
        let live = store(); live.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        out.append(State(name: "live-samantha", view: IslandSettingsRoot(store: live, tab: .samantha, rendering: true)))
        let liveW = store(); liveW.set("gpt-live-1", for: IslandSettingsSchema.voiceModel)
        out.append(State(name: "worst-live-samantha", view: IslandSettingsRoot(store: liveW, tab: .samantha, rendering: true,
                                                                              permissions: Self.missing, defs: Self.worstDefs)))
        out.append(State(name: "calendar-missing", view: IslandSettingsRoot(store: store(), tab: .calendar, rendering: true, permissions: Self.missing)))
        let focus: [(String, SettingsTab, String)] = [
            ("toggle", .general, IslandSettingsSchema.showIsland),
            ("popup", .general, IslandSettingsSchema.displayOn),
            ("tiles", .general, IslandSettingsSchema.idleActivity),
            ("slider", .general, IslandSettingsSchema.resultPeek),
            ("master", .sound, IslandSettingsSchema.soundEnabled),
            ("permission", .calendar, IslandSettingsSchema.calendarAccess),
            ("button", .about, "island.about.openLog"),
            ("voice-popup", .samantha, IslandSettingsSchema.voiceModel),
            ("live-popup", .samantha, IslandSettingsSchema.voiceLiveIntelligence),
            ("cua-permission", .computerUse, IslandSettingsSchema.cuaScreenRecording),
        ]
        for (n, t, k) in focus {
            let fs = store()
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
        let wanted: Set<String> = ["default-general", "default-interaction", "default-grokBot-notoken", "default-grokBot-token",
                                   "calendar-granted", "calendar-missing", "default-fileTray", "worst-samantha",
                                   "default-samantha", "default-computerUse", "toggled-computerUse", "focus-voice-popup",
                                   "live-samantha", "focus-live-popup"]
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
    /// Behaviour + Gestures sections (now the Interaction pane).
    func testGeneralFitsOneScreen() throws {
        IslandLayoutRecorder.enabled = false
        func height(_ defs: @escaping (SettingsTab) -> [SettingDef]) -> CGFloat {
            let r = ImageRenderer(content: IslandSettingsRoot(store: store(), tab: .general, rendering: true, defs: defs))
            r.scale = 1
            r.proposedSize = ProposedViewSize(width: SettingsStyle.size.width, height: nil)
            return CGFloat(r.cgImage?.height ?? 0)
        }
        func content(_ defs: @escaping (SettingsTab) -> [SettingDef]) -> CGFloat {
            // Window height is max(content, 687); measure content alone by stretching the defs list.
            height(defs)
        }
        let now = content(IslandSettingsSchema.defs(for:))
        let merged = content { t in t == .general
            ? IslandSettingsSchema.general + IslandSettingsSchema.interaction.map { var d = $0; d.tab = .general; return d }
            : IslandSettingsSchema.defs(for: t) }
        if let path = ProcessInfo.processInfo.environment["CUA_SETTINGS_GATE_REPORT"] {
            try "General height: \(Int(merged)) pt with Behaviour + Gestures, \(Int(now)) pt after moving them to Interaction\n"
                .write(toFile: path + ".general-height.txt", atomically: true, encoding: .utf8)
        }
        XCTAssertGreaterThan(merged, 800, "the split is warranted")
        XCTAssertLessThanOrEqual(now, 800, "General fits one screen")
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
        nav.focus = IslandSettingsSchema.resultPeek
        s.set(7.5, for: IslandSettingsSchema.resultPeek)
        _ = press(.increase); _ = press(.increase)
        XCTAssertEqual(s.double(IslandSettingsSchema.resultPeek), 8, "clamped at max")
        nav.focus = IslandSettingsSchema.idleActivity
        s.set("agents", for: IslandSettingsSchema.idleActivity)
        _ = press(.increase); XCTAssertEqual(s.string(IslandSettingsSchema.idleActivity), "duo")
        _ = press(.activate); XCTAssertEqual(s.string(IslandSettingsSchema.idleActivity), "none", "Space cycles")
        nav.focus = IslandSettingsSchema.displayOn
        _ = press(.activate); XCTAssertEqual(s.string(IslandSettingsSchema.displayOn), "builtIn")
        XCTAssertTrue(press(.escape).0); XCTAssertNil(nav.focus)
        XCTAssertFalse(press(.escape).0, "Esc passes through when nothing is focused")
        _ = press(.nextPane); XCTAssertEqual(nav.tab, .interaction); XCTAssertNil(nav.focus)
        nav.tab = .calendar; nav.focus = IslandSettingsSchema.calendarAccess
        XCTAssertEqual(press(.activate).1, IslandSettingsSchema.calendarAccess, "missing permission: Allow… runs")
        let granted = SettingsKeyboard.handle(.activate, nav: nav, store: s, defs: IslandSettingsRoot.visibleDefs(.calendar, store: s),
                                              panes: Self.panes, permissions: { _ in true })
        XCTAssertEqual(granted.handled, true); XCTAssertNil(granted.action, "allowed: nothing to request")
    }

    func testKeyboardSkipsTokenRowsAndMapsKeys() {
        let defs = IslandSettingsSchema.defs(for: .grokBot)
        XCTAssertFalse(SettingsKeyboard.focusable(defs).contains { if case .token = $0.kind { return true }; return false })
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 48, flags: []), .next)
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 48, flags: .shift), .previous)
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 48, flags: .control), .nextPane)
        XCTAssertEqual(SettingsKeyboard.key(keyCode: 49, flags: []), .activate)
        XCTAssertNil(SettingsKeyboard.key(keyCode: 49, flags: .command), "⌘ shortcuts pass through")
    }
}
