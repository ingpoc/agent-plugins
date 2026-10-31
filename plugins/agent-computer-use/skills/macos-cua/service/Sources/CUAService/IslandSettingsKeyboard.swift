import AppKit

/// Settings window keyboard model (no reliance on the system's Full Keyboard Access setting):
/// Tab / ⇧Tab and ↓ / ↑ move focus through the pane's rows, Space or Return activates (toggle, next tile or
/// popup option, run a button), ← / → step sliders, tiles and popups (or set a switch off / on),
/// ⌃Tab / ⌃⇧Tab switch panes, Esc clears focus. Token rows are mouse-only (Copy / Rotate stay deliberate).
enum SettingsKey: Equatable { case next, previous, activate, decrease, increase, escape, nextPane, previousPane }

@MainActor
enum SettingsKeyboard {
    static func key(keyCode: UInt16, flags: NSEvent.ModifierFlags) -> SettingsKey? {
        if IslandShortcutRecorder.recording || flags.contains(.command) || flags.contains(.option) { return nil }
        let shift = flags.contains(.shift), ctrl = flags.contains(.control)
        switch keyCode {
        case 48: return ctrl ? (shift ? .previousPane : .nextPane) : (shift ? .previous : .next)
        case 125: return .next
        case 126: return .previous
        case 49, 36, 76: return .activate
        case 123: return .decrease
        case 124: return .increase
        case 53: return .escape
        default: return nil
        }
    }

    static func focusable(_ defs: [SettingDef]) -> [SettingDef] {
        defs.flatMap { d -> [SettingDef] in
            if case .sourceTable = d.kind {
                return IslandSettingsSchema.tableSources.flatMap { IslandSettingsSchema.agent($0) }
            }
            if case .token = d.kind { return [] }
            if case .shortcut = d.kind { return [] }
            return [d]
        }
    }

    /// Applies one key. `handled` = consume the event; `action` = button row key whose action should run.
    @discardableResult
    static func handle(_ k: SettingsKey, nav: SettingsNav, store: IslandSettingsStore, defs: [SettingDef],
                       panes: [SettingsTab], permissions: (String) -> Bool = { _ in false }) -> (handled: Bool, action: String?) {
        let f = focusable(defs)
        let i = nav.focus.flatMap { key in f.firstIndex { $0.key == key } }
        switch k {
        case .next, .previous:
            guard !f.isEmpty else { return (false, nil) }
            let n = f.count
            let j = i.map { (k == .next ? $0 + 1 : $0 - 1 + n) % n } ?? (k == .next ? 0 : n - 1)
            nav.focus = f[j].key
            return (true, nil)
        case .nextPane, .previousPane:
            guard let p = panes.firstIndex(of: nav.tab) else { return (false, nil) }
            nav.tab = panes[(p + (k == .nextPane ? 1 : panes.count - 1)) % panes.count]
            return (true, nil)
        case .escape:
            guard nav.focus != nil else { return (false, nil) }
            nav.focus = nil
            return (true, nil)
        case .activate, .decrease, .increase:
            guard let i else { return (false, nil) }
            let d = f[i]
            guard store.sourceControlEnabled(d.key) else { return (true, nil) }
            switch d.kind {
            case .master, .toggle, .disclosure:
                store.set(k == .activate ? !store.bool(d.key) : k == .increase, for: d.key)
            case .slider(let lo, let hi, let step, _):
                guard k != .activate else { return (true, nil) }
                let v = store.double(d.key) + (k == .increase ? step : -step)
                store.set(min(hi, max(lo, (v / step).rounded() * step)), for: d.key)
            case .tiles(let o):
                store.set(cycle(o.map(\.value), store.string(d.key), k), for: d.key)
            case .popup(let o):
                let options = d.dynamicOptions?(store) ?? o
                let values = options.map(\.value).filter { d.key != AgentSource.samantha.keyPrefix + "cap" || $0 != "info" }
                let current = d.key == IslandSettingsSchema.displayOn ? store.displaySelection() : store.string(d.key)
                store.set(cycle(values, current, k), for: d.key)
            case .button:
                return (true, k == .activate ? d.key : nil)
            case .permission:
                // Already allowed: nothing to ask for (the row shows "Allowed", not a button).
                return (true, k == .activate && !permissions(d.key) ? d.key : nil)
            case .token, .sourceTable, .shortcut:
                return (false, nil)
            }
            return (true, nil)
        }
    }

    static func cycle(_ values: [String], _ current: String, _ k: SettingsKey) -> String {
        // A stored value the popup doesn't list (e.g. a model set by a newer runtime) is never
        // overwritten from the keyboard; the mouse popup can still pick a listed value.
        guard let i = values.firstIndex(of: current) else { return current }
        switch k {
        case .activate: return values[(i + 1) % values.count]
        case .increase: return values[min(values.count - 1, i + 1)]
        default: return values[max(0, i - 1)]
        }
    }
}

import Carbon
import SwiftUI

struct IslandShortcutRecorder: NSViewRepresentable {
    @ObservedObject var store: IslandSettingsStore
    @MainActor static var recording = false

    func makeNSView(context: Context) -> RecorderButton {
        let button = RecorderButton()
        button.store = store
        button.target = button
        button.action = #selector(RecorderButton.begin)
        button.bezelStyle = .rounded
        return button
    }
    func updateNSView(_ button: RecorderButton, context: Context) {
        button.store = store
        if !Self.recording { button.title = IslandShortcut.read(store).display }
    }

    final class RecorderButton: NSButton {
        weak var store: IslandSettingsStore?
        override var acceptsFirstResponder: Bool { true }
        override func viewWillMove(toWindow newWindow: NSWindow?) {
            if newWindow == nil { IslandShortcutRecorder.recording = false }
            super.viewWillMove(toWindow: newWindow)
        }
        @objc func begin() {
            IslandShortcutRecorder.recording = true
            title = "Type shortcut"
            window?.makeFirstResponder(self)
        }
        override func resignFirstResponder() -> Bool {
            IslandShortcutRecorder.recording = false
            if let store { title = IslandShortcut.read(store).display }
            return super.resignFirstResponder()
        }
        override func keyDown(with event: NSEvent) {
            guard IslandShortcutRecorder.recording else { super.keyDown(with: event); return }
            let flags = event.modifierFlags
            var mods: UInt32 = 0
            if flags.contains(.command) { mods |= UInt32(cmdKey) }
            if flags.contains(.control) { mods |= UInt32(controlKey) }
            if flags.contains(.option) { mods |= UInt32(optionKey) }
            if flags.contains(.shift) { mods |= UInt32(shiftKey) }
            guard mods != 0, let text = event.charactersIgnoringModifiers, text.count == 1,
                  let scalar = text.unicodeScalars.first, !CharacterSet.controlCharacters.contains(scalar),
                  let store else { NSSound.beep(); return }
            IslandShortcutRecorder.recording = false
            IslandShortcut(keyCode: UInt32(event.keyCode), modifiers: mods, character: text.lowercased()).save(store)
            title = IslandShortcut.read(store).display
            window?.makeFirstResponder(nil)
        }
    }
}
