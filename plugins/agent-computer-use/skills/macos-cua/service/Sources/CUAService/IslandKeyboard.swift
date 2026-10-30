import AppKit
import Carbon.HIToolbox
import SwiftUI

extension View {
    /// Keyboard focus ring (⌃⌘I navigation): 2 pt accent ring, offset 2 pt outside the target, following
    /// its shape (capsule for buttons, the tile's radius + offset for tiles). Overlay: no layout change.
    func islandFocusRing(_ on: Bool, shape: IslandFocusShape) -> some View {
        overlay(Group {
            switch shape {
            case .capsule: Capsule(style: .continuous).strokeBorder(IslandPalette.accent, lineWidth: 2)
            case .rounded(let r):
                RoundedRectangle(cornerRadius: r + IslandFocusShape.outset, style: .continuous).strokeBorder(IslandPalette.accent, lineWidth: 2)
            }
        }
        .padding(-IslandFocusShape.outset)
        .modifier(RingProbe(on: on))
        .opacity(on ? 1 : 0)
        .allowsHitTesting(false))
    }
}

enum IslandFocusShape: Equatable {
    case capsule
    case rounded(CGFloat)
    /// 2 pt gap + 2 pt ring.
    static let outset: CGFloat = 4
    static let tile = IslandFocusShape.rounded(7)
}

struct IslandShortcut: Equatable {
    let keyCode: UInt32
    let modifiers: UInt32
    let character: String
    static let `default` = IslandShortcut(keyCode: UInt32(kVK_ANSI_I), modifiers: UInt32(controlKey | cmdKey), character: "i")
    @MainActor static func read(_ store: IslandSettingsStore) -> IslandShortcut {
        guard let d = store.defaults.dictionary(forKey: IslandSettingsSchema.shortcut),
              let key = d["keyCode"] as? Int, let mods = d["modifiers"] as? Int,
              let character = d["character"] as? String, character.count == 1,
              key >= 0, key < 128, mods > 0, mods <= Int(UInt32.max),
              UInt32(mods) & UInt32(controlKey | optionKey | shiftKey | cmdKey) != 0 else { return .default }
        return IslandShortcut(keyCode: UInt32(key), modifiers: UInt32(mods), character: character)
    }
    var display: String {
        (modifiers & UInt32(controlKey) != 0 ? "⌃" : "") + (modifiers & UInt32(optionKey) != 0 ? "⌥" : "")
        + (modifiers & UInt32(shiftKey) != 0 ? "⇧" : "") + (modifiers & UInt32(cmdKey) != 0 ? "⌘" : "") + character.uppercased()
    }
    var spoken: String {
        ([modifiers & UInt32(controlKey) != 0 ? "Control" : nil,
          modifiers & UInt32(optionKey) != 0 ? "Option" : nil,
          modifiers & UInt32(shiftKey) != 0 ? "Shift" : nil,
          modifiers & UInt32(cmdKey) != 0 ? "Command" : nil, character.uppercased()].compactMap { $0 }).joined(separator: "-")
    }
    var menuModifiers: NSEvent.ModifierFlags {
        var f: NSEvent.ModifierFlags = []
        if modifiers & UInt32(controlKey) != 0 { f.insert(.control) }
        if modifiers & UInt32(optionKey) != 0 { f.insert(.option) }
        if modifiers & UInt32(shiftKey) != 0 { f.insert(.shift) }
        if modifiers & UInt32(cmdKey) != 0 { f.insert(.command) }
        return f
    }
    @MainActor func save(_ store: IslandSettingsStore) {
        store.set(["keyCode": Int(keyCode), "modifiers": Int(modifiers), "character": character], for: IslandSettingsSchema.shortcut)
    }
}

// MARK: - Global hotkey (⌃⌘I) — public Carbon API, no Accessibility needed

final class IslandHotKey {
    static let keyCode = UInt32(kVK_ANSI_I)
    static let modifiers = UInt32(controlKey | cmdKey)
    static let display = "⌃⌘I"
    var shortcut = IslandShortcut.default
    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    var onPress: (() -> Void)?

    @discardableResult
    func register() -> Bool {
        guard ref == nil else { return true }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let me = Unmanaged.passUnretained(self).toOpaque()
        let hs = InstallEventHandler(GetApplicationEventTarget(), { _, _, ud in
            guard let ud else { return noErr }
            let hk = Unmanaged<IslandHotKey>.fromOpaque(ud).takeUnretainedValue()
            DispatchQueue.main.async { hk.onPress?() }
            return noErr
        }, 1, &spec, me, &handler)
        guard hs == noErr else { unregister(); return false }
        let id = EventHotKeyID(signature: OSType(0x4953_4C44), id: 1)   // 'ISLD'
        let st = RegisterEventHotKey(shortcut.keyCode, shortcut.modifiers, id, GetApplicationEventTarget(), 0, &ref)
        if st != noErr { unregister() }
        return st == noErr
    }

    func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        if let handler { RemoveEventHandler(handler) }
        handler = nil
    }
    deinit { unregister() }
}

// MARK: - Keyboard navigation (arrows / Tab, Return, Esc) + VoiceOver announcements

enum IslandNavTarget: Equatable {
    case open                 // compact: Return opens the card
    case secondary            // compact Duo: swap
    case row(String)          // agent list row control
    case cancelStop(String)   // keep the agent running after arming Stop
    case action(IslandCardAction)
}

