import AppKit
import Foundation
import IOKit.ps
import Security

// Island Settings (Alcove parity + AI Agents). Schema-driven: every row in the window is a
// `SettingDef`, so the window, persistence and tests share one source of truth.
// Alcove inventory: evidence/p6/alcove-settings/inventory.md.

enum SettingsTab: String, CaseIterable, Identifiable {
    case general, battery, connectivity, focus, display, sound
    case nowPlaying, calendar, fileTray, lockScreen
    case samantha, grokBot, cursor, codex
    case about
    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: return "General"
        case .battery: return "Battery"
        case .connectivity: return "Connectivity"
        case .focus: return "Focus"
        case .display: return "Display"
        case .sound: return "Sound"
        case .nowPlaying: return "Now Playing"
        case .calendar: return "Calendar"
        case .fileTray: return "File Tray"
        case .lockScreen: return "Lock Screen"
        case .samantha: return "Samantha"
        case .grokBot: return "Grok Bot"
        case .cursor: return "Cursor"
        case .codex: return "Codex"
        case .about: return "About"
        }
    }

    /// Sidebar icon: SF Symbol on a coloured rounded square (Alcove style).
    var symbol: String {
        switch self {
        case .general: return "gearshape.fill"
        case .battery: return "bolt.fill"
        case .connectivity: return "headphones"
        case .focus: return "moon.fill"
        case .display: return "sun.max.fill"
        case .sound: return "speaker.wave.2.fill"
        case .nowPlaying: return "play.fill"
        case .calendar: return "calendar"
        case .fileTray: return "tray.fill"
        case .lockScreen: return "lock.fill"
        case .samantha: return "waveform"
        case .grokBot: return "sparkles"
        case .cursor: return "cursorarrow.rays"
        case .codex: return "chevron.left.forwardslash.chevron.right"
        case .about: return "info"
        }
    }

    var tint: NSColor {
        switch self {
        case .general, .about: return NSColor(white: 0.42, alpha: 1)
        case .battery: return NSColor(red: 0.98, green: 0.55, blue: 0.35, alpha: 1)
        case .connectivity: return NSColor(red: 0.20, green: 0.80, blue: 0.62, alpha: 1)
        case .focus: return NSColor(red: 0.42, green: 0.42, blue: 0.95, alpha: 1)
        case .display: return NSColor(red: 0.62, green: 0.45, blue: 0.95, alpha: 1)
        case .sound: return NSColor(red: 0.85, green: 0.40, blue: 0.85, alpha: 1)
        case .nowPlaying: return NSColor(red: 0.96, green: 0.30, blue: 0.40, alpha: 1)
        case .calendar: return NSColor(red: 0.95, green: 0.30, blue: 0.30, alpha: 1)
        case .fileTray: return NSColor(white: 0.55, alpha: 1)
        case .lockScreen: return NSColor(white: 0.12, alpha: 1)
        case .samantha: return NSColor(red: 0.39, green: 0.82, blue: 1.0, alpha: 1)   // #64D2FF
        case .grokBot: return NSColor(white: 0.15, alpha: 1)
        case .cursor: return NSColor(white: 0.22, alpha: 1)
        case .codex: return NSColor(red: 0.10, green: 0.55, blue: 0.45, alpha: 1)
        }
    }

    /// "Soon" = persisted but inert until the §12 phase lands.
    var badge: String? {
        switch self {
        case .battery, .focus, .lockScreen: return "Soon"
        case .nowPlaying where !NowPlayingKeys.live: return "Soon"
        default: return nil
        }
    }

    var agentSource: AgentSource? {
        switch self {
        case .samantha: return .samantha
        case .grokBot: return .grokBot
        case .cursor: return .cursor
        case .codex: return .codex
        default: return nil
        }
    }

    static func sidebar(hasBattery: Bool) -> [(String?, [SettingsTab])] {
        [
            (nil, [.general]),
            ("Notifications", (hasBattery ? [.battery] : []) + [.connectivity, .focus, .display, .sound]),
            ("Live Activities", [.nowPlaying, .calendar, .fileTray, .lockScreen]),
            ("AI Agents", [.samantha, .grokBot, .cursor, .codex]),
            ("Samantha", [.about]),
        ]
    }
}

