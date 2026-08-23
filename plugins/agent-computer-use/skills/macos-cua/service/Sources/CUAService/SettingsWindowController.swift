import AppKit

@MainActor
final class SettingsWindowController: NSWindowController {
    static let shared = SettingsWindowController()

    private var modelPopup: NSPopUpButton!
    private var voicePopup: NSPopUpButton!
    private var eagernessPopup: NSPopUpButton!
    private var micProfilePopup: NSPopUpButton!
    private var noteField: NSTextField!
    var onSaved: ((VoiceSettingsStore.Settings) -> Void)?

    private init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 260),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Samantha Settings"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildUI()
        reloadFromDisk()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func showSettings() {
        reloadFromDisk()
        window?.center()
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),
        ])

        modelPopup = labeledPopup(
            title: "Realtime model",
            items: VoiceSettingsStore.models.map(\.label),
            in: stack
        )
        voicePopup = labeledPopup(
            title: "Voice",
            items: VoiceSettingsStore.voices.map(\.label),
            in: stack
        )
        eagernessPopup = labeledPopup(
            title: "Listening eagerness",
            items: VoiceSettingsStore.eagernessLevels.map(\.label),
            in: stack
        )
        micProfilePopup = labeledPopup(
            title: "Mic noise profile",
            items: VoiceSettingsStore.micProfiles.map(\.label),
            in: stack
        )

        noteField = NSTextField(wrappingLabelWithString: "")
        noteField.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        noteField.textColor = .secondaryLabelColor
        noteField.preferredMaxLayoutWidth = 380
        stack.addArrangedSubview(noteField)

        let saveButton = NSButton(title: "Save", target: self, action: #selector(saveClicked))
        saveButton.bezelStyle = .rounded
        stack.addArrangedSubview(saveButton)
    }

    private func labeledPopup(title: String, items: [String], in stack: NSStackView) -> NSPopUpButton {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 8
        let label = NSTextField(labelWithString: title)
        label.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        label.widthAnchor.constraint(equalToConstant: 140).isActive = true
        let popup = NSPopUpButton()
        popup.addItems(withTitles: items)
        row.addArrangedSubview(label)
        row.addArrangedSubview(popup)
        stack.addArrangedSubview(row)
        return popup
    }

    private func reloadFromDisk() {
        let s = VoiceSettingsStore.load()
        select(popup: modelPopup, id: s.realtimeModel, in: VoiceSettingsStore.models)
        select(popup: voicePopup, id: s.realtimeVoice, in: VoiceSettingsStore.voices)
        select(popup: eagernessPopup, id: s.eagerness, in: VoiceSettingsStore.eagernessLevels)
        select(popup: micProfilePopup, id: s.micProfile, in: VoiceSettingsStore.micProfiles)
        noteField.stringValue =
            "Config: ~/.config/voice-cua/settings.json — turn Samantha off and on after saving."
    }

    private func select(popup: NSPopUpButton, id: String, in options: [(id: String, label: String)]) {
        guard let idx = options.firstIndex(where: { $0.id == id }) else { return }
        popup.selectItem(at: idx)
    }

    private func currentSettings() -> VoiceSettingsStore.Settings {
        let mi = max(0, modelPopup.indexOfSelectedItem)
        let vi = max(0, voicePopup.indexOfSelectedItem)
        let ei = max(0, eagernessPopup.indexOfSelectedItem)
        let pi = max(0, micProfilePopup.indexOfSelectedItem)
        return VoiceSettingsStore.Settings(
            realtimeModel: VoiceSettingsStore.models[mi].id,
            realtimeVoice: VoiceSettingsStore.voices[vi].id,
            eagerness: VoiceSettingsStore.eagernessLevels[ei].id,
            micProfile: VoiceSettingsStore.micProfiles[pi].id
        )
    }

    @objc private func saveClicked() {
        let settings = currentSettings()
        do {
            try VoiceSettingsStore.save(settings)
            onSaved?(settings)
            noteField.stringValue = "Saved. Turn Samantha off and on to apply."
        } catch {
            noteField.stringValue = "Save failed: \(error.localizedDescription)"
        }
    }
}