extension IslandModel {
    var navTargets: [IslandNavTarget] {
        if !cardOpen { return secondary == nil ? [.open] : [.open, .secondary] }
        if cardMode == .agents {
            return IslandAgentRows.hierarchical(listRows).flatMap { item -> [IslandNavTarget] in
                guard IslandAgentRows.control(for: item.agent) != nil else { return [] }
                return isStopArmed(item.id) ? [.row(item.id), .cancelStop(item.id)] : [.row(item.id)]
            }
        }
        switch card.controls {
        case .decision(let s, let p, _): return [.action(s.1), .action(p.1)]
        case .icons(let items): return items.map { .action($0.action) }
        default: return []
        }
    }

    var navTarget: IslandNavTarget? {
        let t = navTargets
        guard keyboardActive, !t.isEmpty else { return nil }
        return t[min(max(0, navIndex), t.count - 1)]
    }

    func isNavFocused(_ t: IslandNavTarget) -> Bool { navTarget == t }

    /// VoiceOver label for a target (also what is announced as focus moves).
    func navLabel(_ t: IslandNavTarget) -> String {
        switch t {
        case .open: return "Open \(presentation.spokenName)"
        case .secondary: return "Swap to \(secondary?.rawValue ?? "other activity")"
        case .row(let id):
            guard let item = IslandAgentRows.hierarchical(listRows).first(where: { $0.id == id }),
                  let c = IslandAgentRows.control(for: item.agent) else { return "" }
            let a = item.agent
            let who = a.name.isEmpty ? a.app : a.name
            let parent = item.parentName.map { ", child of \($0)" } ?? ""
            let state = IslandAgentRows.statusText(a)
            let failure = rowFailures[id].map { ", \($0)" } ?? ""
            switch c {
            case .stop: return "\(isStopArmed(id) ? "Confirm stop" : "Stop") \(who): \(a.title), \(state)\(parent)\(failure)"
            case .retry: return "Retry \(who): \(a.title), \(state)\(parent)\(failure)"
            case .review:
                let kind = a.request_kind == "choice" ? "choice" : "approval"
                let destructive = a.destructive ? ", destructive request" : ""
                return "Review \(kind) from \(who): \(a.title), \(state)\(destructive)\(parent)"
            case .dismiss: return "Dismiss stale agent \(who): \(state)\(parent)"
            }
        case .cancelStop(let id):
            let item = IslandAgentRows.hierarchical(listRows).first { $0.id == id }
            let who = item.map { $0.agent.name.isEmpty ? $0.agent.app : $0.agent.name } ?? "agent"
            return "Keep \(who) running"
        case .action(let a):
            if case .decision(let s, let p, _) = card.controls {
                if a == s.1 { return s.0 }
                if a == p.1 { return p.0 }
            }
            if case .icons(let items) = card.controls, let i = items.first(where: { $0.action == a }) { return i.label }
            return ""
        }
    }

    /// ⌃⌘I: open the island with keyboard focus; pressed again (or Esc) closes and hands focus back.
    func hotKeyPressed() {
        if keyboardActive { endKeyboard(close: true); return }
        if !cardOpen {
            forceCompact = false
            if !cardOpen { toggleCard(byClick: false) }
        }
        keyboardActive = true
        let t = navTargets
        // Irreversible requests start on the safer secondary action.
        if t.count == 2, cardOpen, case .decision(_, _, let destructive) = card.controls {
            navIndex = destructive ? 0 : 1
        } else {
            navIndex = 0
        }
        if let begin = onBeginKeyboard, !begin() { keyboardActive = false; return }
        announceFocus()
    }

    func endKeyboard(close: Bool) {
        guard keyboardActive else { return }
        keyboardActive = false
        if close, cardOpen {
            if isSticky || isChoiceCard {
                forceCompact = true
            } else {
                toggleCard(byClick: false)
            }
        }
        onEndKeyboard?()
    }

    /// Arrow / Tab / Return / Esc while the island holds keyboard focus. True = consumed.
    func handleNavKey(keyCode: UInt16, shift: Bool) -> Bool {
        guard keyboardActive else { return false }
        let t = navTargets
        switch Int(keyCode) {
        case kVK_Escape:
            endKeyboard(close: true)
            return true
        case kVK_LeftArrow, kVK_UpArrow:
            guard !t.isEmpty else { return true }
            navIndex = (min(navIndex, t.count - 1) - 1 + t.count) % t.count
            announceFocus()
            return true
        case kVK_RightArrow, kVK_DownArrow, kVK_Tab:
            guard !t.isEmpty else { return true }
            navIndex = (min(navIndex, t.count - 1) + (shift && Int(keyCode) == kVK_Tab ? -1 : 1) + t.count) % t.count
            announceFocus()
            return true
        case kVK_Return, kVK_ANSI_KeypadEnter, kVK_Space:
            guard let target = navTarget else { return true }
            activate(target)
            return true
        default:
            return false
        }
    }

    private func activate(_ t: IslandNavTarget) {
        switch t {
        case .open:
            toggleCard(byClick: false)
            navIndex = 0
        case .secondary:
            tapSecondary()
        case .row(let id):
            if let a = listRows.first(where: { $0.id == id }) { tapRowControl(a) }
        case .cancelStop(let id):
            cancelStop(id)
        case .action(let a):
            performCardAction(a)
        }
        announceFocus()
    }

    /// Card buttons (click and Return share this path).
    func performCardAction(_ action: IslandCardAction) {
        guard prepareCardAction(action) else { return }
        switch action {
        case .allow: resolveConfirm?(true)
        case .deny: resolveConfirm?(false)
        default: break
        }
        onAction?(action)
    }

    func announceFocus() {
        guard let t = navTarget else { return }
        let label = navLabel(t)
        if !label.isEmpty { onAnnounce?(label) }
    }
}

private struct RingProbe: ViewModifier {
    let on: Bool
    func body(content: Content) -> some View {
        if on { content.layoutProbe("focus-ring", .ring) } else { content }
    }
}
