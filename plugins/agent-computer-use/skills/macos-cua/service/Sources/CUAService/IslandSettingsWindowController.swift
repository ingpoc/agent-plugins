import AppKit
import SwiftUI

/// Island Settings (Alcove replica). Opened only from a user action (status menu ⌘, or the
/// island's context menu) — the only time it activates the app.
@MainActor
final class IslandSettingsWindowController: NSWindowController {
    static let shared = IslandSettingsWindowController()

    var onVoiceSettings: () -> Void = {}
    let nav = SettingsNav()
    private var keyMonitor: Any?

    private init() {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: SettingsStyle.size),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
            backing: .buffered,
            defer: true
        )
        window.title = "Island Settings"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.19, green: 0.19, blue: 0.19, alpha: 1)
        super.init(window: window)
        window.contentView = NSHostingView(rootView: IslandSettingsRoot(
            store: .shared,
            nav: nav,
            onVoiceSettings: { [weak self] in self?.onVoiceSettings() },
            onShowLog: { IslandSettingsWindowController.revealLog() }
        ))
        window.setContentSize(SettingsStyle.size)
        // Keyboard model for this window only (SettingsKeyboard); other windows' keys pass through.
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            let code = e.keyCode, flags = e.modifierFlags, windowNumber = e.windowNumber
            let consume = MainActor.assumeIsolated { () -> Bool in
                guard let self, let w = self.window, windowNumber == w.windowNumber,
                      let k = SettingsKeyboard.key(keyCode: code, flags: flags) else { return false }
                let store = IslandSettingsStore.shared
                let r = SettingsKeyboard.handle(k, nav: self.nav, store: store,
                                                defs: IslandSettingsRoot.visibleDefs(self.nav.tab, store: store),
                                                panes: SettingsTab.sidebar(hasBattery: IslandSettingsSchema.hasInternalBattery).flatMap(\.1))
                if let key = r.action {
                    IslandSettingsRoot.perform(key, onVoiceSettings: { [weak self] in self?.onVoiceSettings() },
                                               onShowLog: { IslandSettingsWindowController.revealLog() })
                }
                return r.handled
            }
            return consume ? nil : e
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// User-initiated only.
    func showSettings() {
        if window?.isVisible != true { window?.center() }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    static func revealLog() {
        let cache = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/macos-cua", isDirectory: true)
        let log = cache.appendingPathComponent("samantha-activity.jsonl")
        NSWorkspace.shared.activateFileViewerSelecting([FileManager.default.fileExists(atPath: log.path) ? log : cache])
    }
}