enum AgentSource: String, CaseIterable, Codable {
    case samantha, grokBot = "grok", cursor, codex
    var title: String {
        switch self {
        case .samantha: return "Samantha"
        case .grokBot: return "Grok Bot"
        case .cursor: return "Cursor"
        case .codex: return "Codex"
        }
    }
    /// Map an island agent name/app ("Codex", "Grok Bot", "Cursor") to a source.
    static func match(_ s: String) -> AgentSource? {
        let l = s.lowercased()
        if l.contains("samantha") { return .samantha }
        if l.contains("grok") || l == "comet" { return .grokBot }  // Grok Bot drives Comet
        if l.contains("cursor") { return .cursor }
        if l.contains("codex") { return .codex }
        return nil
    }
    var keyPrefix: String { "island.agent.\(rawValue)." }
    /// CLI / wire name (island-post --source).
    var cliName: String { self == .grokBot ? "grok-bot" : rawValue }
    var defaultApp: String {
        switch self {
        case .samantha: return ""
        case .grokBot: return "Comet"
        case .cursor: return "Cursor"
        case .codex: return "Codex"
        }
    }
    /// Exact source names accepted on the wire.
    static func parse(_ s: String) -> AgentSource? {
        switch s.lowercased().trimmingCharacters(in: .whitespaces) {
        case "samantha": return .samantha
        case "grok", "grok-bot", "grokbot", "grok_bot": return .grokBot
        case "cursor": return .cursor
        case "codex": return .codex
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
    case token                                            // AI source Keychain token row
    case button(String)                                   // action row

    static func == (a: SettingKind, b: SettingKind) -> Bool {
        switch (a, b) {
        case (.master(let x), .master(let y)): return x == y
        case (.toggle, .toggle), (.token, .token): return true
        case (.slider(let a1, let b1, let c1, let d1), .slider(let a2, let b2, let c2, let d2)):
            return a1 == a2 && b1 == b2 && c1 == c2 && d1 == d2
        case (.tiles(let x), .tiles(let y)): return x.map(\.value) == y.map(\.value)
        case (.popup(let x), .popup(let y)): return x.map(\.value) == y.map(\.value)
        case (.button(let x), .button(let y)): return x == y
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
    /// true = drives real behaviour now; false = persisted but inert ("Soon").
    var wired: Bool
    var info: String? = nil
    /// Only shown when this bool key is on (e.g. Hover duration needs Expand on hover).
    var visibleWhen: String? = nil
    var id: String { key }
}

enum IslandSettingsSchema {
    // Keys used by wiring code.
    static let hideInFullscreen = "island.general.hideInFullscreen"
    static let hideInMissionControl = "island.general.hideInMissionControl"
    static let hideFromScreenCapture = "island.general.hideFromScreenCapture"
    static let forceSimulatedNotch = "island.general.forceSimulatedNotch"
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

    static let all: [SettingDef] = general + battery + connectivity + focus + display + sound
        + nowPlaying + calendar + fileTray + lockScreen + AgentSource.allCases.flatMap(agent) + about

    static let general: [SettingDef] = [
        .init(tab: .general, section: nil, key: showIsland, label: "Show Dynamic Island", kind: .toggle, defaultValue: .bool(true), wired: true,
              info: "Also in the menu bar menu"),
        .init(tab: .general, section: nil, key: "island.general.launchAtLogin", label: "Launch at login", kind: .toggle,
              defaultValue: .bool(true), wired: false, info: "CUAService is started by launchd"),
        .init(tab: .general, section: nil, key: hideInFullscreen, label: "Hide in fullscreen", kind: .toggle, defaultValue: .bool(true), wired: true),
        .init(tab: .general, section: nil, key: hideInMissionControl, label: "Hide in mission control", kind: .toggle, defaultValue: .bool(true), wired: true),
        .init(tab: .general, section: nil, key: "island.general.hideWhileGaming", label: "Hide while gaming", kind: .toggle, defaultValue: .bool(false), wired: false),
        .init(tab: .general, section: nil, key: hideFromScreenCapture, label: "Hide from screen capture", kind: .toggle, defaultValue: .bool(false), wired: true),
        .init(tab: .general, section: nil, key: hotKey, label: "Keyboard shortcut \(IslandHotKey.display)", kind: .toggle, defaultValue: .bool(true), wired: true,
              info: "Opens the island with keyboard focus: arrows or Tab move, Return activates, Esc closes. If another app owns the shortcut it stays off (see the log)"),
        .init(tab: .general, section: nil, key: forceSimulatedNotch, label: "Force simulated notch", kind: .toggle, defaultValue: .bool(false), wired: false,
              info: "Displays without a notch always use the simulated notch"),
        .init(tab: .general, section: nil, key: displayOn, label: "Display on",
              kind: .popup([("main", "Main display"), ("all", "All displays"), ("builtIn", "Built-in display")]),
              defaultValue: .string("main"), wired: true, info: "All displays: Soon (falls back to main)"),
        .init(tab: .general, section: "Idle Activity", key: "island.general.mostRecent", label: "Most Recent", kind: .toggle, defaultValue: .bool(false), wired: false),
        .init(tab: .general, section: "Idle Activity", key: idleActivity, label: "Idle activity",
              kind: .tiles([t("none", "None", "nosign"), t("samantha", "Samantha", "circle.circle.fill"),
                            t("agents", "Agents", "person.2.fill"), t("duo", "Duo", "rectangle.split.2x1.fill")]),
              defaultValue: .string("duo"), wired: true),
        .init(tab: .general, section: "Behaviour", key: contrastOutline, label: "Contrast outline", kind: .toggle, defaultValue: .bool(true), wired: true),
        .init(tab: .general, section: "Behaviour", key: "island.general.progressiveBlur", label: "Progressive blur", kind: .toggle, defaultValue: .bool(true), wired: false),
        .init(tab: .general, section: "Behaviour", key: haptics, label: "Haptic feedback", kind: .toggle, defaultValue: .bool(true), wired: true,
              info: "Trackpad tick on swipe thresholds and drop targets"),
        .init(tab: .general, section: "Behaviour", key: mutePromptSound, label: "Mute agent prompt sound", kind: .toggle, defaultValue: .bool(false), wired: true,
              info: "Soft Tink when an agent asks you something (only when its app isn't frontmost)"),
        .init(tab: .general, section: "Behaviour", key: expandOnHover, label: "Expand on hover", kind: .toggle, defaultValue: .bool(false), wired: true,
              info: "Opens the card on hover instead of click"),
        .init(tab: .general, section: "Behaviour", key: hoverDuration, label: "Hover duration", kind: .slider(min: 0, max: 0.6, step: 0.1, unit: "s"),
              defaultValue: .double(0), wired: true, visibleWhen: expandOnHover),
        .init(tab: .general, section: "Durations", key: resultPeek, label: "Result peek", kind: .slider(min: 2, max: 8, step: 0.5, unit: "s"),
              defaultValue: .double(4), wired: true, info: "Done card auto-peek when there is a result"),
        .init(tab: .general, section: "Durations", key: errorPeek, label: "Error peek", kind: .slider(min: 3, max: 10, step: 0.5, unit: "s"),
              defaultValue: .double(6), wired: true),
        .init(tab: .general, section: "Durations", key: doneSettle, label: "Done settles after", kind: .slider(min: 2, max: 8, step: 0.5, unit: "s"),
              defaultValue: .double(4), wired: true),
        .init(tab: .general, section: "Gestures", key: naturalMovement, label: "Natural movement", kind: .toggle, defaultValue: .bool(false), wired: true),
        .init(tab: .general, section: "Gestures", key: swipeToToggle, label: "Swipe to toggle open", kind: .toggle, defaultValue: .bool(true), wired: true,
              info: "Swipe down over the island opens the card, up closes it"),
        .init(tab: .general, section: "Gestures", key: swipeToCycle, label: "Swipe to switch activity", kind: .toggle, defaultValue: .bool(true), wired: true,
              info: "Two-finger swipe sideways swaps the two live activities (Duo)"),
        .init(tab: .general, section: "Gestures", key: "island.general.swipeToDismiss", label: "Swipe to dismiss activity", kind: .toggle, defaultValue: .bool(true), wired: false,
              info: "Swipe vertically to toggle activity"),
        .init(tab: .general, section: "Gestures", key: "island.general.swipeToSkip", label: "Swipe to skip media", kind: .toggle, defaultValue: .bool(true), wired: false),
    ]

    static func hud(_ tab: SettingsTab, _ name: String, styles: [(String, String, String)], extra: [SettingDef] = []) -> [SettingDef] {
        let p = "island.\(tab.rawValue)."
        return [
            .init(tab: tab, section: nil, key: p + "enabled", label: name, kind: .master(tab.symbol), defaultValue: .bool(true), wired: true,
                  info: tab == .display ? "Built-in displays only; external display keys pass through" : "Replaces the system volume HUD (needs Accessibility)"),
            .init(tab: tab, section: nil, key: p + "duration", label: "Duration", kind: .slider(min: 1, max: 2, step: 0.1, unit: "s"), defaultValue: .double(1.2), wired: true),
            .init(tab: tab, section: nil, key: p + "style", label: "Progress style", kind: .tiles(styles.map { t($0.0, $0.1, $0.2) }), defaultValue: .string("white"), wired: false),
            .init(tab: tab, section: nil, key: p + "showPercentage", label: "Show percentage", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: tab, section: nil, key: p + "hideLabel", label: "Hide label", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: tab, section: nil, key: p + "speed", label: "Speed",
                  kind: .tiles([t("smooth", "Smooth", "scribble"), t("fast", "Fast", "hare.fill"), t("instant", "Instant", "bolt.fill")]),
                  defaultValue: .string("fast"), wired: true),
        ] + extra
    }

    static let battery: [SettingDef] = {
        let p = "island.battery."
        return [
            .init(tab: .battery, section: nil, key: p + "enabled", label: "Battery", kind: .master("bolt.fill"), defaultValue: .bool(true), wired: false),
            .init(tab: .battery, section: nil, key: p + "hidePercentage", label: "Hide percentage", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .battery, section: nil, key: p + "hideLabel", label: "Hide label", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .battery, section: nil, key: p + "notifyLowPower", label: "Notify on low power mode", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .battery, section: nil, key: p + "warnLow", label: "Warn on low battery", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .battery, section: nil, key: p + "lowThreshold", label: "Low battery threshold", kind: .slider(min: 10, max: 30, step: 1, unit: "%"), defaultValue: .double(20), wired: false),
            .init(tab: .battery, section: nil, key: p + "playSound", label: "Play sound on low battery", kind: .toggle, defaultValue: .bool(true), wired: false),
        ]
    }()

    static let connectivity: [SettingDef] = {
        let p = "island.connectivity."
        return [
            .init(tab: .connectivity, section: nil, key: p + "enabled", label: "Connectivity", kind: .master("headphones"), defaultValue: .bool(true), wired: true,
                  info: "Quick Peek when the audio output changes"),
            .init(tab: .connectivity, section: nil, key: p + "agentPeeks", label: "Agent and app peeks", kind: .toggle, defaultValue: .bool(true), wired: true,
                  info: "One-line peeks posted with island-post --kind peek"),
            .init(tab: .connectivity, section: nil, key: p + "hideWhileSharing", label: "Hide peeks while screen sharing", kind: .toggle, defaultValue: .bool(true), wired: true,
                  info: "Peeks are kept out of screen captures; during a macOS Screen Sharing session they are dropped. Other apps' screen sharing cannot be detected, so turn on Hide from screen capture for those"),
            .init(tab: .connectivity, section: nil, key: p + "respectFocus", label: "Silence peeks during Focus", kind: .toggle, defaultValue: .bool(true), wired: true,
                  info: "Needs Focus status access (below); approvals still show"),
            .init(tab: .connectivity, section: nil, key: p + "focusAccess", label: "Focus status access", kind: .button("Allow Focus Status…"), defaultValue: .none, wired: true,
                  info: "Asks macOS once; manage later in System Settings → Privacy → Focus"),
            .init(tab: .connectivity, section: nil, key: p + "duration", label: "Peek duration", kind: .slider(min: 2.5, max: 6, step: 0.5, unit: "s"), defaultValue: .double(3), wired: true),
            .init(tab: .connectivity, section: nil, key: p + "warnLow", label: "Warn on low battery", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .connectivity, section: nil, key: p + "lowThreshold", label: "Low battery threshold", kind: .slider(min: 10, max: 30, step: 1, unit: "%"), defaultValue: .double(20), wired: false),
            .init(tab: .connectivity, section: nil, key: p + "playSound", label: "Play sound on low battery", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .connectivity, section: nil, key: p + "model", label: "Device glyph",
                  kind: .tiles([t("threeDimensional", "3D models", "airpodspro"), t("symbol", "Symbols", "airpods")]),
                  defaultValue: .string("threeDimensional"), wired: false),
        ]
    }()

    static let focus: [SettingDef] = {
        let p = "island.focus."
        return [
            .init(tab: .focus, section: nil, key: p + "enabled", label: "Focus", kind: .master("moon.fill"), defaultValue: .bool(true), wired: false),
            .init(tab: .focus, section: nil, key: p + "duration", label: "Duration", kind: .slider(min: 2.5, max: 6, step: 0.5, unit: "s"), defaultValue: .double(5), wired: false),
            .init(tab: .focus, section: nil, key: p + "playSoundSleep", label: "Play sound on sleep focus", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .focus, section: nil, key: p + "replaceWaveform", label: "Replace waveform during focus", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .focus, section: nil, key: p + "replaceWhen", label: "When focus is", kind: .popup([("any", "Any")]), defaultValue: .string("any"),
                  wired: false, visibleWhen: p + "replaceWaveform"),
            .init(tab: .focus, section: nil, key: p + "hideLabel", label: "Hide label", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .focus, section: "Allowed Notifications", key: p + "allowBattery", label: "Battery", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .focus, section: "Allowed Notifications", key: p + "allowConnectivity", label: "Connectivity", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .focus, section: "Allowed Notifications", key: p + "allowDisplay", label: "Display", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .focus, section: "Allowed Notifications", key: p + "allowSound", label: "Sound", kind: .toggle, defaultValue: .bool(true), wired: false),
            .init(tab: .focus, section: "Allowed Notifications", key: p + "allowAgents", label: "AI agents", kind: .toggle, defaultValue: .bool(true), wired: false),
        ]
    }()

    static let display = hud(.display, "Display", styles: [("white", "White", "minus"), ("accent", "Accent", "minus"), ("glow", "Glow", "sparkle")])
    static let sound = hud(.sound, "Sound", styles: [("white", "White", "minus"), ("accent", "Accent", "minus"), ("decibel", "Decibel", "chart.bar.fill")],
                           extra: [.init(tab: .sound, section: nil, key: "island.sound.output", label: "Output",
                                         kind: .tiles([t("speaker", "Show symbol", "speaker.wave.2.fill"), t("device", "Show device", "airpodspro")]),
                                         defaultValue: .string("device"), wired: false)])

    static let nowPlaying: [SettingDef] = {
        let p = "island.nowPlaying."
        return [
            .init(tab: .nowPlaying, section: nil, key: p + "enabled", label: "Now Playing", kind: .master("play.fill"), defaultValue: .bool(true), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "idleDuration", label: "Idle duration", kind: .slider(min: 0, max: 10, step: 1, unit: "s"), defaultValue: .double(3), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "badge", label: "Title badge",
                  kind: .tiles([t("none", "None", "nosign"), t("explicit", "Explicit", "e.square.fill"), t("format", "Format", "hifispeaker.fill")]),
                  defaultValue: .string("explicit"), wired: false,
                  info: "Soon — the system now-playing feed has no explicit/format flag"),
            .init(tab: .nowPlaying, section: nil, key: p + "color", label: "Progress colour",
                  kind: .tiles([t("mono", "Monochrome", "minus"), t("color", "Colored", "minus"), t("gradient", "Gradient", "minus")]),
                  defaultValue: .string("mono"), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "liveWaveform", label: "Use live waveform", kind: .toggle, defaultValue: .bool(false), wired: false,
                  info: "Soon — needs audio capture permission; synthetic waveform for now"),
            .init(tab: .nowPlaying, section: nil, key: p + "disableCompactWaveform", label: "Disable compact waveform", kind: .toggle, defaultValue: .bool(false), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "disableArtworkFlip", label: "Disable artwork flip", kind: .toggle, defaultValue: .bool(false), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "hideWhileSourceActive", label: "Hide while source app is active", kind: .toggle, defaultValue: .bool(false), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "hideTitleExtras", label: "Hide media title extras", kind: .toggle, defaultValue: .bool(false), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: nil, key: p + "action", label: "Action button",
                  kind: .tiles([t("favorite", "Like", "star.fill"), t("repeat", "Repeat", "repeat"), t("copy", "Copy", "link")]),
                  defaultValue: .string("favorite"), wired: NowPlayingKeys.live,
                  info: "Like shows only when the source supports it (Music)"),
            .init(tab: .nowPlaying, section: "Source", key: p + "automaticSource", label: "Automatic", kind: .toggle, defaultValue: .bool(true), wired: NowPlayingKeys.live),
            .init(tab: .nowPlaying, section: "Source", key: p + "app", label: "App", kind: .popup([("system", "System"), ("music", "Music"), ("spotify", "Spotify")]),
                  defaultValue: .string("music"), wired: NowPlayingKeys.live, info: "Used when Automatic is off"),
        ]
    }()

    static let calendar: [SettingDef] = {
        let p = "island.calendar."
        return [
            .init(tab: .calendar, section: nil, key: p + "enabled", label: "Calendar", kind: .master("calendar"), defaultValue: .bool(true), wired: true,
                  info: "Read-only: the island never edits your calendars"),
            .init(tab: .calendar, section: nil, key: calendarAccess, label: "Calendar access", kind: .button("Grant Access…"), defaultValue: .none, wired: true,
                  info: "Asks macOS once; manage later in System Settings → Privacy → Calendars"),
            .init(tab: .calendar, section: nil, key: p + "timeBefore", label: "Time before event", kind: .slider(min: 5, max: 30, step: 5, unit: "m"), defaultValue: .double(10), wired: true),
            .init(tab: .calendar, section: nil, key: p + "playSoundEvent", label: "Play sound on calendar event", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .calendar, section: nil, key: p + "hourlyChime", label: "Play hourly chime", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .calendar, section: nil, key: p + "useCalendarColor", label: "Use calendar color", kind: .toggle, defaultValue: .bool(false), wired: true),
            .init(tab: .calendar, section: nil, key: p + "disableDuringEvents", label: "Disable activity during events", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .calendar, section: nil, key: p + "weatherOnEmptyDay", label: "Show weather on empty day", kind: .toggle, defaultValue: .bool(false), wired: false,
                  info: "Needs location"),
            .init(tab: .calendar, section: nil, key: p + "eventIndicators", label: "Use event indicators", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .calendar, section: nil, key: p + "monthView", label: "Always show month view", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .calendar, section: nil, key: p + "timeToLeave", label: "Notify when time to leave", kind: .toggle, defaultValue: .bool(false), wired: false,
                  info: "Needs location"),
            .init(tab: .calendar, section: "Calendars", key: p + "calendarsMode", label: "All calendars",
                  kind: .popup([("both", "Both"), ("activity", "Live activity"), ("expanded", "Expanded view")]),
                  defaultValue: .string("both"), wired: false, info: "Per-calendar rows appear once Calendar access is granted"),
        ]
    }()

    static let fileTray: [SettingDef] = [
        .init(tab: .fileTray, section: nil, key: fileTrayEnabled, label: "File Tray", kind: .master("tray.fill"), defaultValue: .bool(true), wired: true,
              info: "Drag files to the island to park them (references, never copies)"),
    ]

    static let lockScreen: [SettingDef] = {
        let p = "island.lockScreen."
        func allow(_ k: String, _ l: String) -> SettingDef {
            .init(tab: .lockScreen, section: "Allowed Notifications", key: p + "allowLocked." + k, label: l, kind: .toggle, defaultValue: .bool(true), wired: false)
        }
        func widget(_ k: String, _ l: String, _ d: Bool) -> SettingDef {
            .init(tab: .lockScreen, section: "Widgets", key: p + "widget." + k, label: l, kind: .toggle, defaultValue: .bool(d), wired: false)
        }
        return [
            .init(tab: .lockScreen, section: nil, key: p + "enabled", label: "Lock Screen", kind: .master("lock.fill"), defaultValue: .bool(true), wired: false),
            .init(tab: .lockScreen, section: nil, key: p + "keepAwake", label: "Keep awake", kind: .slider(min: 0, max: 30, step: 1, unit: "m"), defaultValue: .double(0), wired: false),
            .init(tab: .lockScreen, section: nil, key: p + "showOnScreenSaver", label: "Show on screen saver", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .lockScreen, section: nil, key: p + "soundLock", label: "Play sound on lock", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .lockScreen, section: nil, key: p + "soundUnlock", label: "Play sound on unlock", kind: .toggle, defaultValue: .bool(true), wired: false),
            widget("battery", "Battery", true), widget("connectivity", "Connectivity", true), widget("focus", "Focus", true),
            widget("calendar", "Calendar", false), widget("nowPlaying", "Now Playing", true), widget("agents", "AI agents", false),
            .init(tab: .lockScreen, section: "Widgets", key: p + "nowPlayingStyle", label: "Now Playing style",
                  kind: .tiles([t("glass", "Glass", "capsule.fill"), t("clear", "Clear", "capsule")]), defaultValue: .string("glass"), wired: false),
            .init(tab: .lockScreen, section: "Widgets", key: p + "widgetIdle", label: "Idle duration", kind: .slider(min: 0, max: 15, step: 1, unit: "m"), defaultValue: .double(10), wired: false),
            .init(tab: .lockScreen, section: "Widgets", key: p + "showVolumeControl", label: "Show volume control", kind: .toggle, defaultValue: .bool(false), wired: false),
            .init(tab: .lockScreen, section: "Widgets", key: p + "offset", label: "Offset tuning", kind: .slider(min: -50, max: 50, step: 1, unit: "px"), defaultValue: .double(0), wired: false),
            .init(tab: .lockScreen, section: "Widgets", key: p + "weather", label: "Weather", kind: .toggle, defaultValue: .bool(false), wired: false, info: "Needs location"),
            allow("battery", "Battery"), allow("connectivity", "Connectivity"), allow("focus", "Focus"), allow("display", "Display"), allow("sound", "Sound"),
        ]
    }()

    static func agent(_ s: AgentSource) -> [SettingDef] {
        let tab: SettingsTab = s == .samantha ? .samantha : s == .grokBot ? .grokBot : s == .cursor ? .cursor : .codex
        let p = s.keyPrefix
        let on = s != .codex
        let cap: AttentionCap = s == .samantha ? .critical : s == .codex ? .info : .attention
        var rows: [SettingDef] = [
            .init(tab: tab, section: nil, key: p + "enabled", label: s.title, kind: .master(tab.symbol), defaultValue: .bool(on), wired: true,
                  info: s == .samantha ? "Built-in" : "Off hides this source's activities and approvals"),
            .init(tab: tab, section: nil, key: p + "cap", label: "Attention cap",
                  kind: .tiles([t("ambient", "Ambient", "circle.dotted"), t("info", "Info", "info.circle"),
                                t("attention", "Attention", "exclamationmark.circle"), t("critical", "Critical", "exclamationmark.octagon")]),
                  defaultValue: .string(cap.rawValue), wired: true, info: "Attention or higher may raise cards and the needs-you badge"),
            .init(tab: tab, section: nil, key: p + "canRaiseApprovals", label: "Can raise approval cards", kind: .toggle,
                  defaultValue: .bool(s != .codex), wired: true),
        ]
        rows.append(.init(tab: tab, section: "Access", key: p + "token", label: "Token", kind: .token, defaultValue: .none, wired: true,
                          info: s == .samantha ? "Created when you turn Samantha on · samantha.island / samantha"
                                               : "island-post --source \(s.cliName) · samantha.island / \(s.rawValue)"))
        rows += [
            .init(tab: tab, section: "Durations", key: p + "needsYouPeek", label: "Needs-you peek", kind: .slider(min: 2.5, max: 10, step: 0.5, unit: "s"),
                  defaultValue: .double(5), wired: false),
            .init(tab: tab, section: "Durations", key: p + "donePeek", label: "Result dwell", kind: .slider(min: 2, max: 8, step: 0.5, unit: "s"),
                  defaultValue: .double(4), wired: false),
            .init(tab: tab, section: "Durations", key: p + "errorPeek", label: "Error peek", kind: .slider(min: 3, max: 10, step: 0.5, unit: "s"),
                  defaultValue: .double(6), wired: false),
        ]
        if s == .samantha {
            rows.append(.init(tab: tab, section: "Voice", key: "island.agent.samantha.voiceSettings", label: "Model, voice and mic",
                              kind: .button("Voice Settings…"), defaultValue: .none, wired: true))
        }
        return rows
    }

    static let about: [SettingDef] = [
        .init(tab: .about, section: nil, key: "island.about.autoUpdate", label: "Automatically check for updates", kind: .toggle, defaultValue: .bool(true), wired: false),
        .init(tab: .about, section: nil, key: "island.about.openLog", label: "Activity log", kind: .button("Show in Finder"), defaultValue: .none, wired: true),
    ]

    static func defs(for tab: SettingsTab) -> [SettingDef] { all.filter { $0.tab == tab } }
    static func def(_ key: String) -> SettingDef? { all.first { $0.key == key } }

    /// Battery tab only on Macs with an internal battery.
    static var hasInternalBattery: Bool {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef] else { return false }
        return list.contains { src in
            guard let d = IOPSGetPowerSourceDescription(info, src)?.takeUnretainedValue() as? [String: Any] else { return false }
            return (d[kIOPSTypeKey] as? String) == kIOPSInternalBatteryType
        }
    }
}

// MARK: - Store

@MainActor
final class IslandSettingsStore: ObservableObject {
    static let shared = IslandSettingsStore(defaults: .standard)
    let defaults: UserDefaults
    private(set) var tokens: IslandTokenStore

    init(defaults: UserDefaults, tokens: IslandTokenStore = KeychainTokenStore()) {
        self.defaults = defaults
        self.tokens = tokens
    }

    func bool(_ key: String) -> Bool {
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
        if let s = defaults.string(forKey: key) { return s }
        if case .string(let s)? = IslandSettingsSchema.def(key)?.defaultValue { return s }
        return ""
    }
    func set(_ value: Any, for key: String) {
        objectWillChange.send()
        defaults.set(value, forKey: key)
        for o in observers { o(self) }
    }
    private var observers: [(IslandSettingsStore) -> Void] = []
    func addObserver(_ o: @escaping (IslandSettingsStore) -> Void) { observers.append(o) }

    // Agent gating (§10/§11).
    func sourceEnabled(_ s: AgentSource) -> Bool { bool(s.keyPrefix + "enabled") }
    func cap(_ s: AgentSource) -> AttentionCap {
        let key = s.keyPrefix + "cap"
        if let c = AttentionCap(rawValue: string(key)) { return c }
        if case .string(let d)? = IslandSettingsSchema.def(key)?.defaultValue, let c = AttentionCap(rawValue: d) { return c }
        return .info
    }
    func mayRaiseApproval(_ s: AgentSource) -> Bool {
        sourceEnabled(s) && bool(s.keyPrefix + "canRaiseApprovals") && cap(s).rank >= AttentionCap.attention.rank
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
        guard tokens.read(source: s.rawValue) == nil else { return }
        try? rotateToken(s)
    }

    /// Hub request verification: Keychain read cached 30 s per source (Samantha posts at ≤ 10 Hz).
    private var verifyCache: [AgentSource: (token: String?, at: Date)] = [:]
    func tokenForVerification(_ s: AgentSource, now: Date = Date(), refresh: Bool = false) -> String? {
        if !refresh, let c = verifyCache[s], now.timeIntervalSince(c.at) < 30 { return c.token }
        if refresh, let c = verifyCache[s], now.timeIntervalSince(c.at) < 1 { return c.token }  // bound re-reads
        let t = tokens.read(source: s.rawValue)
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
    func maskedToken(_ s: AgentSource) -> String {
        guard let t = tokens.read(source: s.rawValue), t.count >= 3 else { return "Not set" }
        return "••••" + String(t.suffix(3))
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
    func read(source: String) -> String?
    func write(_ token: String, source: String) throws
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
    func read(source: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: Self.service,
                                kSecAttrAccount as String: source, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }
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
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
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
    /// Mission Control: the Dock shows full-screen windows at layer ≥ 18 covering a display.
    static func missionControlActive() -> Bool {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] else { return false }
        let displays = NSScreen.screens.compactMap { $0.displayID.map(CGDisplayBounds) }
        return list.contains { w in
            guard (w[kCGWindowOwnerName as String] as? String) == "Dock",
                  let layer = w[kCGWindowLayer as String] as? Int, layer >= 18, layer < 25,
                  let b = w[kCGWindowBounds as String] as? [String: CGFloat] else { return false }
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
