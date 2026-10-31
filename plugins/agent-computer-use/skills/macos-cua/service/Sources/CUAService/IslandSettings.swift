import os
import AppKit
import Foundation
import ApplicationServices
import Intents
import Security

// Island Settings (Alcove parity + AI Agents). Schema-driven: every row in the window is a
// `SettingDef`, so the window, persistence and tests share one source of truth.
// Alcove inventory: evidence/p6/alcove-settings/inventory.md.

enum SettingsTab: String, CaseIterable, Identifiable {
    case general, agents, samantha, computerUse, about
    var id: String { rawValue }
    var title: String {
        switch self {
        case .general: return "General"
        case .agents: return "Agents"
        case .samantha: return "Samantha"
        case .computerUse: return "Computer Use"
        case .about: return "About"
        }
    }
    var symbol: String {
        switch self {
        case .general: return "gearshape.fill"
        case .agents: return "person.3.fill"
        case .samantha: return "waveform"
        case .computerUse: return "cursorarrow.click.2"
        case .about: return "info"
        }
    }
    var tint: NSColor {
        switch self {
        case .samantha, .computerUse: return NSColor(red: 0.39, green: 0.82, blue: 1, alpha: 1)
        default: return NSColor(white: 0.42, alpha: 1)
        }
    }
    static let sidebar: [(String?, [SettingsTab])] = [
        (nil, [.general, .agents, .samantha]), ("System", [.computerUse])
    ]
    static let pinned: SettingsTab = .about
    static var panes: [SettingsTab] { sidebar.flatMap(\.1) + [pinned] }
}

enum AgentSource: String, CaseIterable, Codable {
    case samantha, grokBot = "grok", cursor, codex, claude, agentRoster = "agent"
    var title: String {
        switch self {
        case .samantha: return "Samantha"
        case .grokBot: return "Grok Bot"
        case .cursor: return "Cursor"
        case .codex: return "Codex"
        case .claude: return "Claude"
        case .agentRoster: return "Agent Roster"
        }
    }
    /// Map an island agent name/app ("Codex", "Grok Bot", "Cursor") to a source.
    static func match(_ s: String) -> AgentSource? {
        let l = s.lowercased()
        if l.contains("samantha") { return .samantha }
        if l.contains("grok") || l == "comet" { return .grokBot }  // Grok Bot drives Comet
        if l.contains("cursor") { return .cursor }
        if l.contains("codex") { return .codex }
        if l.contains("claude") { return .claude }
        if l == "agent" || l.contains("agent roster") { return .agentRoster }
        return nil
    }
    /// Codex and Claude are observed read-only (thread state / hooks); they cannot take a decision back.
    var canDecide: Bool { self != .codex && self != .claude }
    var keyPrefix: String { "island.agent.\(rawValue)." }
    /// CLI / wire name (island-post --source).
    var cliName: String { self == .grokBot ? "grok-bot" : rawValue }
    var defaultApp: String {
        switch self {
        case .samantha: return ""
        case .grokBot: return "Grok Bot"
        case .cursor: return "Cursor"
        case .codex: return "Codex"
        case .claude: return "Claude"
        case .agentRoster: return ""
        }
    }
    /// Exact source names accepted on the wire.
    static func parse(_ s: String) -> AgentSource? {
        switch s.lowercased().trimmingCharacters(in: .whitespaces) {
        case "samantha": return .samantha
        case "grok", "grok-bot", "grokbot", "grok_bot": return .grokBot
        case "cursor": return .cursor
        case "codex": return .codex
        case "claude": return .claude
        case "agent": return .agentRoster
        default: return nil
        }
    }
}

enum AttentionCap: String, CaseIterable {
    case ambient, info, attention, critical
    var rank: Int { Self.allCases.firstIndex(of: self) ?? 0 }
}

enum SettingKind: Equatable {
    case master(String)                                   // master toggle row with symbol
    case toggle
    case slider(min: Double, max: Double, step: Double, unit: String)
    case tiles([(value: String, label: String, symbol: String)])
    case popup([(value: String, label: String)])
    case disclosure, sourceTable, shortcut
    case token                                            // AI source Keychain token row
    case button(String)                                   // action row
    case permission(String)                               // macOS permission: "Allowed" when granted, else this button

    static func == (a: SettingKind, b: SettingKind) -> Bool {
        switch (a, b) {
        case (.master(let x), .master(let y)): return x == y
        case (.disclosure, .disclosure), (.sourceTable, .sourceTable), (.shortcut, .shortcut), (.toggle, .toggle), (.token, .token): return true
        case (.slider(let a1, let b1, let c1, let d1), .slider(let a2, let b2, let c2, let d2)):
            return a1 == a2 && b1 == b2 && c1 == c2 && d1 == d2
        case (.tiles(let x), .tiles(let y)): return x.map(\.value) == y.map(\.value)
        case (.popup(let x), .popup(let y)): return x.map(\.value) == y.map(\.value)
        case (.button(let x), .button(let y)), (.permission(let x), .permission(let y)): return x == y
        default: return false
        }
    }
}

enum SettingDefault: Equatable {
    case bool(Bool), double(Double), string(String), none
}

