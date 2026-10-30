import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// The menu-bar menu: one "ACU Settings…" (⌘,) entry point, no Voice/Island Settings leftovers.
@MainActor
final class StatusMenuTests: XCTestCase {
    func testMenuHasOneACUSettingsItemWithCommandComma() {
        let menu = StatusBarController().makeMenu()
        let titles = menu.items.map(\.title)
        let settings = menu.items.filter { $0.title == "ACU Settings…" }
        XCTAssertEqual(settings.count, 1)
        XCTAssertEqual(settings.first?.keyEquivalent, ",")
        XCTAssertEqual(settings.first?.keyEquivalentModifierMask, [.command])
        XCTAssertFalse(titles.contains { $0.localizedCaseInsensitiveContains("voice settings") }, "\(titles)")
        XCTAssertFalse(titles.contains { $0.localizedCaseInsensitiveContains("island settings") }, "\(titles)")
        XCTAssertEqual(titles.filter { $0.hasSuffix("Settings…") }.count, 1)
        XCTAssertEqual(IslandSettingsWindowController.shared.window?.title, "ACU Settings")
    }

    func testIslandMenuItemUsesCurrentShortcut() {
        let menu = StatusBarController().makeMenu()
        let shortcut = IslandShortcut.read(IslandSettingsStore.shared)
        let item = menu.items.first { $0.title == "Show Dynamic Island" }
        XCTAssertNotNil(item?.action)
        XCTAssertEqual(item?.keyEquivalent, shortcut.character.lowercased())
        XCTAssertEqual(item?.keyEquivalentModifierMask, shortcut.menuModifiers)
        XCTAssertNotNil(menu.items.first { $0.title == "Show Island in Menu Bar" })
    }

    func testOpenSettingsPaneNames() {
        XCTAssertEqual(SettingsTab.forPane("computer-use"), .some(.computerUse))
        XCTAssertEqual(SettingsTab.forPane(" Computer_Use "), .some(.computerUse))
        XCTAssertEqual(SettingsTab.forPane("samantha"), .some(.samantha))
        XCTAssertEqual(SettingsTab.forPane(""), .some(nil), "no pane: keep the last pane")
        XCTAssertNil(SettingsTab.forPane("voice-settings"), "unknown pane → -32602")
    }

    func testKeyboardNeverOverwritesAnUnlistedValue() {
        XCTAssertEqual(SettingsKeyboard.cycle(["a", "b"], "z", .activate), "z")
        XCTAssertEqual(SettingsKeyboard.cycle(["a", "b"], "a", .activate), "b")
    }

    /// 2x review crop of the menu. NSMenu can't draw offscreen, so this draws the real menu's items
    /// (titles, check state, shortcuts, the Samantha switch row) in the dark menu style.
    func testRenderMenu() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        let menu = StatusBarController().makeMenu()
        let rows: [MenuRow] = menu.items.map { item in
            if item.isSeparatorItem { return MenuRow(kind: .separator, title: "", key: "", on: false) }
            if let v = item.view as? MenuSwitchRowView { return MenuRow(kind: .toggle, title: v.label.stringValue, key: "", on: v.toggle.state == .on) }
            let key = item.keyEquivalent.isEmpty ? "" : (item.keyEquivalentModifierMask.contains(.command) ? "⌘" : "") + item.keyEquivalent.uppercased()
            return MenuRow(kind: item.isEnabled && item.action != nil ? .item : .header, title: item.title, key: key, on: item.state == .on)
        }
        let r = ImageRenderer(content: MenuMock(rows: rows).environment(\.colorScheme, .dark))
        r.scale = 2
        guard let img = r.cgImage else { return XCTFail("render menu") }
        let url = URL(fileURLWithPath: dir).appendingPathComponent("menu-acu-settings-v2-dark.png")
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path), "never overwrite")
        try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: url)
    }
}

private struct MenuRow: Identifiable {
    enum Kind { case header, toggle, item, separator }
    let id = UUID()
    let kind: Kind, title: String, key: String, on: Bool
}

private struct MenuMock: View {
    let rows: [MenuRow]
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(rows) { row in
                switch row.kind {
                case .separator:
                    Divider().overlay(Color.white.opacity(0.12)).padding(.horizontal, 10).padding(.vertical, 5)
                case .toggle:
                    HStack { Text(row.title); Spacer(); // NSSwitch-backed Toggles don't draw offscreen; same geometry drawn by hand.
                        Capsule().fill(row.on ? Color.accentColor : Color.white.opacity(0.22)).frame(width: 32, height: 18)
                            .overlay(Circle().fill(Color.white).padding(2), alignment: row.on ? .trailing : .leading) }
                        .padding(.leading, 18).padding(.trailing, 14).frame(height: 28)
                default:
                    HStack(spacing: 0) {
                        Text(row.on ? "✓" : "").frame(width: 18, alignment: .center)
                        Text(row.title).foregroundStyle(row.kind == .header ? Color.secondary : Color.primary)
                        Spacer(minLength: 24)
                        Text(row.key).foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 6).frame(height: 22)
                }
            }
        }
        .font(.system(size: NSFont.systemFontSize))
        .padding(.vertical, 5)
        .frame(width: 240)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(white: 0.17)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.15)))
        .padding(8)
        .background(Color.black)
    }
}
