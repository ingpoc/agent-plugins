import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Layout gate for the Island Settings window: every pane in default, toggled and worst-case (long labels,
/// max slider values) states plus keyboard focus on each control kind, light and dark. Fails on overlap,
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

    static let panes = SettingsTab.sidebar(hasBattery: true).flatMap(\.1)

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
            default: break
            }
            return d
        }
    }

    func states() -> [State] {
        var out: [State] = []
        for t in Self.panes {
            out.append(State(name: "default-\(t.rawValue)", view: IslandSettingsRoot(store: store(), tab: t, rendering: true, hasBattery: true)))
            let s = store(); toggled(s)
            out.append(State(name: "toggled-\(t.rawValue)", view: IslandSettingsRoot(store: s, tab: t, rendering: true, hasBattery: true)))
            let w = store(); toggled(w)
            out.append(State(name: "worst-\(t.rawValue)", view: IslandSettingsRoot(store: w, tab: t, rendering: true, hasBattery: true, defs: Self.worstDefs)))
        }
        let focus: [(String, SettingsTab, String)] = [
            ("toggle", .general, IslandSettingsSchema.showIsland),
            ("popup", .general, IslandSettingsSchema.displayOn),
            ("tiles", .general, IslandSettingsSchema.idleActivity),
            ("slider", .general, IslandSettingsSchema.resultPeek),
            ("master", .sound, IslandSettingsSchema.soundEnabled),
            ("button", .calendar, IslandSettingsSchema.calendarAccess),
        ]
        for (n, t, k) in focus {
            out.append(State(name: "focus-\(n)", view: IslandSettingsRoot(store: store(), nav: SettingsNav(tab: t, focus: k), rendering: true, hasBattery: true)))
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
            for (tag, scheme) in [("light", ColorScheme.light), ("dark", ColorScheme.dark)] {
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
        XCTAssertEqual(checked.count, states.count * 2)
        XCTAssertGreaterThanOrEqual(states.count, Self.panes.count * 3 + 6)
        XCTAssertTrue(issues.isEmpty, "settings layout gate (\(issues.count)):\n" + issues.prefix(60).joined(separator: "\n"))
    }

    func testRenderSettings() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        IslandLayoutRecorder.enabled = false
        for st in states() {
            for (tag, scheme) in [("light", ColorScheme.light), ("dark", ColorScheme.dark)] {
                let r = ImageRenderer(content: st.view.environment(\.colorScheme, scheme))
                r.scale = 2
                guard let img = r.cgImage else { XCTFail("render \(st.name)"); continue }
                let url = URL(fileURLWithPath: dir).appendingPathComponent("settings-\(st.name)-\(tag).png")
                XCTAssertFalse(FileManager.default.fileExists(atPath: url.path), "never overwrite \(url.lastPathComponent)")
                try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: url)
            }
        }
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
        _ = press(.activate); XCTAssertEqual(s.string(IslandSettingsSchema.displayOn), "all")
        XCTAssertTrue(press(.escape).0); XCTAssertNil(nav.focus)
        XCTAssertFalse(press(.escape).0, "Esc passes through when nothing is focused")
        _ = press(.nextPane); XCTAssertEqual(nav.tab, Self.panes[1]); XCTAssertNil(nav.focus)
        nav.tab = .calendar; nav.focus = IslandSettingsSchema.calendarAccess
        XCTAssertEqual(press(.activate).1, IslandSettingsSchema.calendarAccess, "button rows run their action")
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