struct SettingDef: Identifiable {
    var tab: SettingsTab
    var section: String?          // group heading (outside the card)
    var key: String
    var label: String
    var kind: SettingKind
    var defaultValue: SettingDefault
    /// Hover help ("i" glyph); at most one per card.
    var info: String? = nil
    /// Only shown when this bool key is on (e.g. Hover duration needs Expand on hover).
    var visibleWhen: String? = nil
    /// 11 pt caption under the row (master rows explain the feature here instead of an "i").
    var caption: String? = nil
    var dynamicOptions: (@MainActor (IslandSettingsStore) -> [(value: String, label: String)])? = nil
    var id: String { key }
}

enum IslandSettingsSchema {
    // Keys used by wiring code.
    static let hideInFullscreen = "island.general.hideInFullscreen"
    static let hideInMissionControl = "island.general.hideInMissionControl"
    static let hideFromScreenCapture = "island.general.hideFromScreenCapture"
    static let displayOn = "island.general.displayOn"
    static let idleActivity = "island.general.idleActivity"
    static let contrastOutline = "island.general.contrastOutline"
    static let expandOnHover = "island.general.expandOnHover"
    static let hoverDuration = "island.general.hoverDuration"
    static let resultPeek = "island.general.resultPeek"
    static let errorPeek = "island.general.errorPeek"
    static let doneSettle = "island.general.doneSettle"
    static let selectedDisplay = "island.general.selectedDisplayUUID"
    static let showIsland = "island.general.showIsland"
    static let mutePromptSound = "island.general.mutePromptSound"
    // Phase A2–A9
    static let soundEnabled = "island.sound.enabled"
    static let soundDuration = "island.sound.duration"
    static let soundSpeed = "island.sound.speed"
    static let displayEnabled = "island.display.enabled"
    static let connectivityEnabled = "island.connectivity.enabled"
    static let peekDuration = "island.connectivity.duration"
    static let agentPeeks = "island.connectivity.agentPeeks"
    static let hideWhileSharing = "island.connectivity.hideWhileSharing"
    static let respectFocus = "island.connectivity.respectFocus"
    static let focusAccess = "island.connectivity.focusAccess"
    static let hotKey = "island.general.hotKey"
    static let shortcut = "island.general.shortcut"
    nonisolated(unsafe) static var focusAccessAction: (() -> Void)?
    static let calendarEnabled = "island.calendar.enabled"
    static let calendarLead = "island.calendar.timeBefore"
    static let calendarColor = "island.calendar.useCalendarColor"
    static let calendarAccess = "island.calendar.access"
    static let fileTrayEnabled = "island.fileTray.enabled"
    static let haptics = "island.general.haptics"
    static let naturalMovement = "island.general.naturalMovement"
    static let swipeToToggle = "island.general.swipeToToggle"
    static let swipeToCycle = "island.general.swipeToCycle"
    /// Settings → Calendar → Grant access… (set by the island controller).
    nonisolated(unsafe) static var calendarAccessAction: (() -> Void)?

    static func t(_ v: String, _ l: String, _ s: String = "") -> (value: String, label: String, symbol: String) { (v, l, s) }

    static let openOn = "island.general.openOn"
    static let openAtLogin = "island.general.openAtLogin"
    static let moreExpanded = "island.settings.moreExpanded"
    static let tokensExpanded = "island.settings.tokensExpanded"
    static let alertDuration = "island.agents.alertDuration"
    static let promptSound = "island.agents.promptSound"
    static let restoreAgents = "island.agents.restoreDefaults"
    static let sourceTable = "island.agents.sources"
    static let all: [SettingDef] = general + agents + samantha + computerUse + about + sourceSettings + backing
    /// Stored settings drawn by another row: hotKey is the Keyboard shortcut row's clear (x) / record state.
    static let backing: [SettingDef] = [
        .init(tab: .general, section: nil, key: hotKey, label: "Keyboard shortcut on", kind: .toggle, defaultValue: .bool(true)),
    ]

    // Samantha voice settings: same keys as ~/.config/voice-cua/settings.json (read by voice_settings.py),
    // so stored values survive the move from the old Samantha Settings window.
    static let voicePrefix = "voice."
    static let voiceModel = "voice.realtime_model"
    static let voiceVoice = "voice.realtime_voice"
    static let voiceEagerness = "voice.eagerness"
    static let voiceMic = "voice.mic_profile"
    static let voiceLiveBulk = "voice.live_bulk_model"
    static let voiceLiveIntelligence = "voice.live_intelligence_model"
    /// Listening / Microphone tuned the removed Realtime session; GPT-Live owns turn-taking, so
    /// those keys stay in settings.json (local mic gate) but have no Settings row.
    static let voiceKeys = [voiceModel, voiceVoice, voiceLiveBulk, voiceLiveIntelligence]
    /// `visibleWhen` for the GPT-Live-1 backend pickers ("key=value" form).
    static let whenLive = voiceModel + "=" + VoiceSettingsStore.liveModel

    // Computer Use (CUAService) — the onboarding rows: real state, Grant = native request or deep link.
    static let cuaAccessibility = ACUPermission.accessibility.key
    static let cuaScreenRecording = ACUPermission.screenRecording.key
    static let computerUse: [SettingDef] = ACUPermission.allCases.map { p in
        .init(tab: .computerUse, section: p.section, key: p.key, label: p.label,
              kind: .permission("Grant…"), defaultValue: .none,
              visibleWhen: p.bundleID.map { installedPrefix + $0 },
              caption: p.caption)
    }
    /// `visibleWhen` for Automation rows: hidden when the target app isn't installed.
    static let installedPrefix = "installed:"

