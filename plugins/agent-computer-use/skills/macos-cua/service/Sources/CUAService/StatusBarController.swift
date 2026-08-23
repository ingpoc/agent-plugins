import AppKit
import Foundation

/// Wi‑Fi-style menu row: label + trailing `NSSwitch`.
@MainActor
final class MenuSwitchRowView: NSView {
    let label = NSTextField(labelWithString: "Samantha")
    let toggle = NSSwitch()

    init(width: CGFloat = 220) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 28))
        label.font = NSFont.menuFont(ofSize: NSFont.systemFontSize)
        label.translatesAutoresizingMaskIntoConstraints = false
        toggle.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        addSubview(toggle)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 18),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            toggle.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            toggle.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}

/// White menubar glyph for Agent Computer Use (not a template — stays white
/// like neighboring status items on accent/colored menu bars).
@MainActor
final class StatusBarController: NSObject {
    private var item: NSStatusItem?
    private var active = false
    private var voiceRunning = false
    private var suppressToggleAction = false

    private weak var voiceSupervisor: VoiceSupervisor?
    private weak var islandController: IslandController?

    private var samanthaRow: MenuSwitchRowView?

    func wire(voice: VoiceSupervisor, island: IslandController) {
        voiceSupervisor = voice
        islandController = island
        voice.onRunningChanged = { [weak self] running in
            self?.setVoiceRunning(running)
            if running {
                island.startStreaming()
            } else {
                island.stopStreaming()
            }
        }
    }

    func install() {
        guard item == nil else { return }
        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = status.button {
            button.image = Self.loadIcon()
            button.image?.isTemplate = false
            button.toolTip = "Agent Computer Use"
            button.imagePosition = .imageOnly
        }
        let menu = NSMenu(title: "Agent Computer Use")
        let header = NSMenuItem(
            title: "Agent Computer Use",
            action: nil,
            keyEquivalent: ""
        )
        header.isEnabled = false
        menu.addItem(header)

        let row = MenuSwitchRowView()
        row.toggle.target = self
        row.toggle.action = #selector(samanthaToggled(_:))
        let samanthaItem = NSMenuItem()
        samanthaItem.view = row
        menu.addItem(samanthaItem)
        samanthaRow = row

        let settingsItem = NSMenuItem(
            title: "Samantha Settings…",
            action: #selector(openSettings),
            keyEquivalent: ","
        )
        settingsItem.keyEquivalentModifierMask = [.command]
        settingsItem.target = self
        menu.addItem(settingsItem)

        menu.addItem(NSMenuItem.separator())

        let quit = NSMenuItem(
            title: "Quit Agent Computer Use",
            action: #selector(quitApplication),
            keyEquivalent: "q"
        )
        quit.keyEquivalentModifierMask = [.command]
        quit.target = self
        menu.addItem(quit)

        status.menu = menu
        item = status
        setActive(false)
        setVoiceRunning(false)
    }

    func setActive(_ on: Bool) {
        active = on
        item?.button?.appearsDisabled = false
        item?.button?.alphaValue = 1.0
        item?.button?.toolTip = on
            ? "Agent Computer Use — active"
            : "Agent Computer Use"
    }

    private func setVoiceRunning(_ on: Bool) {
        voiceRunning = on
        suppressToggleAction = true
        samanthaRow?.toggle.state = on ? .on : .off
        samanthaRow?.toggle.isEnabled = true
        suppressToggleAction = false
        item?.button?.toolTip = on
            ? "Agent Computer Use — Samantha on"
            : (active ? "Agent Computer Use — active" : "Agent Computer Use")
    }

    @objc private func samanthaToggled(_ sender: NSSwitch) {
        guard !suppressToggleAction else { return }
        if sender.state == .on {
            Task { @MainActor in
                let started = await self.voiceSupervisor?.start() ?? false
                guard !started else { return }
                self.suppressToggleAction = true
                sender.state = .off
                self.suppressToggleAction = false
                self.showMicrophoneRequiredAlert()
            }
        } else {
            voiceSupervisor?.stop()
        }
    }

    private func showMicrophoneRequiredAlert() {
        let alert = NSAlert()
        alert.messageText = "Microphone access required"
        alert.informativeText = (
            "Turning Samantha on uses your microphone. Allow access when macOS prompts, "
            + "or enable Voice CUA under System Settings → Privacy & Security → Microphone."
        )
        alert.addButton(withTitle: "Open Settings")
        alert.addButton(withTitle: "OK")
        if alert.runModal() == .alertFirstButtonReturn {
            if let url = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    @objc private func openSettings() {
        SettingsWindowController.shared.onSaved = { [weak self] _ in
            guard let self, self.voiceRunning else { return }
            self.noteRestartIfVoiceOn()
        }
        SettingsWindowController.shared.showSettings()
    }

    private func noteRestartIfVoiceOn() {
        let alert = NSAlert()
        alert.messageText = "Restart Samantha?"
        alert.informativeText = "Settings apply on the next voice session. Turn Samantha off and on now?"
        alert.addButton(withTitle: "Restart Now")
        alert.addButton(withTitle: "Later")
        if alert.runModal() == .alertFirstButtonReturn {
            voiceSupervisor?.stop()
            Task { @MainActor in
                _ = await self.voiceSupervisor?.start()
            }
        }
    }

    @objc private func quitApplication(_ sender: Any?) {
        voiceSupervisor?.stop()
        islandController?.stopStreaming()
        NSApplication.shared.terminate(sender)
    }

    private static func loadIcon() -> NSImage {
        if let img = loadBundled() { return img }
        let fallback = NSImage(
            systemSymbolName: "display.and.arrow.down",
            accessibilityDescription: "Agent Computer Use"
        ) ?? NSImage(size: NSSize(width: 18, height: 18))
        fallback.isTemplate = false
        let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        if let configured = fallback.withSymbolConfiguration(config) {
            return configured
        }
        return fallback
    }

    private static func loadBundled() -> NSImage? {
        let names = ["MenubarIcon", "MenubarIcon@2x"]
        if let url = Bundle.main.url(forResource: "MenubarIcon", withExtension: "png"),
           let img = NSImage(contentsOf: url) {
            img.isTemplate = false
            img.size = NSSize(width: 18, height: 18)
            return img
        }
        let res = Bundle.main.resourceURL
            ?? Bundle.main.bundleURL
                .appendingPathComponent("Contents/Resources", isDirectory: true)
        for name in names {
            let url = res.appendingPathComponent("\(name).png")
            if let img = NSImage(contentsOf: url) {
                img.isTemplate = false
                img.size = NSSize(width: 18, height: 18)
                return img
            }
        }
        return nil
    }

}
