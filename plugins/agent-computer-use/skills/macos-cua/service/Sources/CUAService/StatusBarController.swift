import AVFoundation
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
    private let gate: IslandGate
    private var gateItem: NSMenuItem?
    private var wallItem: NSMenuItem?
    private var wallErrorItem: NSMenuItem?
    private var voiceState = "idle"
    private var active = false
    private var voiceRunning = false
    private var suppressToggleAction = false

    private weak var voiceSupervisor: VoiceSupervisor?
    private weak var islandController: IslandController?

    private var samanthaRow: MenuSwitchRowView?
    private var showIslandItem: NSMenuItem?
    private var openIslandItem: NSMenuItem?

    init(gate: IslandGate? = nil) {
        self.gate = gate ?? .shared
        super.init()
    }

    func wire(voice: VoiceSupervisor, island: IslandController) {
        island.onVoiceStateChanged = { [weak self] kind in
            self?.voiceState = kind
            self?.updateGateStatus()
        }
        voiceSupervisor = voice
        islandController = island
        voice.onGatewayPortChanged = { [weak island] port in
            island?.updateGateway(port: port)
        }
        voice.onRunningChanged = { [weak self] running in
            self?.setVoiceRunning(running)
            if running {
                island.updateGateway(port: voice.gatewayPort)
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
            button.image = Self.loadIcon()   // template: tinted like the other menu bar icons
            button.toolTip = "Agent Computer Use"
            button.imagePosition = .imageOnly
        }
        status.menu = makeMenu()
        item = status
        setActive(false)
        setVoiceRunning(false)
    }

    /// The status-bar menu (kept separate from NSStatusBar so tests can inspect and render it).
    func makeMenu() -> NSMenu {
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

        let access = NSMenuItem(title: gate.accessSummary, action: #selector(openGate), keyEquivalent: "")
        access.target = self
        menu.addItem(access)
        gateItem = access
        gate.onChange.append { [weak self] in self?.updateGateStatus() }
        updateGateAccountRow()

        let openIsland = NSMenuItem(title: "Show Dynamic Island", action: #selector(openIslandCard), keyEquivalent: "")
        openIsland.target = self
        menu.addItem(openIsland)
        openIslandItem = openIsland
        updateShortcut(IslandSettingsStore.shared)
        let showIsland = NSMenuItem(title: "Show Island in Menu Bar", action: #selector(toggleShowIsland(_:)), keyEquivalent: "")
        showIsland.target = self
        showIsland.state = IslandSettingsStore.shared.bool(IslandSettingsSchema.showIsland) ? .on : .off
        menu.addItem(showIsland)
        showIslandItem = showIsland
        IslandSettingsStore.shared.addObserver { [weak self] s in
            self?.showIslandItem?.state = s.bool(IslandSettingsSchema.showIsland) ? .on : .off
            self?.updateShortcut(s)
        }

        // ACU Settings (⌘,): one settings window for the whole plugin (island, Samantha voice, agents, Computer Use).
        let settingsItem = NSMenuItem(
            title: "ACU Settings…",
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
        return menu
    }

    func setActive(_ on: Bool) {
        active = on
        defer { updateGateStatus() }
        item?.button?.appearsDisabled = false
        item?.button?.alphaValue = 1.0
        item?.button?.toolTip = on
            ? "Agent Computer Use — active"
            : "Agent Computer Use"
    }

    private func setVoiceRunning(_ on: Bool) {
        voiceRunning = on
        defer { updateGateStatus() }
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
            // User click: provision Samantha's island token if it doesn't exist yet.
            IslandSettingsStore.shared.ensureToken(.samantha)
            Task { @MainActor in
                let started = await self.voiceSupervisor?.start() ?? false
                guard !started else { return }
                self.suppressToggleAction = true
                sender.state = .off
                self.suppressToggleAction = false
                // Never a prompt or a home-made dialog: onboarding's Microphone row does the asking.
                if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized {
                    OnboardingWindowController.shared.show(row: .microphone)
                }
            }
        } else {
            voiceSupervisor?.stop()
        }
    }

    private func updateShortcut(_ store: IslandSettingsStore) {
        let shortcut = IslandShortcut.read(store)
        openIslandItem?.keyEquivalent = shortcut.character.lowercased()
        openIslandItem?.keyEquivalentModifierMask = shortcut.menuModifiers
    }

    @objc private func openGate() { gate.show() }

    @objc private func toggleWallVisibility() { gate.setWallVisibility(!gate.showOnWall) }

    private func updateGateAccountRow() {
        gateItem?.title = gate.accessSummary
        guard gate.isUnlocked else {
            gateItem?.submenu = nil; wallItem = nil; wallErrorItem = nil
            return
        }
        if gateItem?.submenu == nil {
            let menu = NSMenu(title: "Island account")
            let details = NSMenuItem(title: "Account details…", action: #selector(openGate), keyEquivalent: "")
            details.target = self
            menu.addItem(details)
            let visibility = NSMenuItem(title: "Show me on the site", action: #selector(toggleWallVisibility), keyEquivalent: "")
            visibility.target = self
            menu.addItem(visibility)
            wallItem = visibility
            let error = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            error.isEnabled = false
            menu.addItem(error)
            wallErrorItem = error
            // Keep the optimistic request disabled while AppKit tracks the open menu.
            menu.autoenablesItems = false
            gateItem?.submenu = menu
        }
        wallItem?.state = gate.showOnWall ? .on : .off
        wallItem?.isEnabled = !gate.wallUpdating
        wallErrorItem?.isHidden = gate.wallError == nil
        wallErrorItem?.title = gate.wallError ?? ""
        wallErrorItem?.toolTip = gate.wallError
    }

    private func updateGateStatus() {
        updateGateAccountRow()
        guard let button = item?.button else { return }
        let kind = voiceRunning ? voiceState : "idle"
        let symbol: String?
        switch kind {
        case "listening": symbol = "mic.fill"
        case "speaking": symbol = "waveform"
        case "thinking", "reconnecting": symbol = "ellipsis.bubble.fill"
        default: symbol = nil
        }
        if !gate.isUnlocked, let symbol {
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Samantha \(kind)")
            button.toolTip = "Agent Computer Use — Samantha \(kind)"
        } else {
            button.image = Self.loadIcon()
            button.toolTip = voiceRunning ? "Agent Computer Use — Samantha on" : (active ? "Agent Computer Use — active" : "Agent Computer Use")
        }
    }

    @objc private func openIslandCard() { islandController?.openKeyboard() }

    @objc private func toggleShowIsland(_ sender: NSMenuItem) {
        let s = IslandSettingsStore.shared
        s.set(!s.bool(IslandSettingsSchema.showIsland), for: IslandSettingsSchema.showIsland)
    }

    @objc private func openSettings() {
        IslandSettingsWindowController.shared.showSettings()
    }

    @objc private func quitApplication(_ sender: Any?) {
        voiceSupervisor?.stop()
        islandController?.stopStreaming()
        NSApplication.shared.terminate(sender)
    }

    /// "Island Cursor" template glyph: StatusIcon.pdf (18×18 pt vector) first, PNGs as fallback.
    /// Template, so the menu bar tints it like every other status item (white on dark, black on light).
    static func loadIcon(resources: URL? = Bundle.main.resourceURL) -> NSImage {
        let img = loadBundled(resources: resources)
            ?? NSImage(systemSymbolName: "cursorarrow", accessibilityDescription: "Agent Computer Use")
            ?? NSImage(size: NSSize(width: 18, height: 18))
        img.isTemplate = true
        img.accessibilityDescription = "Agent Computer Use"
        return img
    }

    static func loadBundled(resources: URL?) -> NSImage? {
        guard let res = resources else { return nil }
        if let pdf = NSImage(contentsOf: res.appendingPathComponent("StatusIcon.pdf")) {
            pdf.size = NSSize(width: 18, height: 18)
            return pdf
        }
        // PNG set: one NSImage carrying the 1x/2x/3x reps.
        let reps = ["StatusIcon.png", "StatusIcon@2x.png", "StatusIcon@3x.png"].compactMap {
            NSImageRep(contentsOf: res.appendingPathComponent($0))
        }
        guard !reps.isEmpty else { return nil }
        let img = NSImage(size: NSSize(width: 18, height: 18))
        for r in reps { r.size = NSSize(width: 18, height: 18); img.addRepresentation(r) }
        return img
    }

}