    static let accessibilityDisplay = "island.display.accessibility"
    static let accessibilitySound = "island.sound.accessibility"

    static let general: [SettingDef] = [
        .init(tab: .general, section: nil, key: showIsland, label: "Show Dynamic Island", kind: .toggle, defaultValue: .bool(true)),
        .init(tab: .general, section: nil, key: displayOn, label: "Show on", kind: .popup([]), defaultValue: .string("main"),
              dynamicOptions: { $0.displayOptions() }),
        .init(tab: .general, section: nil, key: openOn, label: "Open the island",
              kind: .popup([("click", "On click"), ("hover", "On hover")]), defaultValue: .string("click")),
        .init(tab: .general, section: nil, key: hoverDuration, label: "Hover delay", kind: .slider(min: 0.1, max: 0.5, step: 0.1, unit: "s"),
              defaultValue: .double(0.2), visibleWhen: openOn + "=hover"),
        .init(tab: .general, section: nil, key: shortcut, label: "Keyboard shortcut", kind: .shortcut, defaultValue: .none),
        .init(tab: .general, section: nil, key: openAtLogin, label: "Open at login", kind: .toggle, defaultValue: .bool(true)),
        .init(tab: .general, section: nil, key: moreExpanded, label: "More", kind: .disclosure, defaultValue: .bool(false)),
    ] + more

    static let more: [SettingDef] = {
        let behaviour: [SettingDef] = [
            .init(tab: .general, section: "Behaviour", key: contrastOutline, label: "Contrast outline", kind: .toggle, defaultValue: .bool(true)),
            .init(tab: .general, section: "Behaviour", key: haptics, label: "Haptic feedback", kind: .toggle, defaultValue: .bool(true)),
            .init(tab: .general, section: "Behaviour", key: naturalMovement, label: "Natural movement", kind: .toggle, defaultValue: .bool(false)),
            .init(tab: .general, section: "Behaviour", key: swipeToToggle, label: "Swipe to toggle open", kind: .toggle, defaultValue: .bool(true)),
            .init(tab: .general, section: "Behaviour", key: swipeToCycle, label: "Swipe to switch activity", kind: .toggle, defaultValue: .bool(true)),
            .init(tab: .general, section: "Behaviour", key: idleActivity, label: "Idle activity",
                  kind: .tiles([t("none", "None", "nosign"), t("samantha", "Samantha", "circle.circle.fill"),
                                t("agents", "Agents", "person.2.fill"), t("duo", "Duo", "rectangle.split.2x1.fill")]), defaultValue: .string("duo")),
        ]
        let audio: [SettingDef] = [
            .init(tab: .general, section: "Audio output", key: connectivityEnabled, label: "Audio output", kind: .master("headphones"),
                  defaultValue: .bool(true), caption: "Quick Peek when the audio output changes.")
        ]
        return (behaviour + display + sound + audio + nowPlaying + calendar + fileTray).map {
            var d = $0; d.visibleWhen = moreExpanded; return d
        }
    }()

    static func hud(_ name: String, _ title: String, symbol: String, caption: String, accessibilityKey: String) -> [SettingDef] {
        let p = "island.\(name)."
        return [
            .init(tab: .general, section: title, key: p + "enabled", label: title, kind: .master(symbol), defaultValue: .bool(true), caption: caption),
            .init(tab: .general, section: title, key: accessibilityKey, label: "Accessibility access", kind: .permission("Allow…"), defaultValue: .none),
            .init(tab: .general, section: title, key: p + "duration", label: "Duration", kind: .slider(min: 1, max: 2, step: 0.1, unit: "s"), defaultValue: .double(1.2)),
            .init(tab: .general, section: title, key: p + "speed", label: "Speed",
                  kind: .tiles([t("smooth", "Smooth", "scribble"), t("fast", "Fast", "hare.fill"), t("instant", "Instant", "bolt.fill")]), defaultValue: .string("fast")),
        ]
    }
    static let display = hud("display", "Display HUD", symbol: "sun.max.fill",
                             caption: "Replaces the brightness HUD on built-in displays; external display keys pass through.", accessibilityKey: accessibilityDisplay)
    static let sound = hud("sound", "Sound HUD", symbol: "speaker.wave.2.fill",
                           caption: "Replaces the system volume HUD.", accessibilityKey: accessibilitySound)

    static let nowPlaying: [SettingDef] = {
        typealias K = NowPlayingKeys
        return [
            .init(tab: .general, section: "Now Playing", key: K.enabled, label: "Now Playing", kind: .master("play.fill"), defaultValue: .bool(true)),
            .init(tab: .general, section: "Now Playing", key: K.idleDuration, label: "Idle duration", kind: .slider(min: 0, max: 10, step: 1, unit: "s"), defaultValue: .double(3)),
            .init(tab: .general, section: "Now Playing", key: K.color, label: "Progress colour",
                  kind: .tiles([t("mono", "Monochrome", "minus"), t("color", "Colored", "minus"), t("gradient", "Gradient", "minus")]),
                  defaultValue: .string("mono")),
            .init(tab: .general, section: "Now Playing", key: K.disableCompactWaveform, label: "Disable compact waveform", kind: .toggle, defaultValue: .bool(false)),
            .init(tab: .general, section: "Now Playing", key: K.disableArtworkFlip, label: "Disable artwork flip", kind: .toggle, defaultValue: .bool(false)),
            .init(tab: .general, section: "Now Playing", key: K.hideWhileSourceActive, label: "Hide while source app is active", kind: .toggle, defaultValue: .bool(false)),
            .init(tab: .general, section: "Now Playing", key: K.hideTitleExtras, label: "Hide media title extras", kind: .toggle, defaultValue: .bool(false)),
            .init(tab: .general, section: "Now Playing", key: K.action, label: "Action button",
                  kind: .tiles([t("favorite", "Like", "star.fill"), t("repeat", "Repeat", "repeat"), t("copy", "Copy", "link")]),
                  defaultValue: .string("favorite"), info: "Like shows only when the source supports it (Music)"),
            .init(tab: .general, section: "Now Playing", key: K.automaticSource, label: "Automatic", kind: .toggle, defaultValue: .bool(true)),
            .init(tab: .general, section: "Now Playing", key: K.app, label: "App", kind: .popup([("system", "System"), ("music", "Music"), ("spotify", "Spotify")]),
                  defaultValue: .string("music"), caption: "Used when Automatic is off"),
        ]
    }()

