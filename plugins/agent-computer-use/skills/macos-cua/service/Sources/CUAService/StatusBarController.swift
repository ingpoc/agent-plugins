import AppKit
import Foundation

/// White menubar glyph for Agent Computer Use (not a template — stays white
/// like neighboring status items on accent/colored menu bars).
@MainActor
final class StatusBarController: NSObject {
    private var item: NSStatusItem?
    private var active = false
    private var voiceRunning = false

    private weak var voiceSupervisor: VoiceSupervisor?
    private weak var islandController: IslandController?

    private var startVoiceItem: NSMenuItem?
    private var stopVoiceItem: NSMenuItem?
    private var approveItem: NSMenuItem?
    private var denyItem: NSMenuItem?

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

        let start = NSMenuItem(
            title: "Voice ▶ Start",
            action: #selector(startVoice),
            keyEquivalent: ""
        )
        start.target = self
        menu.addItem(start)
        startVoiceItem = start

        let stop = NSMenuItem(
            title: "Voice ⏹ Stop",
            action: #selector(stopVoice),
            keyEquivalent: ""
        )
        stop.target = self
        stop.isEnabled = false
        menu.addItem(stop)
        stopVoiceItem = stop

        menu.addItem(NSMenuItem.separator())

        let approve = NSMenuItem(
            title: "Approve confirm",
            action: #selector(approveConfirm),
            keyEquivalent: "y"
        )
        approve.keyEquivalentModifierMask = [.command]
        approve.target = self
        menu.addItem(approve)
        approveItem = approve

        let deny = NSMenuItem(
            title: "Deny confirm",
            action: #selector(denyConfirm),
            keyEquivalent: "n"
        )
        deny.keyEquivalentModifierMask = [.command]
        deny.target = self
        menu.addItem(deny)
        denyItem = deny

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
        startVoiceItem?.isEnabled = !on
        stopVoiceItem?.isEnabled = on
        item?.button?.toolTip = on
            ? "Agent Computer Use — voice on"
            : (active ? "Agent Computer Use — active" : "Agent Computer Use")
    }

    @objc private func startVoice() {
        voiceSupervisor?.start()
    }

    @objc private func stopVoice() {
        voiceSupervisor?.stop()
    }

    @objc private func approveConfirm() {
        Task { await islandController?.resolveConfirm(approved: true) }
    }

    @objc private func denyConfirm() {
        Task { await islandController?.resolveConfirm(approved: false) }
    }

    private static func loadIcon() -> NSImage {
        if let img = loadBundled() { return img }
        if let img = loadSkillAssets() { return img }
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

    private static func loadSkillAssets() -> NSImage? {
        let candidates = [
            NSString(string: "~/.cursor/plugins/local/agent-computer-use/skills/macos-cua/assets")
                .expandingTildeInPath,
            NSString(
                string: "~/Documents/remote-claude/active/apps/agent-plugins/plugins/agent-computer-use/skills/macos-cua/assets"
            ).expandingTildeInPath,
        ]
        for dir in candidates {
            let twoX = (dir as NSString).appendingPathComponent("MenubarIcon@2x.png")
            let oneX = (dir as NSString).appendingPathComponent("MenubarIcon.png")
            for path in [twoX, oneX] where FileManager.default.fileExists(atPath: path) {
                if let img = NSImage(contentsOfFile: path) {
                    img.isTemplate = false
                    img.size = NSSize(width: 18, height: 18)
                    return img
                }
            }
        }
        return nil
    }
}