    static let calendar: [SettingDef] = {
        let p = "island.calendar."
        return [
            .init(tab: .general, section: "Calendar", key: p + "enabled", label: "Calendar", kind: .master("calendar"), defaultValue: .bool(true),
                  caption: "Read-only: the island never edits your calendars."),
            .init(tab: .general, section: "Calendar", key: calendarAccess, label: "Calendar access", kind: .permission("Allow…"), defaultValue: .none),
            .init(tab: .general, section: "Calendar", key: p + "timeBefore", label: "Time before event", kind: .slider(min: 5, max: 30, step: 5, unit: "m"), defaultValue: .double(10)),
            .init(tab: .general, section: "Calendar", key: p + "useCalendarColor", label: "Use calendar color", kind: .toggle, defaultValue: .bool(false)),
        ]
    }()

    static let fileTray: [SettingDef] = [
        .init(tab: .general, section: "File Tray", key: fileTrayEnabled, label: "File Tray", kind: .master("tray.fill"), defaultValue: .bool(true),
              caption: "Drag files onto the island to hold them here."),
    ]

    static let tableSources: [AgentSource] = [.samantha, .grokBot, .codex, .claude, .cursor]
    static let alertOptions = [("info", "List only"), ("attention", "Needs you"), ("critical", "Needs you, done and errors")]
    static let sourceSettings: [SettingDef] = AgentSource.allCases.flatMap { s -> [SettingDef] in
        let p = s.keyPrefix
        return [
            .init(tab: .agents, section: nil, key: p + "enabled", label: s.title, kind: .toggle, defaultValue: .bool(true)),
            .init(tab: .agents, section: nil, key: p + "cap", label: s.title + " alerts", kind: .popup(alertOptions), defaultValue: .string(s == .agentRoster ? "info" : "attention")),
            .init(tab: .agents, section: nil, key: p + "canRaiseApprovals", label: s.title + " approve", kind: .toggle, defaultValue: .bool(s.canDecide && s != .agentRoster)),
        ]
    }
    static let agents: [SettingDef] = [
        .init(tab: .agents, section: nil, key: sourceTable, label: "Show agents from", kind: .sourceTable, defaultValue: .none),
        .init(tab: .agents, section: nil, key: alertDuration, label: "Alerts stay on screen for", kind: .slider(min: 3, max: 8, step: 0.5, unit: "s"),
              defaultValue: .double(4), caption: "Errors stay 2 s longer. Hovering keeps an alert open."),
        .init(tab: .agents, section: nil, key: promptSound, label: "Play a sound when an agent needs you", kind: .toggle,
              defaultValue: .bool(true), caption: "Only when that app isn't in front."),
        .init(tab: .agents, section: "Quiet during", key: respectFocus, label: "Focus", kind: .toggle, defaultValue: .bool(true)),
        .init(tab: .agents, section: "Quiet during", key: focusAccess, label: "Focus status access", kind: .permission("Allow…"), defaultValue: .none),
        .init(tab: .agents, section: "Quiet during", key: hideWhileSharing, label: "Screen sharing", kind: .toggle, defaultValue: .bool(true)),
        .init(tab: .agents, section: "Quiet during", key: hideInFullscreen, label: "Full-screen apps", kind: .toggle, defaultValue: .bool(true),
              caption: "No peeks or sounds. Screen sharing also hides titles."),
        .init(tab: .agents, section: nil, key: hideFromScreenCapture, label: "Hide island in screenshots and recordings", kind: .toggle, defaultValue: .bool(false)),
        .init(tab: .agents, section: nil, key: tokensExpanded, label: "Setup tokens", kind: .disclosure, defaultValue: .bool(false)),
    ] + [AgentSource.codex, .claude, .cursor, .grokBot, .agentRoster].map { s in
        .init(tab: .agents, section: "Setup tokens", key: s.keyPrefix + "token", label: s.title, kind: .token, defaultValue: .none,
              visibleWhen: tokensExpanded)
    } + [
        .init(tab: .agents, section: nil, key: restoreAgents, label: "Restore defaults", kind: .button("Restore defaults"), defaultValue: .none)
    ]
    static let samantha: [SettingDef] = [
        .init(tab: .samantha, section: nil, key: voiceVoice, label: "Voice", kind: .popup(VoiceSettingsStore.voices),
              defaultValue: .string(VoiceSettingsStore.Settings.default.realtimeVoice), visibleWhen: whenLive),
        .init(tab: .samantha, section: nil, key: voiceModel, label: "Model", kind: .popup(VoiceSettingsStore.models),
              defaultValue: .string(VoiceSettingsStore.Settings.default.realtimeModel), caption: "Changes apply the next time Samantha starts.")
    ]
    static func agent(_ s: AgentSource) -> [SettingDef] { sourceSettings.filter { $0.key.hasPrefix(s.keyPrefix) } }

    static let about: [SettingDef] = [
        .init(tab: .about, section: nil, key: "island.about.openLog", label: "Activity log", kind: .button("Show in Finder"), defaultValue: .none),
    ]

    /// Settings removed with their rows (Soon features, per-agent durations). Stored values are inert and are
    /// dropped on launch; SETTINGS-PACKET.md lists them so they can come back with their features.
    static let retiredKeys: [String] = {
        let g = "island.general.", c = "island.calendar.", n = "island.nowPlaying.", k = "island.connectivity."
        var keys = [g + "launchAtLogin", g + "hideWhileGaming", g + "forceSimulatedNotch", g + "mostRecent", g + "progressiveBlur",
                    g + "swipeToDismiss", g + "swipeToSkip",
                    k + "warnLow", k + "lowThreshold", k + "playSound", k + "model",
                    n + "badge", n + "liveWaveform",
                    c + "playSoundEvent", c + "hourlyChime", c + "disableDuringEvents", c + "weatherOnEmptyDay", c + "eventIndicators",
                    c + "monthView", c + "timeToLeave", c + "calendarsMode",
                    "island.sound.output", "island.about.autoUpdate", "island.agent.samantha.voiceSettings"]
        for h in ["island.display.", "island.sound."] { keys += [h + "style", h + "showPercentage", h + "hideLabel"] }
        for s in AgentSource.allCases { keys += [s.keyPrefix + "needsYouPeek", s.keyPrefix + "donePeek", s.keyPrefix + "errorPeek"] }
        return keys
    }()
    static let retiredPrefixes = ["island.battery.", "island.focus.", "island.lockScreen."]

    /// Real permission state for `.permission` rows. Reads only; never prompts.
    nonisolated static func permissionGranted(_ key: String) -> Bool {
        switch key {
        case calendarAccess: return IslandCalendarController.authorized
        case focusAccess: return INFocusStatusCenter.default.authorizationStatus == .authorized
        case accessibilityDisplay, accessibilitySound: return AXIsProcessTrusted()
        default: return ACUPermission.from(key: key).map { PermissionCenter.state($0) == .granted } ?? false
        }
    }

    static func defs(for tab: SettingsTab) -> [SettingDef] { all.filter { d in d.tab == tab && !(sourceSettings + backing).contains { $0.key == d.key } } }
    static func def(_ key: String) -> SettingDef? { all.first { $0.key == key } }

}

// MARK: - Store

@MainActor
final class IslandSettingsStore: ObservableObject {
    static let shared: IslandSettingsStore = {
        let s = IslandSettingsStore(defaults: .standard)
        s.dropRetiredSettings()
        s.seedLoginItem()
        return s
    }()
    let defaults: UserDefaults
    private(set) var tokens: IslandTokenStore
    /// `voice.*` rows live in ~/.config/voice-cua/settings.json (shared with the voice runtime).
    /// Only the real store uses the file; test stores keep them in their own defaults suite.
    let voiceInFile: Bool
    let loginItem: IslandLoginItem
    var screens: () -> [IslandSettingsScreen]


    init(defaults: UserDefaults, tokens: IslandTokenStore? = nil, voiceInFile: Bool? = nil,
         loginItem: IslandLoginItem? = nil, screens: @escaping () -> [IslandSettingsScreen] = IslandSettingsScreen.connected) {
        self.defaults = defaults
        let isLive = defaults === UserDefaults.standard && !IslandEventLog.isTesting
        self.tokens = tokens ?? (isLive ? KeychainTokenStore() : MemoryTokenStore())
        self.loginItem = loginItem ?? (isLive ? SystemIslandLoginItem() : MemoryIslandLoginItem())
        self.screens = screens
        self.voiceInFile = voiceInFile ?? isLive
    }

    /// Removes values of retired rows (inert since they were Soon / per-agent) and maps the retired
    /// "All displays" choice to the main display (what it already fell back to). Idempotent.
    func dropRetiredSettings() {
        typealias K = IslandSettingsSchema
        for source in AgentSource.allCases {
            let key = source.keyPrefix + "cap"
            if defaults.string(forKey: key) == "ambient" { defaults.set("info", forKey: key) }
            if source == .samantha, defaults.string(forKey: key) == "info" { defaults.set("attention", forKey: key) }
        }
        if defaults.object(forKey: K.openOn) == nil, defaults.bool(forKey: K.expandOnHover) {
            defaults.set("hover", forKey: K.openOn)
        }
        if defaults.object(forKey: K.hoverDuration) != nil {
            defaults.set(min(0.5, max(0.1, defaults.double(forKey: K.hoverDuration))), forKey: K.hoverDuration)
        }
        if defaults.object(forKey: K.alertDuration) == nil, defaults.object(forKey: K.resultPeek) != nil {
            defaults.set(min(8, max(3, defaults.double(forKey: K.resultPeek))), forKey: K.alertDuration)
        }
        if defaults.object(forKey: K.promptSound) == nil, defaults.bool(forKey: K.mutePromptSound) {
            defaults.set(false, forKey: K.promptSound)
        }
        if !defaults.bool(forKey: "island.migrated.0264") {
            let codex = AgentSource.codex.keyPrefix + "enabled"
            if defaults.object(forKey: codex) != nil, !defaults.bool(forKey: codex) { defaults.removeObject(forKey: codex) }
            defaults.set(true, forKey: "island.migrated.0264")
        }
        for key in [K.expandOnHover, K.resultPeek, K.errorPeek, K.doneSettle, K.peekDuration, K.mutePromptSound] {
            defaults.removeObject(forKey: key)
        }
        for k in IslandSettingsSchema.retiredKeys { defaults.removeObject(forKey: k) }
        for k in defaults.dictionaryRepresentation().keys where IslandSettingsSchema.retiredPrefixes.contains(where: { k.hasPrefix($0) }) {
            defaults.removeObject(forKey: k)
        }
        if defaults.string(forKey: IslandSettingsSchema.displayOn) == "all" { defaults.set("main", forKey: IslandSettingsSchema.displayOn) }
    }

    func bool(_ key: String) -> Bool {
        if key == IslandSettingsSchema.openAtLogin { return loginItem.isOn }
        if key == AgentSource.codex.keyPrefix + "canRaiseApprovals" || key == AgentSource.claude.keyPrefix + "canRaiseApprovals" { return false }
        if key == IslandSettingsSchema.hideInMissionControl { return true }
        if key == IslandSettingsSchema.agentPeeks, defaults.object(forKey: key) == nil { return true }
        if defaults.object(forKey: key) != nil { return defaults.bool(forKey: key) }
        if case .bool(let b)? = IslandSettingsSchema.def(key)?.defaultValue { return b }
        return false
    }
    func double(_ key: String) -> Double {
        if defaults.object(forKey: key) != nil { return defaults.double(forKey: key) }
        if case .double(let d)? = IslandSettingsSchema.def(key)?.defaultValue { return d }
        return 0
    }
    func string(_ key: String) -> String {
        if voiceInFile, key.hasPrefix(IslandSettingsSchema.voicePrefix) { return VoiceSettingsStore.value(for: key) }
        if let s = defaults.string(forKey: key) { return s }
        if case .string(let s)? = IslandSettingsSchema.def(key)?.defaultValue { return s }
        return ""
    }
    func set(_ value: Any, for key: String) {
        objectWillChange.send()
        if key == IslandSettingsSchema.openAtLogin, let on = value as? Bool {
            updateLoginItem(on)
            defaults.set(true, forKey: IslandSettingsSchema.openAtLogin + ".seeded")
            for o in observers { o(self) }
            return
        }
        if key == IslandSettingsSchema.displayOn, let choice = value as? String, choice.hasPrefix("selected:") {
            defaults.set(String(choice.dropFirst("selected:".count)), forKey: IslandSettingsSchema.selectedDisplay)
            defaults.set("selected", forKey: key)
            for o in observers { o(self) }
            return
        }
        let value: Any = key == AgentSource.samantha.keyPrefix + "cap" && ["info", "ambient"].contains(value as? String ?? "") ? "attention" : value
        if voiceInFile, key.hasPrefix(IslandSettingsSchema.voicePrefix) {
            VoiceSettingsStore.set(String(describing: value), for: key)  // settings.json, not UserDefaults
        } else {
            defaults.set(value, forKey: key)
        }
        for o in observers { o(self) }
    }
    private var observers: [(IslandSettingsStore) -> Void] = []
    func addObserver(_ o: @escaping (IslandSettingsStore) -> Void) { observers.append(o) }

    // Agent gating (§10/§11).
    func sourceEnabled(_ s: AgentSource) -> Bool { bool(s.keyPrefix + "enabled") }
    func cap(_ s: AgentSource) -> AttentionCap {
        let c = AttentionCap(rawValue: string(s.keyPrefix + "cap")) ?? (s == .agentRoster ? .info : .attention)
        if s == .samantha { return c.rank < AttentionCap.attention.rank ? .attention : c }
        return c == .ambient ? .info : c
    }
    func mayRaiseApproval(_ s: AgentSource) -> Bool {
        s.canDecide && sourceEnabled(s) && bool(s.keyPrefix + "canRaiseApprovals")  // approvals are needs-you: independent of the alert level
    }

    func sourceControlEnabled(_ key: String) -> Bool {
        guard let s = AgentSource.allCases.first(where: { key.hasPrefix($0.keyPrefix) }) else { return true }
        if key.hasSuffix(".enabled") { return true }
        if !sourceEnabled(s) { return false }
        if key.hasSuffix(".canRaiseApprovals") { return s.canDecide }
        return true
    }

    func restoreAgentDefaults() {
        let keys = IslandSettingsSchema.sourceSettings.map(\.key) + [
            IslandSettingsSchema.alertDuration, IslandSettingsSchema.promptSound,
            IslandSettingsSchema.respectFocus, IslandSettingsSchema.hideWhileSharing,
            IslandSettingsSchema.hideInFullscreen, IslandSettingsSchema.hideFromScreenCapture
        ]
        objectWillChange.send()
        for key in keys { defaults.removeObject(forKey: key) }
        for o in observers { o(self) }
    }

    func displayOptions() -> [(value: String, label: String)] {
        let connected = screens()
        var options = [(value: "main", label: "Display with menu bar")]
        if connected.contains(where: \.builtIn) || string(IslandSettingsSchema.displayOn) == "builtIn" {
            options.append(("builtIn", "Built-in display"))
        }
        if connected.count > 1 {
            options += connected.map { ("selected:" + $0.uuid, $0.name) }
        }
        return options
    }

    func displaySelection() -> String {
        let choice = string(IslandSettingsSchema.displayOn)
        guard choice == "selected" else { return choice }
        let selected = "selected:" + string(IslandSettingsSchema.selectedDisplay)
        return displayOptions().contains { $0.value == selected } ? selected : "main"
    }

    func seedLoginItem() {
        let key = IslandSettingsSchema.openAtLogin + ".seeded"
        guard defaults.object(forKey: key) == nil else { return }
        if loginItem.status == .notRegistered { updateLoginItem(true) }
        defaults.set(true, forKey: key)
    }

    private func updateLoginItem(_ on: Bool) {
        do {
            if on { try loginItem.register() } else { try loginItem.unregister() }
            if on && loginItem.status == .requiresApproval { loginItem.openSettings() }
        } catch {
            SamanthaActivityLog.startup(phase: "login_item", status: "error", detail: on ? "Registration failed" : "Unregistration failed")
        }
    }

    /// Rotate: new 32-byte base64url token, the old one is replaced immediately.
    /// Never returns or logs the secret; Copy is the only way out (concealed, auto-cleared).
    func rotateToken(_ s: AgentSource) throws {
        objectWillChange.send()
        try tokens.write(Self.newToken(), source: s.rawValue)
        verifyCache[s] = nil
    }

    /// Create the token only if missing (called from a user click, e.g. turning Samantha on).
    func ensureToken(_ s: AgentSource) {
        guard !tokens.exists(source: s.rawValue) else { return }
        try? rotateToken(s)
    }

    /// Hub request verification: Keychain read cached 30 s per source (Samantha posts at ≤ 10 Hz).
    private var verifyCache: [AgentSource: (token: String?, at: Date)] = [:]
    func tokenForVerification(_ s: AgentSource, now: Date = Date(), refresh: Bool = false) -> String? {
        if !refresh, let c = verifyCache[s], now.timeIntervalSince(c.at) < 30 { return c.token }
        if refresh, let c = verifyCache[s], now.timeIntervalSince(c.at) < 1 { return c.token }  // bound re-reads
        let t = refresh ? tokens.reload(source: s.rawValue) : tokens.read(source: s.rawValue)
        verifyCache[s] = (t, now)
        return t
    }

    /// Copy the token as a concealed pasteboard item and clear it after 60 s if still ours.
    func copyToken(_ s: AgentSource, pasteboard: NSPasteboard = .general) -> Bool {
        guard let t = tokens.read(source: s.rawValue) else { return false }
        pasteboard.clearContents()
        let item = NSPasteboardItem()
        item.setString(t, forType: .string)
        item.setString("", forType: NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType"))
        pasteboard.writeObjects([item])
        let change = pasteboard.changeCount
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) {
            if pasteboard.changeCount == change { pasteboard.clearContents() }
        }
        return true
    }
    /// "••••" + last 4, or nil when there is no token (the row then offers Create token).
    /// Metadata only: is there a token for this source (no Keychain data read).
    func hasToken(_ s: AgentSource) -> Bool { tokens.exists(source: s.rawValue) }
    func maskedToken(_ s: AgentSource) -> String? {
        guard let t = tokens.read(source: s.rawValue), t.count >= 4 else { return nil }
        return "••••" + String(t.suffix(4))
    }
    nonisolated static func newToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
}

// MARK: - Tokens (Keychain service "samantha.island", account = source). Never logged or shown in full.

protocol IslandTokenStore {
    /// Token data. Keychain-backed: read once per process, then served from memory.
    func read(source: String) -> String?
    func write(_ token: String, source: String) throws
    /// Metadata only (no secret, never a Keychain prompt).
    func exists(source: String) -> Bool
    /// Bypass the memory cache (bounded: a hub request failed verification).
    func reload(source: String) -> String?
}

extension IslandTokenStore {
    func exists(source: String) -> Bool { read(source: source) != nil }
    func reload(source: String) -> String? { read(source: source) }
}

struct KeychainTokenStore: IslandTokenStore {
    static let service = "samantha.island"

    static func access(label: String) -> SecAccess? {
        var me: SecTrustedApplication?
        var cli: SecTrustedApplication?
        guard SecTrustedApplicationCreateFromPath(nil, &me) == errSecSuccess, let me,
              SecTrustedApplicationCreateFromPath("/usr/bin/security", &cli) == errSecSuccess, let cli else { return nil }
        var access: SecAccess?
        guard SecAccessCreate(label as CFString, [me, cli] as CFArray, &access) == errSecSuccess else { return nil }
        return access
    }
    /// Per-process token cache: the Keychain data is read once (lazily), so tab switches and
    /// re-renders never raise "wants to use your confidential information".
    private static let cache = OSAllocatedUnfairLock<[String: String?]>(initialState: [:])
    /// Data reads that actually hit the Keychain (tests).
    nonisolated(unsafe) static var dataReads = 0

    func read(source: String) -> String? {
        if let hit = Self.cache.withLock({ $0[source] }) { return hit }
        return reload(source: source)
    }
    func reload(source: String) -> String? {
        let t = Self.readData(source: source)
        Self.cache.withLock { $0[source] = .some(t) }
        if t != nil { KeychainGrant.markRead(KeychainGrant.islandID(source)) }
        return t
    }
    static func readData(source: String) -> String? {
        dataReads += 1
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: source, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }
    func exists(source: String) -> Bool {
        if let hit = Self.cache.withLock({ $0[source] }) { return hit != nil }
        return KeychainGrant.exists([kSecAttrService as String: Self.service, kSecAttrAccount as String: source])
    }
    static func forget() { cache.withLock { $0 = [:] } }
    func write(_ token: String, source: String) throws {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: Self.service,
                                   kSecAttrAccount as String: source]
        let data = Data(token.utf8)
        // Rotate = delete + add so the item always carries the current access list.
        SecItemDelete(base as CFDictionary)
        var status: OSStatus
        do {
            var add = base; add[kSecValueData as String] = data
            add[kSecAttrLabel as String] = "Samantha island token (\(source))"
            // Readable without a prompt by this app and /usr/bin/security (island-post and the voice
            // helper read it in-process via `security find-generic-password -w`).
            if let access = Self.access(label: "Samantha island token") { add[kSecAttrAccess as String] = access }
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            Self.cache.withLock { $0[source] = nil }
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
        Self.cache.withLock { $0[source] = .some(token) }
        KeychainGrant.markRead(KeychainGrant.islandID(source))   // we created it: our ACL, no prompt
    }
}

final class MemoryTokenStore: IslandTokenStore {
    var tokens: [String: String] = [:]
    func read(source: String) -> String? { tokens[source] }
    func write(_ token: String, source: String) throws { tokens[source] = token }
}

// MARK: - Screen choice / environment probes (wired General settings)

enum IslandDisplayChoice {
    /// Pick the screen for the island. "all" is Soon → main.
    static func pick(choice: String, screens: [NSScreen], selectedUUID: String?, main: NSScreen) -> NSScreen {
        switch choice {
        case "builtIn":
            return screens.first { $0.displayID.map { CGDisplayIsBuiltin($0) != 0 } ?? false } ?? main
        case "selected":
            if let u = selectedUUID, let s = screens.first(where: { $0.displayUUID == u }) { return s }
            return main
        default:
            return main
        }
    }
}

extension NSScreen {
    var displayUUID: String {
        guard let id = displayID, let u = CGDisplayCreateUUIDFromDisplayID(id)?.takeRetainedValue() else { return "" }
        return CFUUIDCreateString(nil, u) as String
    }
}

enum IslandEnvironment {
    /// Mission Control: Window Server shows a display-sized shield (`ExposeShieldWindow`, WindowManager,
    /// layer 19) over each display. The Dock's own window is ALSO display-sized at the Dock level (20) and is
    /// on screen whenever the Dock is showing, so it must never count — counting it hid the island for as
    /// long as the Dock was visible (always, with Dock auto-hide off).
    static func missionControlActive() -> Bool {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] else { return false }
        let displays = NSScreen.screens.compactMap { $0.displayID.map(CGDisplayBounds) }
        return missionControlActive(windows: list, displays: displays)
    }

    static let dockWindowLevel = Int(CGWindowLevelForKey(.dockWindow))

    static func missionControlActive(windows: [[String: Any]], displays: [CGRect]) -> Bool {
        windows.contains { w in
            let owner = w[kCGWindowOwnerName as String] as? String ?? ""
            guard owner == "Dock" || owner == "WindowManager",
                  let layer = w[kCGWindowLayer as String] as? Int, layer >= 18, layer < 25,
                  let b = w[kCGWindowBounds as String] as? [String: CGFloat] else { return false }
            if owner == "Dock" && (layer == dockWindowLevel || (w[kCGWindowName as String] as? String) == "Dock") {
                return false  // the Dock itself, shown or revealed: not Mission Control
            }
            let r = CGRect(x: b["X"] ?? -1, y: b["Y"] ?? -1, width: b["Width"] ?? 0, height: b["Height"] ?? 0)
            return displays.contains { abs($0.minX - r.minX) < 2 && abs($0.minY - r.minY) < 2
                && abs($0.width - r.width) < 2 && abs($0.height - r.height) < 2 }
        }
    }
    /// Fullscreen space: no on-screen menubar strip on the island screen AND a normal-layer window
    /// covers the whole display (so an auto-hiding menu bar alone never hides the island).
    static func fullscreenActive(on screen: NSScreen) -> Bool {
        guard MenuBarWindowAnchor.strip(on: screen) == nil, let id = screen.displayID else { return false }
        let d = CGDisplayBounds(id)
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return false }
        return list.contains { w in
            let owner = w[kCGWindowOwnerName as String] as? String ?? ""
            guard (w[kCGWindowLayer as String] as? Int) == 0,
                  !["Window Server", "Dock", "Finder"].contains(owner),
                  (w[kCGWindowAlpha as String] as? Double ?? 1) > 0.01,
                  let b = w[kCGWindowBounds as String] as? [String: CGFloat] else { return false }
            return abs((b["X"] ?? -1) - d.minX) < 1 && abs((b["Y"] ?? -1) - d.minY) < 1
                && abs((b["Width"] ?? 0) - d.width) < 1 && abs((b["Height"] ?? 0) - d.height) < 1
        }
    }
}
