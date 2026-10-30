import AppKit
import SwiftUI

// MARK: - Wire format

struct IslandAgent: Codable, Equatable, Identifiable {
    var id: String = ""
    var name: String = ""      // "Codex"
    var app: String = ""       // app whose icon represents the agent ("Cursor", "Codex")
    var title: String = ""     // what it's doing
    var status: String = ""    // running | done | error | needs_you | process (local PID; task activity unknown)
    var needs_you: Bool = false
    var elapsed_s: Double = 0
    /// Fully qualified source:id of the registered parent task, when this is a subagent.
    var parent_id: String? = nil
    /// Controls the posting agent honours ("stop", "retry"); the island only offers these.
    var controls: [String] = []
    /// Hub-owned metadata used by request rows and control-failure recovery.
    var request_kind: String? = nil
    var destructive = false
    var last_seen_at: Double = 0

    init(id: String = "", name: String = "", app: String = "", title: String = "", status: String = "",
         needs_you: Bool = false, elapsed_s: Double = 0, parent_id: String? = nil, controls: [String] = [],
         request_kind: String? = nil, destructive: Bool = false, last_seen_at: Double = 0) {
        self.id = id; self.name = name; self.app = app; self.title = title
        self.status = status; self.needs_you = needs_you; self.elapsed_s = elapsed_s
        self.parent_id = parent_id; self.controls = controls
        self.request_kind = request_kind; self.destructive = destructive; self.last_seen_at = last_seen_at
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        app = try c.decodeIfPresent(String.self, forKey: .app) ?? ""
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? ""
        needs_you = try c.decodeIfPresent(Bool.self, forKey: .needs_you) ?? false
        elapsed_s = try c.decodeIfPresent(Double.self, forKey: .elapsed_s) ?? 0
        parent_id = try c.decodeIfPresent(String.self, forKey: .parent_id)
        controls = try c.decodeIfPresent([String].self, forKey: .controls) ?? []
        request_kind = try c.decodeIfPresent(String.self, forKey: .request_kind)
        destructive = try c.decodeIfPresent(Bool.self, forKey: .destructive) ?? false
        last_seen_at = try c.decodeIfPresent(Double.self, forKey: .last_seen_at) ?? 0
    }
}

struct IslandChoiceOption: Codable, Equatable, Hashable {
    var label: String
    var value: String
}

/// An approval or choice request from another agent (Grok Bot/Cursor/Codex). `kind == "choice"`:
/// a prompt with up to 6 options (single or multi), optional free text; shown as a compact needs-you
/// signal and opened by a click. `kind == "approval"`: the classic Deny/Approve card (sticky).
struct IslandAgentApproval: Codable, Equatable {
    var id: String = ""
    var agent: String = ""
    var app: String = ""
    var title: String = ""
    var subtitle: String = ""
    var destructive: Bool = false
    var kind: String = "approval"
    var options: [IslandChoiceOption] = []
    var multi: Bool = false
    var allow_text: Bool = false
    var primary: String = ""
    var danger: String = ""
    /// Epoch seconds the hub expires the card (0 = unknown). Drives the final-60 s ring and T-60 peek.
    var expires_at: Double = 0

    var isChoice: Bool { kind == "choice" }
    var expiresDate: Date? { expires_at > 0 ? Date(timeIntervalSince1970: expires_at) : nil }

    init(id: String = "", agent: String = "", app: String = "", title: String = "", subtitle: String = "",
         destructive: Bool = false, kind: String = "approval", options: [IslandChoiceOption] = [],
         multi: Bool = false, allow_text: Bool = false, primary: String = "", danger: String = "") {
        self.id = id; self.agent = agent; self.app = app; self.title = title
        self.subtitle = subtitle; self.destructive = destructive
        self.kind = kind; self.options = options; self.multi = multi; self.allow_text = allow_text
        self.primary = primary; self.danger = danger
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        agent = try c.decodeIfPresent(String.self, forKey: .agent) ?? ""
        app = try c.decodeIfPresent(String.self, forKey: .app) ?? ""
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        subtitle = try c.decodeIfPresent(String.self, forKey: .subtitle) ?? ""
        destructive = try c.decodeIfPresent(Bool.self, forKey: .destructive) ?? false
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "approval"
        options = (try? c.decodeIfPresent([IslandChoiceOption].self, forKey: .options)) ?? []
        multi = try c.decodeIfPresent(Bool.self, forKey: .multi) ?? false
        allow_text = try c.decodeIfPresent(Bool.self, forKey: .allow_text) ?? false
        primary = try c.decodeIfPresent(String.self, forKey: .primary) ?? ""
        danger = try c.decodeIfPresent(String.self, forKey: .danger) ?? ""
        expires_at = try c.decodeIfPresent(Double.self, forKey: .expires_at) ?? 0
    }
}

/// A card that timed out without an answer: 4 s notice, then a hollow red ring on its source's circle
/// until the user opens the agent list or the agent clears it (never dropped silently).
struct IslandExpired: Codable, Equatable {
    var id: String = ""
    var agent: String = ""
    var app: String = ""
    var title: String = ""
    var at: Double = 0
}

struct IslandSnapshot: Codable, Equatable {
    var kind: String = "idle"
    var title: String = ""
    var detail: String = ""
    var app: String = ""
    var step: String = ""
    var confirm_id: String = ""
    var confirm_prompt: String = ""
    var active_apps: [String] = []
    var expanded: Bool = false
    var step_index: Int = 0
    var step_total: Int = 0
    var agents: [IslandAgent] = []
    var agent_approval: IslandAgentApproval?
    var voice_side: String = "idle"
    var voice_level: Double = 0
    var voice_levels: [Double] = []
    /// Approval/choice cards waiting (hub queue, arrival order); the agent circle shows the count.
    var pending_count: Int = 0
    /// Timed-out cards not yet acknowledged (newest last).
    var expired: [IslandExpired] = []

    init(
        kind: String = "idle", title: String = "", detail: String = "", app: String = "", step: String = "",
        confirm_id: String = "", confirm_prompt: String = "", active_apps: [String] = [], expanded: Bool = false,
        step_index: Int = 0, step_total: Int = 0, agents: [IslandAgent] = [], agent_approval: IslandAgentApproval? = nil,
        voice_side: String = "idle", voice_level: Double = 0, voice_levels: [Double] = []
    ) {
        self.kind = kind; self.title = title; self.detail = detail; self.app = app; self.step = step
        self.confirm_id = confirm_id; self.confirm_prompt = confirm_prompt; self.active_apps = active_apps
        self.expanded = expanded; self.step_index = step_index; self.step_total = step_total
        self.agents = agents; self.agent_approval = agent_approval
        self.voice_side = voice_side; self.voice_level = voice_level; self.voice_levels = voice_levels
    }

    /// Lenient SSE decode — missing fields fall back to defaults, unknown keys ignored.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "idle"
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
        app = try c.decodeIfPresent(String.self, forKey: .app) ?? ""
        step = try c.decodeIfPresent(String.self, forKey: .step) ?? ""
        confirm_id = try c.decodeIfPresent(String.self, forKey: .confirm_id) ?? ""
        confirm_prompt = try c.decodeIfPresent(String.self, forKey: .confirm_prompt) ?? ""
        active_apps = try c.decodeIfPresent([String].self, forKey: .active_apps) ?? []
        expanded = try c.decodeIfPresent(Bool.self, forKey: .expanded) ?? false
        step_index = try c.decodeIfPresent(Int.self, forKey: .step_index) ?? 0
        step_total = try c.decodeIfPresent(Int.self, forKey: .step_total) ?? 0
        agents = (try? c.decodeIfPresent([IslandAgent].self, forKey: .agents)) ?? []
        agent_approval = try? c.decodeIfPresent(IslandAgentApproval.self, forKey: .agent_approval)
        if agent_approval?.id.isEmpty ?? true, agent_approval?.title.isEmpty ?? true { agent_approval = nil }
        voice_side = try c.decodeIfPresent(String.self, forKey: .voice_side) ?? "idle"
        voice_level = try c.decodeIfPresent(Double.self, forKey: .voice_level) ?? 0
        voice_levels = try c.decodeIfPresent([Double].self, forKey: .voice_levels) ?? []
        pending_count = try c.decodeIfPresent(Int.self, forKey: .pending_count) ?? 0
        expired = (try? c.decodeIfPresent([IslandExpired].self, forKey: .expired)) ?? []
    }

    /// True when rendering the other snapshot would look identical —
    /// voice meters intentionally do not count as a visual change.
    func sameVisual(as other: IslandSnapshot) -> Bool {
        kind == other.kind && title == other.title && detail == other.detail && app == other.app
            && step == other.step && confirm_id == other.confirm_id && confirm_prompt == other.confirm_prompt
            && active_apps == other.active_apps && expanded == other.expanded
            && step_index == other.step_index && step_total == other.step_total
            && agents == other.agents && agent_approval == other.agent_approval
            && pending_count == other.pending_count && expired == other.expired
    }
}

/// Incremental SSE event extractor — `drain` removes complete events from the
/// buffer and leaves any trailing partial event in place.
enum IslandSSEParser {
    /// True when the byte buffer ends with a blank line ("\n\n"), i.e. a full SSE event.
    /// Decode bytes only at this point so multibyte UTF-8 is never split.
    static func isEventBoundary(_ raw: [UInt8]) -> Bool {
        raw.count >= 2 && raw[raw.count - 1] == 0x0A && raw[raw.count - 2] == 0x0A
    }

    static func drain(_ buffer: inout String) -> [IslandSnapshot] {
        buffer = buffer.replacingOccurrences(of: "\r\n", with: "\n")
        var snapshots: [IslandSnapshot] = []
        while let range = buffer.range(of: "\n\n") {
            let event = String(buffer[..<range.lowerBound])
            buffer.removeSubrange(..<range.upperBound)
            var dataLines: [String] = []
            for line in event.split(separator: "\n", omittingEmptySubsequences: false) {
                guard line.hasPrefix("data:") else { continue }
                var payload = String(line.dropFirst("data:".count))
                if payload.hasPrefix(" ") { payload.removeFirst() }
                dataLines.append(payload)
            }
            guard !dataLines.isEmpty else { continue } // comments like ": ping"
            let json = dataLines.joined(separator: "\n")
            guard let data = json.data(using: .utf8),
                  let snap = try? JSONDecoder().decode(IslandSnapshot.self, from: data) else { continue }
            snapshots.append(snap)
        }
        return snapshots
    }
}

// MARK: - Colour (§2): one accent + three signals, everything else white on black

enum IslandPalette {
    static let accent = Color(red: 0x64 / 255, green: 0xD2 / 255, blue: 0xFF / 255)  // #64D2FF
    static let amber = Color(red: 0xFF / 255, green: 0xB3 / 255, blue: 0x40 / 255)   // #FFB340
    static let red = Color(red: 0xFF / 255, green: 0x45 / 255, blue: 0x3A / 255)     // #FF453A
    static let destructiveFill = Color(red: 0xB4 / 255, green: 0x22 / 255, blue: 0x1B / 255)
    static let green = Color(red: 0x30 / 255, green: 0xD1 / 255, blue: 0x58 / 255)   // #30D158
    static let keyLine = Color.white.opacity(0.10)
    static let track = Color.white.opacity(0.15)
}

// MARK: - Presentation (§4)

/// What the pill shows. Samantha's state owns the pill, except an agent
/// approval, which takes it until answered.
enum IslandPresentation: Equatable {
    case idle, listening, thinking, speaking, acting, actingSteps, needsYou, error, done, doneSettled, agentApproval, reconnecting
    /// System now playing (music/podcast) — fills the pill when Samantha is idle/listening.
    case nowPlaying
    /// A choice/approval just timed out: 4 s "Timed out. Still open in chat" notice.
    case expired
    /// Volume / brightness HUD fits the icon, gap, and value bar.
    case hud
    /// Quick Peek fits its text and badges, capped at 244 pt.
    case peek
    /// A4 upcoming event countdown (ring on the calendar glyph).
    case calendar
    /// A9 file tray holding items (tray glyph + count).
    case tray

    /// Base widths; Quick Peek measures content up to its cap.
    var compactWidth: CGFloat {
        switch self {
        case .idle, .doneSettled, .nowPlaying, .calendar, .tray: return IslandBarLayout.idleWidth
        case .actingSteps: return IslandBarLayout.wideWidth
        case .hud: return IslandBarLayout.hudWidth
        case .peek: return IslandBarLayout.peekWidth
        default: return IslandBarLayout.standardWidth
        }
    }

    /// Kind group used for the pinch-and-grow content swap.
    var family: String {
        switch self {
        case .acting, .actingSteps: return "acting"
        case .done, .doneSettled: return "done"
        case .nowPlaying: return "media"
        default: return String(describing: self)
        }
    }

    var isConversation: Bool {
        switch self {
        case .listening, .thinking, .speaking, .acting, .actingSteps, .done, .doneSettled: return true
        default: return false
        }
    }

    var motionFamily: String { isConversation ? "conversation" : family }

    var spokenName: String {
        switch self {
        case .idle, .doneSettled: return "ready"
        case .listening: return "listening"
        case .thinking: return "thinking"
        case .speaking: return "speaking"
        case .acting, .actingSteps: return "working"
        case .needsYou: return "needs you"
        case .error: return "error"
        case .done: return "done"
        case .agentApproval: return "agent approval needed"
        case .reconnecting: return "reconnecting"
        case .nowPlaying: return "now playing"
        case .expired: return "request timed out"
        case .hud: return "level"
        case .peek: return "notification"
        case .calendar: return "upcoming event"
        case .tray: return "file tray"
        }
    }
}

// MARK: - Circles

enum IslandCircleItem: Equatable, Identifiable {
    case app(name: String, dimmed: Bool, driving: Bool)
    case overflow(Int)

    var id: String {
        switch self {
        case .app(let name, _, _): return "app:\(name)"
        case .overflow: return "overflow"
        }
    }
}

enum IslandCircles {
    static let dimAfter: TimeInterval = 10
    static let removeAfter: TimeInterval = 60

    /// `apps` arrive oldest → newest; display is newest first (nearest the pill).
    /// Apps idle > 60 s drop out, > 10 s dim to 55 %; the driven app never does.
    static func items(apps: [String], lastActive: [String: Date], driving: String, now: Date) -> [IslandCircleItem] {
        var seen = Set<String>()
        var live: [(String, Bool)] = []
        for app in apps.reversed() where !app.isEmpty && seen.insert(app).inserted {
            let idle = now.timeIntervalSince(lastActive[app] ?? now)
            let isDriving = app == driving
            if !isDriving, idle >= removeAfter { continue }
            live.append((app, !isDriving && idle >= dimAfter))
        }
        var out: [IslandCircleItem] = live.prefix(IslandBarLayout.maxCircles).map {
            .app(name: $0.0, dimmed: $0.1, driving: $0.0 == driving)
        }
        let extra = IslandBarLayout.overflowCount(count: live.count)
        if extra > 0 { out.append(.overflow(extra)) }
        return out
    }
}

// MARK: - Source circles (left of the pill): one circle per source, max 3 + "+N"

struct IslandSourceGroup: Equatable, Identifiable {
    var id: String            // source/agent name ("Grok Bot"), or "overflow"
    var app: String = ""
    var needsYou = 0
    var failed = false
    var expired = false
    var overflow = 0
    var processCount = 0
}

enum IslandSourceGroups {
    static let maxCircles = 3

    /// `hidePending`: the card the pill itself shows — its row adds no circle/badge (one amber mark).
    static func groups(agents: [IslandAgent], expired: [IslandExpired], hidePending: String?) -> [IslandSourceGroup] {
        let processCount = IslandAgentRows.processObservations(agents).count
        var order: [String] = []
        var map: [String: IslandSourceGroup] = [:]
        func touch(_ name: String, _ app: String) {
            if map[name] == nil { order.append(name); map[name] = IslandSourceGroup(id: name, app: app) }
            if map[name]!.app.isEmpty { map[name]!.app = app }
        }
        for a in IslandAgentRows.taskAgents(agents) {
            if let h = hidePending, a.id == "pending:" + h { continue }
            let name = a.name.isEmpty ? a.app : a.name
            guard !name.isEmpty else { continue }
            touch(name, a.app)
            if a.needs_you || a.status == "needs_you" { map[name]!.needsYou += 1 }
            if a.status == "error" || a.status == "stale" { map[name]!.failed = true }
        }
        for e in expired {
            let name = e.agent.isEmpty ? e.app : e.agent
            guard !name.isEmpty else { continue }
            touch(name, e.app)
            map[name]!.expired = true
        }
        let ranked = order.enumerated().compactMap { index, name in map[name].map { (index, $0) } }
            .sorted {
                func rank(_ g: IslandSourceGroup) -> Int { g.needsYou > 0 ? 0 : (g.failed || g.expired ? 1 : 2) }
                let l = rank($0.1), r = rank($1.1)
                return l == r ? $0.0 < $1.0 : l < r
            }.map(\.1)
        let taskCircleLimit = maxCircles - (processCount > 0 ? 1 : 0)
        var out = Array(ranked.prefix(taskCircleLimit))
        if ranked.count > taskCircleLimit {
            let rest = Array(ranked.dropFirst(taskCircleLimit))
            out.append(IslandSourceGroup(id: "overflow", needsYou: rest.reduce(0) { $0 + $1.needsYou },
                                         failed: rest.contains { $0.failed }, expired: rest.contains { $0.expired }, overflow: rest.count))
        }
        if processCount > 0 { out.append(IslandSourceGroup(id: "local-processes", processCount: processCount)) }
        return out
    }
}

// MARK: - Card content (§5/§6)

enum IslandCardAction: Equatable {
    case stopListening, cancel, stopSpeaking, replay, stopTask, show(String), deny, allow, dismiss, retry,
         open(String), copy(String), agentDeny(String), agentApprove(String),
         choiceSubmit(id: String, values: [String], text: String?, via: String), choiceDismiss(String)
}

enum IslandCardControl: Equatable {
    /// Alcove media style: SF Symbols 18 pt, 44 pt hit areas, evenly spaced.
    case icons([(symbol: String, label: String, action: IslandCardAction)])
    /// Agent choice: option capsules (2 per row), optional multi-select + free text.
    case choice(IslandAgentApproval)
    /// Two full-width 148×32 capsules with a 12 pt gap; at most one primary.
    case decision(secondary: (String, IslandCardAction), primary: (String, IslandCardAction), destructive: Bool)
    case none

    static func == (a: IslandCardControl, b: IslandCardControl) -> Bool {
        switch (a, b) {
        case (.none, .none): return true
        case (.choice(let x), .choice(let y)): return x == y
        case (.icons(let x), .icons(let y)):
            return x.map(\.symbol) == y.map(\.symbol) && x.map(\.action) == y.map(\.action)
        case (.decision(let s1, let p1, let d1), .decision(let s2, let p2, let d2)):
            return s1.0 == s2.0 && s1.1 == s2.1 && p1.0 == p2.0 && p1.1 == p2.1 && d1 == d2
        default: return false
        }
    }
}

enum IslandCardMeta: Equatable {
    case none
    case progress(Double, String)   // 4 pt bar + 11 pt mono label
    case micMeter                    // 24 bars, white
    case voiceMeter                  // 24 bars, accent
}

enum IslandCardArt: Equatable {
    case orb
    case app(String)
    case appDone(String)             // app icon + green check badge
    case tile(symbol: String, tint: String)   // "amber" | "red"
    case agent(String)               // agent icon + amber dot
    case artwork                     // now playing album art (model.nowPlaying)
    case expiredRing                 // hollow red ring (timed-out notice)
}

struct IslandCard: Equatable {
    var art: IslandCardArt
    var title: String
    /// Done with a value result ("84"): 28 pt semibold monospaced digits.
    var valueTitle: Bool = false
    var subtitle: String
    var meta: IslandCardMeta = .none
    var controls: IslandCardControl = .none
}

// MARK: - Model

@MainActor
final class IslandModel: ObservableObject {
    @Published private(set) var kind: String = "idle"
    @Published private(set) var title: String = ""
    @Published private(set) var detail: String = ""
    @Published private(set) var app: String = ""
    @Published private(set) var drivingApp: String = ""
    @Published private(set) var stepIndex = 0
    @Published private(set) var stepTotal = 0
    @Published private(set) var agents: [IslandAgent] = []
    @Published private(set) var agentApproval: IslandAgentApproval?
    @Published private(set) var destructiveApprovalArmedID: String?
    @Published private(set) var circles: [IslandCircleItem] = []
    @Published private(set) var presentation: IslandPresentation = .idle
    /// Compact width actually drawn (74 during the pinch of a kind change).
    @Published private(set) var drawnWidth: CGFloat = IslandBarLayout.idleWidth
    @Published private(set) var cardOpen = false
    @Published private(set) var cardMode: CardMode = .samantha
    @Published var hovering = false {
        didSet {
            guard hovering != oldValue else { return }
            if presentation == .peek {
                if hovering { pinPeek() } else { schedulePeekDismiss(after: 1.5) }
            }
            if hovering, let a = agentApproval {
                if hoveredIDs.insert(a.id).inserted {
                    IslandEventLog.log("hovered", id: a.id, source: a.agent, kind: a.kind, ["state": cardOpen ? "card" : "compact"])
                }
                // Hover pins a Quick Peek (it stays until answered / dismissed / clicked closed).
                if let until = choicePeekUntil, Date() < until {
                    choicePeekUntil = nil
                    userOpened = true
                    openVia = "hover-pin"
                    refresh(now: Date())
                }
            }
        }
    }
    /// 5 compact bars and the 24-bar card meter, 0…1, refreshed ≤ 10 Hz.
    @Published private(set) var bars5: [Double] = Array(repeating: 0, count: 5)
    @Published private(set) var meter24: [Double] = Array(repeating: 0, count: 24)

    enum CardMode: Equatable { case samantha, agents }

    /// Capture harness only: hold the compact view even for sticky/peeking states.
    var forceCompact = false {
        didSet { refresh(now: Date()) }
    }
    var reduceMotion: Bool = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    var resolveConfirm: ((Bool) -> Void)?
    /// Island context menu → ACU Settings… (General pane).
    var onOpenSettings: (() -> Void)?
    var onAction: ((IslandCardAction) -> Void)?
    /// Called when the shape/circle geometry changes so the panel can update hit-testing.
    var onGeometryChange: (() -> Void)?

    private var apps: [String] = []
    private var lastActive: [String: Date] = [:]
    private var kindSince = Date()
    private var conversationUntil = Date.distantPast
    nonisolated static let conversationQuiet: TimeInterval = 3
    private var userOpened = false
    private var peekUntil: Date?
    private var dismissedPeekKind: String?
    var voiceOverEnabled: () -> Bool = { NSWorkspace.shared.isVoiceOverEnabled }
    private var lastMeter = Date.distantPast
    private var pinchTask: Task<Void, Never>?
    private var pinching = false
    private var expandedFlag = false
    // Choice Quick Peek / arrival
    private var schedulers: [String: IslandPeekScheduler] = [:]
    private var choicePeekUntil: Date?
    private var seenCards: [String] = []
    private var hoveredIDs = Set<String>()
    private var openVia = "click"
    private var drafts: [String: (Set<String>, String)] = [:]
    /// A required consent is missing (label, e.g. "Accessibility"): small amber badge on the pill.
    @Published var permissionWarning: String?
    var onPermissionWarningTap: (() -> Void)?
    // Timed-out notices
    @Published private(set) var expired: [IslandExpired] = []
    @Published private(set) var notice: IslandExpired?
    private var noticeUntil: Date?
    private var lastNoticeAt: Double = 0
    private var ackOnClose: [String] = []
    /// Source chat app frontmost and the user typing (< 3 s since a key press) → hold the peek.
    var peekSuppressed: (IslandAgentApproval) -> Bool = { _ in false }
    /// First time a card is seen (controller plays the soft Tink unless muted / source app frontmost).
    var onArrival: ((IslandAgentApproval) -> Void)?
    /// Agent list opened then closed: acknowledge these timed-out notices.
    var onAckExpired: (([String]) -> Void)?
    /// One circle per source (left of the pill).
    @Published private(set) var sourceGroups: [IslandSourceGroup] = []
    /// Seconds left on the pill's card in its final 60 s (nil otherwise) — amber ring + pulse.
    @Published private(set) var cardRemaining: Int?
    // Choice card state (lives here so keys, drafts and tests share it)
    @Published private(set) var choiceSelected: Set<String> = []
    @Published var choiceText = "" { didSet { if choiceText != oldValue { onGeometryChange?() } } }
    @Published private(set) var confirmPending = false
    /// Ask the view to focus the reply field (set by a click into the field / click-to-open).
    @Published var fieldFocusRequested = false
    var isPeeking: Bool { choicePeekUntil.map { Date() < $0 } ?? false }

    // Durations are user-settable (Settings → General → Durations); defaults match §4.
    nonisolated(unsafe) static var peekError: TimeInterval = 6
    nonisolated(unsafe) static var peekDone: TimeInterval = 4
    nonisolated(unsafe) static var doneSettle: TimeInterval = 4

    /// Settings → Idle Activity: "none" hides the idle pill, "samantha" hides agent circles when idle,
    /// "agents"/"duo" show everything.
    @Published var idleActivity = "duo" { didSet { if idleActivity != oldValue { refresh(now: Date()) } } }
    /// Settings → Contrast outline (the 1 pt key line on the shape).
    @Published var contrastOutline = true
    @Published var systemIncreaseContrast = NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast
    @Published var systemDifferentiate = NSWorkspace.shared.accessibilityDisplayShouldDifferentiateWithoutColor
    /// Settings → AI Agents: per-source filters applied to every snapshot.
    var agentAllowed: (IslandAgent) -> Bool = { _ in true }
    var approvalAllowed: (IslandAgentApproval) -> Bool = { _ in true }

    var showAgentCircle: Bool { IslandAgentBadge.showsCircle(rowCount: agents.count + expired.count) && !(idleActivity == "samantha" && isIdlePresentation) }

    var compactAgentAttention: IslandAgentRow? {
        let candidate = agents.enumerated().filter {
            !IslandAgentRows.isProcessObservation($0.element)
                && (["needs_you", "error", "stale"].contains($0.element.status) || $0.element.needs_you)
        }.min {
            let a = IslandAgentRows.priority($0.element), b = IslandAgentRows.priority($1.element)
            return a == b ? $0.offset < $1.offset : a < b
        }
        guard let agent = candidate?.element else { return nil }
        let parentName = agent.parent_id.flatMap { id in agents.first { $0.id == id }?.name }
        return IslandAgentRow(agent: agent, depth: 0, parentName: parentName)
    }

    var destructiveApprovalMissingScope: Bool {
        guard let approval = agentApproval, approval.destructive else { return false }
        return approval.subtitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    /// Settings → AI Agents → Samantha off: her non-blocking voice states stay compact-idle on the island
    /// (confirm / needs-you still surface — they block her).
    var samanthaEnabled = true

    /// Idle pill fully hidden (Idle activity = None, nothing live).
    /// "none": nothing while idle; "agents": only when agents are live.
    var idleHidden: Bool {
        guard isIdlePresentation, !cardOpen, !isSticky else { return false }
        return idleActivity == "none" || (idleActivity == "agents" && agents.isEmpty && sourceGroups.isEmpty)
    }

    private var rawAgents: [IslandAgent] = []
    private var rawApproval: IslandAgentApproval?
    /// Settings changed: re-run the source filters on the last snapshot's agents (no peek re-arm).
    func reapplyFilters() {
        let a = rawAgents.filter(agentAllowed)
        if a != agents { agents = a }
        let ap = rawApproval.flatMap { approvalAllowed($0) ? $0 : nil }
        if ap != agentApproval { agentApproval = ap }
        refresh(now: Date())
    }

    var circleCount: Int {
        circles.reduce(0) { n, item in
            if case .overflow(let extra) = item { return n + extra }
            return n + 1
        }
    }

    var circleApps: [String] {
        circles.compactMap { if case .app(let n, _, _) = $0 { return n } else { return nil } }
    }

    var shapeRect: CGRect {
        if trayTargeted && !cardOpen {
            let s = IslandBarLayout.dropTargetSize
            return CGRect(x: (IslandBarLayout.windowSize.width - s.width) / 2, y: IslandBarLayout.topInset, width: s.width, height: s.height)
        }
        return IslandBarLayout.shapeRect(compactWidth: drawnWidth, card: cardOpen, cardHeight: cardHeight)
    }

    var shapeRadius: CGFloat { cardOpen || trayTargeted ? IslandBarLayout.cardRadius : IslandBarLayout.compactRadius }

    /// Duo secondary pill rect (nil when there is none).
    var secondaryWidth: CGFloat { secondary == .calendar ? 44 : IslandBarLayout.secondaryWidth }
    var secondaryRect: CGRect? { secondary == nil || cardOpen ? nil : IslandBarLayout.secondaryRect(shape: shapeRect, width: secondaryWidth) }

    /// App circles hang off the right-most pill.
    var circleAnchor: CGRect { secondaryRect ?? shapeRect }

    var hitRects: [CGRect] {
        if idleHidden { return [] }
        var r = IslandBarLayout.hitRects(shape: shapeRect, circleCount: 0,
                                         agentCount: showAgentCircle ? sourceGroups.count : 0)
        if let s = secondaryRect { r.append(s.insetBy(dx: -2, dy: -3)) }
        r += IslandBarLayout.circleRects(shape: circleAnchor, count: circleCount).map { $0.insetBy(dx: -2, dy: -2) }
        return r
    }

    // MARK: Apply

    /// Returns false when the snapshot was dropped (benign error).
    @discardableResult
    func apply(_ snap: IslandSnapshot, now: Date = Date()) -> Bool {
        let previousKind = kind
        let previousTitle = title
        let previousNeedsYou = Set(agents.filter { $0.needs_you || $0.status == "needs_you" }.map(\.id))
        var t = snap.title
        var d = snap.kind == "confirm" && !snap.confirm_prompt.isEmpty ? snap.confirm_prompt : snap.detail
        if snap.kind == "error" {
            let raw = t == "Action not confirmed" ? t : [t, d].filter { !$0.isEmpty }.joined(separator: " · ")
            guard let copy = IslandCopy.humanError(raw, target: snap.app) else { return false }
            if copy != raw { NSLog("[island] error raw=%@ shown=%@", String(raw.prefix(300)), copy) }
            t = copy
            d = ""
        } else {
            (t, d) = IslandCopy.compact(title: t, detail: d)  // copy contract: card title only
        }
        let shownKind = samanthaEnabled || ["confirm", "needs_you", "idle"].contains(snap.kind) ? snap.kind : "idle"
        if shownKind != kind { kindSince = now; dismissedPeekKind = nil }
        kind = shownKind
        if shownKind != previousKind && forceCompact { forceCompact = false }
        if !samanthaEnabled || shownKind == "idle" {
            conversationUntil = .distantPast
        } else if ["thinking", "speaking", "acting", "done"].contains(shownKind) {
            conversationUntil = now.addingTimeInterval(Self.conversationQuiet)
        }
        title = t
        detail = d
        app = snap.app
        stepIndex = snap.step_index
        stepTotal = snap.step_total
        rawAgents = snap.agents
        rawApproval = snap.agent_approval
        let nextAgents = snap.agents.filter(agentAllowed)
        if nextAgents != agents { agentsAt = now }
        agents = nextAgents
        reconcileRowFailures(with: nextAgents)
        let prevID = agentApproval?.id
        agentApproval = snap.agent_approval.flatMap { approvalAllowed($0) ? $0 : nil }
        if destructiveApprovalArmedID != nil && destructiveApprovalArmedID != agentApproval?.id {
            disarmDestructiveApproval()
        }
        if snap.pending_count != pendingCount { pendingCount = snap.pending_count }
        if let prevID, agentApproval?.id != prevID {
            // Answered / cleared / preempted: give key back; keep a draft; the next card slides in
            // only if the user was already in the card.
            saveDraft(prevID)
            endTextEntry()
            let keep = cardOpen && userOpened && agentApproval != nil
            userOpened = keep
            choicePeekUntil = nil
            restoreDraft(agentApproval?.id)
        } else if prevID == nil, let a = agentApproval {
            restoreDraft(a.id)
        }
        if let a = agentApproval, a.id != prevID {
            arrive(a, now: now)
            announce("\(a.agent) needs \(a.isChoice ? "a choice" : "approval"): \(a.title)", trigger: a.isChoice ? .choice : .approval)
        } else if let a = agents.first(where: { ($0.needs_you || $0.status == "needs_you") && !previousNeedsYou.contains($0.id) }) {
            announce("\(a.name.isEmpty ? a.app : a.name) needs you: \(a.title)", trigger: .needsYou)
        } else if shownKind != previousKind && ["needs_you", "confirm"].contains(shownKind) {
            announce(t, trigger: .needsYou)
        } else if shownKind == "error" && (shownKind != previousKind || t != previousTitle) {
            announce(t, trigger: .error)
        } else if shownKind == "done" && (shownKind != previousKind || t != previousTitle) && !t.isEmpty {
            announce(t, trigger: .done)
        }
        applyExpired(snap.expired, now: now)
        expandedFlag = snap.expanded
        if shownKind == "idle" {
            apps = []
            lastActive = [:]
            // Hub refreshes (rows, queue count) must not collapse an open choice/approval card.
            if agentApproval == nil && !mediaVisible && notice == nil
                && !(cardOpen && [.calendar, .tray].contains(presentation)) { userOpened = false }
        } else {
            apps = snap.active_apps.isEmpty && !snap.app.isEmpty ? [snap.app] : snap.active_apps
            for a in apps where lastActive[a] == nil { lastActive[a] = now }
        }
        drivingApp = ["acting", "driving"].contains(shownKind) ? snap.app : ""
        if !snap.app.isEmpty { lastActive[snap.app] = now }
        applyMeter(snap, now: now, force: true)
        // Routine voice feedback stays compact; errors and decisions still demand attention.
        switch shownKind {
        case "error":
            if dismissedPeekKind != shownKind {
                peekUntil = voiceOverEnabled() ? .distantFuture : now.addingTimeInterval(Self.peekError)
            }
        case "done":
            if voiceOverEnabled(), dismissedPeekKind != shownKind, !t.isEmpty { peekUntil = .distantFuture }
            else if !voiceOverEnabled() { peekUntil = nil }
        case "listening", "thinking", "speaking", "acting": peekUntil = nil
        default:
            if snap.expanded && (samanthaEnabled || shownKind != "idle") { peekUntil = now.addingTimeInterval(Self.peekDone) } else if !isSticky { peekUntil = nil }
        }
        refresh(now: now)
        return true
    }

    private func arrive(_ a: IslandAgentApproval, now: Date) {
        let first = !seenCards.contains(a.id)
        if first {
            forceCompact = false
            seenCards.append(a.id)
            if seenCards.count > 200 { seenCards.removeFirst(seenCards.count - 200) }
            IslandEventLog.log("shown", id: a.id, source: a.agent, kind: a.kind,
                               ["pending": pendingCount, "options": a.options.count])
            onArrival?(a)
            if a.isChoice {
                var sch = IslandPeekScheduler()
                sch.reset(id: a.id, arrived: now, expires: a.expiresDate)
                schedulers[a.id] = sch
                if schedulers.count > 50, let k = schedulers.keys.first(where: { $0 != a.id }) { schedulers[k] = nil }
            }
        } else if let e = a.expiresDate, var sch = schedulers[a.id], sch.slots.last.map({ $0 < e.addingTimeInterval(-IslandPeekScheduler.finalLead - 1) }) ?? false {
            // Re-posted with a new TTL: keep the count, move the T-60 slot.
            let used = sch.count
            sch.reset(id: a.id, arrived: now, expires: e)
            for _ in 0..<used { _ = sch.due(now: .distantFuture, suppressed: false) }
            schedulers[a.id] = sch
        }
    }

    private func saveDraft(_ id: String) {
        if !choiceSelected.isEmpty || !choiceText.isEmpty { drafts[id] = (choiceSelected, choiceText) } else { drafts[id] = nil }
        if drafts.count > 20, let k = drafts.keys.first { drafts[k] = nil }
    }

    private func restoreDraft(_ id: String?) {
        let d = id.flatMap { drafts[$0] }
        choiceSelected = d?.0 ?? []
        choiceText = d?.1 ?? ""
        confirmPending = false
    }

    private func applyExpired(_ list: [IslandExpired], now: Date) {
        let shown = list.filter { e in
            let a = IslandAgentApproval(id: e.id, agent: e.agent, app: e.app)
            return approvalAllowed(a)
        }
        if shown != expired { expired = shown }
        if let n = noticeUntil, now >= n { notice = nil; noticeUntil = nil }
        if let last = shown.last, last.at > lastNoticeAt {
            lastNoticeAt = last.at
            if now.timeIntervalSince1970 - last.at < 30 {
                notice = last
                noticeUntil = now.addingTimeInterval(Self.noticeDuration)
                IslandEventLog.log("notice", id: last.id, source: last.agent, kind: "expired")
            }
        }
        if let n = notice, !shown.contains(where: { $0.id == n.id }) { notice = nil; noticeUntil = nil }
    }

    nonisolated static let noticeDuration: TimeInterval = 4

    nonisolated static func hasResultValue(_ snap: IslandSnapshot) -> Bool {
        snap.expanded || (!snap.title.isEmpty && snap.title != "Done")
    }

    /// Voice meters: never a visual "state" change, throttled to 10 Hz.
    func applyMeter(_ snap: IslandSnapshot, now: Date = Date(), force: Bool = false) {
        guard force || now.timeIntervalSince(lastMeter) >= 0.1 else { return }
        lastMeter = now
        let level = max(0, min(1, snap.voice_level))
        if samanthaEnabled, level > 0, ["user", "assistant"].contains(snap.voice_side) {
            let wasQuiet = now >= conversationUntil
            conversationUntil = now.addingTimeInterval(Self.conversationQuiet)
            if wasQuiet { refresh(now: now) }
        }
        bars5 = Self.resample(snap.voice_levels, to: 5, fallback: level)
        meter24 = Array((meter24 + [level]).suffix(24))
    }

    nonisolated static func resample(_ levels: [Double], to n: Int, fallback: Double) -> [Double] {
        guard !levels.isEmpty else { return Array(repeating: fallback, count: n) }
        return (0..<n).map { i in
            let idx = min(levels.count - 1, Int((Double(i) + 0.5) * Double(levels.count) / Double(n)))
            return max(0, min(1, levels[idx]))
        }
    }

    // MARK: Activities (A2 HUD · A3 peek · A4 calendar · A5 Duo · A6 gestures · A9 tray · 7 agent rows)

    @Published private(set) var hud: IslandHUDState?
    @Published private(set) var peek: IslandPeekItem?
    @Published private(set) var calendarEvents: [IslandCalendarEvent] = []
    @Published private(set) var tray: [IslandTrayItem] = []
    @Published private(set) var secondary: IslandLiveActivity?
    @Published private(set) var trayTargeted = false
    @Published private(set) var swapped = false
    /// Optimistic row state (Stop / Retry / Approve sent, waiting for the agent's ack).
    @Published private(set) var rowControls: [String: IslandRowControl] = [:]
    /// Stop needs a second click within 3 s: armed rows (button red + check).
    @Published private(set) var stopArmed: [String: Date] = [:]
    /// Rows flashing red after a failed / unacknowledged control.
    @Published private(set) var rowFlash: Set<String> = []
    /// Textual control failure remains until the row updates or disappears.
    @Published private(set) var rowFailures: [String: String] = [:]
    var trayEnabled = true
    /// AppKit's live Reduce Motion (controller observes the workspace notification); OR'd with SwiftUI's.
    @Published var systemReduceMotion = IslandMotion.systemReduceMotion { didSet { reduceMotion = systemReduceMotion } }
    /// ⌃⌘I keyboard focus: arrows / Tab move `navIndex`, Return activates, Esc closes.
    @Published internal(set) var keyboardActive = false
    @Published internal(set) var navIndex = 0
    /// When the current agent rows arrived (rows tick "running Xs" from here).
    @Published private(set) var agentsAt = Date()
    /// Privacy / Focus gate for peeks (false = drop the peek).
    var peekGate: () -> Bool = { true }
    var onPeekVisible: ((Bool) -> Void)?
    /// Returns false when the panel could not take key (hidden): keyboard mode is rolled back.
    var onBeginKeyboard: (() -> Bool)?
    var onEndKeyboard: (() -> Void)?
    var shortcutSpoken = IslandShortcut.default.spoken
    var onAnnounce: ((String) -> Void)?
    private(set) var announcementPriority: IslandAnnouncementPriority = .medium
    private func announce(_ text: String, trigger: IslandAnnouncementTrigger) {
        announcementPriority = trigger.priority
        onAnnounce?(text)
        announcementPriority = .medium
    }
    var calendarEnabled = true
    @Published var useCalendarColor = false
    /// Compact countdown starts this long before an event (Settings → Calendar → Time before event).
    var calendarLead: TimeInterval = 600
    var gestures = (toggle: true, cycle: true)
    nonisolated(unsafe) static var hudDwell: TimeInterval = 1.2
    nonisolated(unsafe) static var peekDwell: TimeInterval = 3
    var onPeekOpen: ((IslandPeekItem) -> Void)?
    var onHaptic: (() -> Void)?
    var onRowControl: ((IslandAgent, IslandRowControl.Action) -> Void)?
    var onTrayChange: (([IslandTrayItem]) -> Void)?
    /// Vertical scroll over a visible HUD adjusts that level (true = up).
    var onHUDScroll: ((IslandHUDState.Kind, Bool) -> Void)?
    var onOpenEvent: ((IslandCalendarEvent, Bool) -> Void)?   // (event, join)
    var onShare: (([URL]) -> Void)?
    /// Settings → Sound/Display → Speed: Smooth 0.35 · Fast 0.15 · Instant (no animation).
    var hudBarAnimation: Animation? = .smooth(duration: 0.15)
    fileprivate var hudTask: Task<Void, Never>?
    fileprivate var peekTask: Task<Void, Never>?
    fileprivate var peekPinned = false
    fileprivate var swipe = IslandSwipeTracker()
    fileprivate var rowStatusAtSend: [String: String] = [:]
    private var rowFailureStatus: [String: String] = [:]
    private var rowFailureLastSeen: [String: Double] = [:]
    private var destructiveApprovalArmedAt: Date?
    fileprivate var hudScrollAccum: CGFloat = 0

    // MARK: Now Playing

    /// Track shown by the island (nil = nothing). Set by `NowPlayingController` via the island controller.
    @Published private(set) var nowPlaying: NowPlayingInfo?
    @Published private(set) var mediaVisible = false
    @Published var mediaDevice: (symbol: String, name: String) = ("hifispeaker.fill", "Speakers")
    /// Now Playing settings mirrored for the view.
    @Published var mediaStyle = NowPlayingStyle()
    var onMedia: ((NowPlayingCommand) -> Void)?
    var onMediaAction: ((String) -> Void)?

    func setNowPlaying(_ info: NowPlayingInfo?, visible: Bool, now: Date = Date()) {
        if info != nowPlaying { nowPlaying = info }
        let v = visible && info != nil
        if v != mediaVisible { mediaVisible = v }
        refresh(now: now)
    }

    /// 1 s clock: done settle, peek expiry, circle dim/removal. True if geometry changed.
    @discardableResult
    func tick(now: Date = Date()) -> Bool {
        let before = (shapeRect, circleCount)
        refresh(now: now)
        return before.0 != shapeRect || before.1 != circleCount
    }

    /// Approval cards and Samantha's confirm hold the card open; choice cards show a compact
    /// needs-you signal and open on click (they stay open while the user is typing).
    @Published private(set) var credentialRequest: CredentialRequest?

    func setCredentialRequest(_ request: CredentialRequest?) {
        credentialRequest = request
        cardMode = .samantha
        userOpened = request != nil
        forceCompact = false
        if request == nil { endTextEntry() }
        refresh(now: Date())
    }

    var isSticky: Bool { credentialRequest != nil || (agentApproval.map { !$0.isChoice } ?? false) || kind == "confirm" }

    /// Choice text field in use (the panel is key only while this is true — set by a click).
    @Published private(set) var textEntryActive = false
    /// Pending approval/choice cards in the hub queue.
    @Published private(set) var pendingCount = 0
    /// Panel hooks: become key after a click into the text field / give key back.
    var onBeginTextEntry: (() -> Void)?
    var onEndTextEntry: (() -> Void)?

    /// The panel becomes key ONLY from a click: into the reply field, or the click that opens a
    /// choice card (keys 1–6 / Return / Esc then work). Peeks and hover never make it key.
    func beginTextEntry(focusField: Bool = true) {
        guard cardOpen, isChoiceCard || credentialRequest != nil else { return }
        if focusField, agentApproval?.allow_text == true { fieldFocusRequested = true }
        guard !textEntryActive else { return }
        textEntryActive = true
        onBeginTextEntry?()
    }

    func endTextEntry() {
        fieldFocusRequested = false
        guard textEntryActive else { return }
        textEntryActive = false
        onEndTextEntry?()
    }

    var isChoiceCard: Bool { presentation == .agentApproval && agentApproval?.isChoice == true }

    /// Compact Duo: music keeps the lead (artwork), the choice's source + amber mark take the trail.
    /// An approval (non-choice) preempts music entirely.
    var duoWithMedia: Bool { mediaVisible && presentation == .agentApproval && agentApproval?.isChoice == true }

    /// Reply field lines (1…3) — the field grows, Shift-Return inserts a newline.
    var choiceTextLines: Int { IslandChoiceLayout.textLines(choiceText) }

    /// Simple cards fit their content; meters keep 176 pt and choices grow with their rows.
    var cardHeight: CGFloat {
        if credentialRequest != nil { return 292 }
        if cardMode == .agents {
            return IslandAgentRows.cardHeight(rows: rosterItemCount, footer: rosterHasMore,
                                              stopConfirmation: !stopArmed.isEmpty, maxVisibleRows: rosterVisibleRows)
        }
        if presentation == .tray { return IslandTrayRules.cardHeight }
        if presentation == .needsYou {
            let width = IslandBarLayout.cardTrailOrigin.x - IslandBarLayout.cardTextX - 8
            let text = (detail as NSString).boundingRect(with: CGSize(width: width, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [.font: NSFont.systemFont(ofSize: 13)])
            return max(176, ceil(text.height) + 102)
        }
        if isChoiceCard, let a = agentApproval {
            return IslandChoiceLayout.cardHeight(a, textLines: choiceTextLines)
        }
        if presentation == .agentApproval, let a = agentApproval, a.destructive {
            let text = "Destructive action · \(a.agent) · \(a.subtitle)"
            let width = IslandBarLayout.cardTrailOrigin.x - IslandBarLayout.cardTextX - 8
            let measured = (text as NSString).boundingRect(with: CGSize(width: width, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [.font: NSFont.systemFont(ofSize: 13)])
            return min(IslandBarLayout.windowSize.height - IslandBarLayout.topInset,
                       max(152, ceil(measured.height) + 120))
        }
        switch presentation {
        case .idle, .doneSettled, .reconnecting, .thinking, .acting, .error, .done, .expired: return 136
        case .agentApproval: return 152
        default: return IslandBarLayout.cardSize.height
        }
    }

    var cardControlsY: CGFloat { cardHeight - IslandBarLayout.cardPadding - IslandBarLayout.cardControlsHeight }

    // MARK: Choice actions (click, keys)

    /// Option capsule click. Multi: toggle. Single: send (with any typed text) — the danger option
    /// needs a second click (it turns red on the first).
    func tapOption(_ value: String, via: String = "click") {
        guard let a = agentApproval, a.isChoice, a.options.contains(where: { $0.value == value }) else { return }
        if a.multi {
            if choiceSelected.contains(value) { choiceSelected.remove(value) } else { choiceSelected.insert(value) }
            confirmPending = false
            return
        }
        if value == a.danger, !(choiceSelected == [value] && confirmPending) {
            choiceSelected = [value]
            confirmPending = true
            return
        }
        choiceSelected = [value]
        _ = submitChoice(via: via)
    }

    /// Keys 1…N pick the Nth shown option (select only; Return sends).
    func selectNumber(_ n: Int) -> Bool {
        guard let a = agentApproval, a.isChoice else { return false }
        let shown = IslandChoiceLayout.shownOptions(a)
        guard n >= 1, n <= shown.count else { return false }
        let v = shown[n - 1].value
        if a.multi {
            if choiceSelected.contains(v) { choiceSelected.remove(v) } else { choiceSelected.insert(v) }
        } else {
            choiceSelected = choiceSelected == [v] ? [] : [v]
        }
        confirmPending = false
        return true
    }

    var choiceCanSend: Bool {
        !choiceSelected.isEmpty || !choiceText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var choiceIncludesDanger: Bool {
        guard let a = agentApproval, !a.danger.isEmpty else { return false }
        return choiceSelected.contains(a.danger)
    }

    /// Send selection + text together. With the danger option selected the first press flips to
    /// "Confirm" and the second sends. Returns true when sent.
    @discardableResult
    func submitChoice(via: String = "click") -> Bool {
        guard let a = agentApproval, a.isChoice, choiceCanSend else { return false }
        if choiceIncludesDanger && !confirmPending { confirmPending = true; return false }
        let ordered = a.options.map(\.value).filter { choiceSelected.contains($0) }
        let t = choiceText.trimmingCharacters(in: .whitespacesAndNewlines)
        endTextEntry()
        drafts[a.id] = nil
        onAction?(.choiceSubmit(id: a.id, values: ordered, text: t.isEmpty ? nil : t, via: via))
        return true
    }

    /// Key handling while the panel is key (choice card only). Returns true when consumed.
    func handleKey(keyCode: UInt16, characters: String, shift: Bool, command: Bool = false, control: Bool = false) -> Bool {
        if credentialRequest != nil {
            if keyCode == 53 { CredentialEntryController.shared.cancelActive(); return true }
            return false
        }
        if keyboardActive && command && !control && (keyCode == 36 || keyCode == 76),
           case .decision(_, let primary, _) = card.controls {
            switch primary.1 {
            case .allow, .agentApprove:
                performCardAction(primary.1)
                return true
            default: break
            }
        }
        if keyboardActive && !textEntryActive && !command && !control { return handleNavKey(keyCode: keyCode, shift: shift) }
        guard isChoiceCard, cardOpen, textEntryActive, !command, !control else { return false }
        switch keyCode {
        case 53:
            endTextEntry()
            if keyboardActive { endKeyboard(close: true) } else { forceCompact = true }
            return true
        case 36, 76:   // Return / Enter
            if shift {
                guard agentApproval?.allow_text == true, choiceTextLines < 3 || !choiceText.hasSuffix("\n") else { return true }
                choiceText += "\n"
                return true
            }
            _ = submitChoice(via: "key")
            return true
        default:
            if choiceText.isEmpty, characters.count == 1, let n = Int(characters), (1...6).contains(n) {
                return selectNumber(n)
            }
            return false
        }
    }

    private func compactWidth(for state: IslandPresentation, now: Date) -> CGFloat {
        if state.isConversation, now < conversationUntil { return IslandBarLayout.standardWidth }
        if state == .peek, let peek { return IslandPeekRules.compactWidth(peek, now: now) }
        if [.idle, .doneSettled].contains(state), compactAgentAttention != nil {
            return max(state.compactWidth, IslandBarLayout.agentAttentionWidth)
        }
        return state.compactWidth
    }

    private func refresh(now: Date) {
        if let n = noticeUntil, now >= n { noticeUntil = nil; notice = nil }
        if voiceOverEnabled(), ["error", "done"].contains(kind), peekUntil != nil, dismissedPeekKind != kind {
            peekUntil = .distantFuture
        }
        let noticeActive = notice != nil && agentApproval == nil
        let live = liveActivities(now: now)
        let conversation = samanthaEnabled && now < conversationUntil
        var next = credentialRequest != nil ? .needsYou : Self.presentation(kind: kind, stepTotal: stepTotal, approval: agentApproval, since: kindSince, now: now,
                                     notice: noticeActive, hud: hud != nil, peek: peek != nil, live: live, conversation: conversation)
        if next == .doneSettled, voiceOverEnabled(), peekUntil == .distantFuture { next = .done }
        let resting = Self.presentation(kind: kind, stepTotal: stepTotal, approval: agentApproval, since: kindSince, now: now,
                                        notice: noticeActive, live: live, conversation: conversation)
        let idleNow = next == .idle || next == .doneSettled
        let nextCircles = idleNow && idleActivity == "samantha" ? []
            : IslandCircles.items(apps: apps, lastActive: lastActive, driving: drivingApp, now: now)
        if nextCircles != circles { circles = nextCircles }
        let groups = IslandSourceGroups.groups(agents: agents, expired: expired, hidePending: agentApproval?.id)
        if groups != sourceGroups { sourceGroups = groups }
        // Final-60 s countdown for the pill's card.
        var remaining: Int?
        if let e = agentApproval?.expiresDate {
            let r = e.timeIntervalSince(now)
            if r > 0 && r <= IslandPeekScheduler.finalLead { remaining = Int(r.rounded(.up)) }
        }
        if cardRemaining == nil, let remaining, let a = agentApproval {
            announce("\(a.agent): \(remaining) seconds remaining", trigger: .expiry)
        }
        if remaining != cardRemaining { cardRemaining = remaining }
        // Choice Quick Peek (non-key, 4 s; hover pins).
        if let until = choicePeekUntil, now >= until { choicePeekUntil = nil }
        if let a = agentApproval, a.isChoice, !userOpened, !forceCompact, choicePeekUntil == nil, var sch = schedulers[a.id] {
            if sch.due(now: now, suppressed: peekSuppressed(a)) {
                choicePeekUntil = now.addingTimeInterval(IslandPeekScheduler.duration)
                openVia = "peek"
                IslandEventLog.log("peek", id: a.id, source: a.agent, kind: a.kind, ["peek": sch.count])
            }
            schedulers[a.id] = sch
        }
        let choicePeek = agentApproval?.isChoice == true && (choicePeekUntil.map { now < $0 } ?? false)
        let wasOpen = cardOpen
        let idleSamantha = (next == .idle || next == .doneSettled) && cardMode == .samantha
        let open = next != .hud && next != .peek && !forceCompact && !(idleSamantha && !isSticky)
            && (userOpened || isSticky || (peekUntil.map { now < $0 } ?? false) || choicePeek
                || (next == .expired && noticeUntil != nil))
        if !open && idleSamantha { userOpened = false }
        if !open { cardMode = .samantha }
        if next != presentation {
            let familyChanged = next.motionFamily != presentation.motionFamily
            presentation = next
            if familyChanged, !(reduceMotion || systemReduceMotion), !open {
                // Alcove content swap: pinch to 74, then grow to the new width.
                pinching = true
                drawnWidth = IslandBarLayout.idleWidth
                pinchTask?.cancel()
                pinchTask = Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: 160_000_000)
                    guard let self, !Task.isCancelled else { return }
                    self.pinching = false
                    self.drawnWidth = self.compactWidth(for: self.presentation, now: Date())
                    self.onGeometryChange?()
                }
            } else if !pinching {
                drawnWidth = compactWidth(for: next, now: now)
            }
        } else if !pinching {
            drawnWidth = compactWidth(for: next, now: now)
        }
        if open != wasOpen {
            cardOpen = open
            if open, let a = agentApproval, cardMode == .samantha {
                IslandEventLog.log("opened", id: a.id, source: a.agent, kind: a.kind,
                                   ["via": isSticky && !userOpened ? "auto" : openVia])
            }
            if !open {
                if textEntryActive { endTextEntry() }
                if !ackOnClose.isEmpty { let ids = ackOnClose; ackOnClose = []; onAckExpired?(ids) }
            }
        }
        // Duo: the next live activity rides in a detached 74 pt pill (compact only).
        let sec: IslandLiveActivity? = idleActivity == "duo" && !open && !duoWithMedia && !trayTargeted
            ? live.first { $0.presentation != resting } : nil
        if sec != secondary { secondary = sec }
        expireRowControls(now: now)
        onGeometryChange?()
    }

    /// Priority: approval → expiry notice → needs-you/reconnecting → HUD → peek → other active states
    /// → live activities (media/calendar/tray) → Samantha idle/listening.
    nonisolated static func presentation(kind: String, stepTotal: Int, approval: IslandAgentApproval?,
                                         since: Date, now: Date, media: Bool = false,
                                         notice: Bool = false, hud: Bool = false, peek: Bool = false,
                                         live: [IslandLiveActivity]? = nil, conversation: Bool = false) -> IslandPresentation {
        if approval != nil { return .agentApproval }
        if notice { return .expired }
        let base = samanthaPresentation(kind: kind, stepTotal: stepTotal, since: since, now: now)
        if [.needsYou, .reconnecting].contains(base) { return base }
        if hud { return .hud }
        if peek { return .peek }
        if conversation, [.listening, .doneSettled].contains(base) { return .listening }
        let acts = live ?? (media ? [.media] : [])
        if let first = acts.first, [.idle, .doneSettled, .listening].contains(base) {
            return first.presentation
        }
        return base
    }

    nonisolated static func samanthaPresentation(kind: String, stepTotal: Int, since: Date, now: Date) -> IslandPresentation {
        switch kind {
        case "listening": return .listening
        case "thinking": return .thinking
        case "speaking": return .speaking
        case "acting", "driving", "secrets": return stepTotal > 1 ? .actingSteps : .acting
        case "confirm", "needs_you": return .needsYou
        case "error": return .error
        case "done": return now.timeIntervalSince(since) < doneSettle ? .done : .doneSettled
        case "reconnecting": return .reconnecting
        default: return .idle
        }
    }

    // MARK: Interaction

    /// The idle pill has no card: a click opens the agent list if any agents are live, else nothing.
    var isIdlePresentation: Bool { presentation == .idle || presentation == .doneSettled }

    /// A click anywhere on the pill/card background (window coords inside the shape) — logged, then toggles.
    func click(at p: CGPoint) {
        IslandEventLog.log("clicked", id: agentApproval?.id ?? "", source: agentApproval?.agent ?? "",
                           kind: agentApproval?.kind ?? presentation.family,
                           ["x": Double(p.x), "y": Double(p.y), "state": cardOpen ? "card" : "compact"])
        toggleCard()
    }

    func toggleCard(byClick: Bool = true) {
        if forceCompact && !cardOpen {
            forceCompact = false
            if cardOpen {
                if byClick && isChoiceCard { beginTextEntry(focusField: agentApproval?.allow_text == true) }
                return
            }
        }
        if !cardOpen && presentation == .hud { return }   // a HUD never opens a card
        if !cardOpen && presentation == .peek, let p = peek {
            onPeekOpen?(p)
            dismissPeek()
            return
        }
        if !cardOpen && isIdlePresentation && !isSticky {
            // No idle card: an idle click opens the roster only when there is something in it.
            if !agents.isEmpty || !expired.isEmpty { openAgentList() }
            return
        }
        let peeking = isPeeking
        if cardOpen && !isSticky && !peeking && !(presentation == .expired) {
            userOpened = false
            peekUntil = nil
            dismissedPeekKind = kind
        } else if cardOpen && presentation == .expired {
            noticeUntil = nil
            notice = nil
            openAgentList()
            return
        } else {
            // A click during a peek pins it (and, being a click, may take key for the keys).
            userOpened = true
            choicePeekUntil = nil
            cardMode = .samantha
            openVia = byClick ? "click" : "hover"
        }
        refresh(now: Date())
        if byClick, cardOpen, isChoiceCard { beginTextEntry(focusField: agentApproval?.allow_text == true) }
    }

    /// Expand on hover: same as a click, but never closes an open card (and never takes key).
    func hoverOpen() {
        guard !cardOpen else { return }
        toggleCard(byClick: false)
    }

    func openAgentList() {
        IslandEventLog.log("clicked", kind: "agents", ["state": "circles"])
        ackOnClose = expired.map(\.id)
        userOpened = true
        cardMode = .agents
        refresh(now: Date())
    }

    func openApprovalFromRoster() {
        guard agentApproval != nil else { return }
        userOpened = true
        forceCompact = false
        peekUntil = nil
        choicePeekUntil = nil
        cardMode = .samantha
        openVia = "roster"
        refresh(now: Date())
        if isChoiceCard, cardOpen { beginTextEntry(focusField: agentApproval?.allow_text == true) }
    }

    /// Agent list rows: live agents, then timed-out cards (hollow red ring).
    var listRows: [IslandAgent] {
        IslandAgentRows.taskAgents(agents) + expired.reversed().map {
            IslandAgent(id: "expired:" + $0.id, name: $0.agent, app: $0.app, title: $0.title, status: "expired")
        }
    }

    var localProcessRows: [IslandAgent] { IslandAgentRows.processObservations(agents) }
    var rosterItemCount: Int {
        max(1, listRows.count + (listRows.isEmpty ? 1 : 0)
            + (localProcessRows.isEmpty ? 0 : localProcessRows.count + 1))
    }
    var rosterVisibleRows: Int {
        min(listRows.isEmpty ? IslandAgentRows.maxRows + 2 : IslandAgentRows.maxRows, rosterItemCount)
    }
    var rosterHasMore: Bool { rosterItemCount > rosterVisibleRows }

    /// Pointer left the shape: close (unless sticky) — the panel adds the 120 ms delay.
    func pointerLeft() {
        // An opened choice card stays until answered, dismissed (X) or clicked closed — hover-away
        // would drop in-progress selections/text.
        guard cardOpen, !isSticky, !textEntryActive, !isChoiceCard, presentation != .expired else { return }
        // VoiceOver hold: an error/done peek stays until it is explicitly dismissed.
        if voiceOverHold { return }
        userOpened = false
        peekUntil = nil
        refresh(now: Date())
    }

    var voiceOverHold: Bool {
        voiceOverEnabled() && ["error", "done"].contains(kind) && peekUntil == .distantFuture && !userOpened
    }

    func dismissCard() {
        endTextEntry()
        userOpened = false
        peekUntil = nil
        if presentation == .expired { notice = nil; noticeUntil = nil }
        if kind == "error" || kind == "done" { kind = "listening"; kindSince = Date() }
        refresh(now: Date())
    }

    // MARK: Card content

    var card: IslandCard {
        var card = Self.card(for: snapshotForCard)
        if let a = agentApproval, a.destructive, case .decision(let secondary, let primary, let destructive) = card.controls {
            let label = destructiveApprovalMissingScope ? "Scope required"
                : isDestructiveApprovalArmed(a.id) ? "Confirm approve" : "Approve"
            card.controls = .decision(secondary: secondary, primary: (label, primary.1), destructive: destructive)
        }
        return card
    }

    struct CardInput: Equatable {
        var presentation: IslandPresentation
        var title: String
        var detail: String
        var app: String
        var stepIndex: Int
        var stepTotal: Int
        var approval: IslandAgentApproval?
        var notice: IslandExpired? = nil
    }

    var snapshotForCard: CardInput {
        CardInput(presentation: presentation, title: title, detail: detail, app: app.isEmpty ? drivingApp : app,
                  stepIndex: stepIndex, stepTotal: stepTotal, approval: agentApproval, notice: notice)
    }

    nonisolated static func card(for c: CardInput) -> IslandCard {
        func titled(_ fallback: String, skip: [String] = []) -> String {
            c.title.isEmpty || skip.contains(c.title) ? fallback : c.title
        }
        switch c.presentation {
        case .reconnecting:
            return IslandCard(art: .orb, title: "Reconnecting", subtitle: "Restoring your conversation. Please wait.",
                              controls: .decision(secondary: ("Turn off", .stopListening), primary: ("Dismiss", .dismiss), destructive: false))
        case .idle, .doneSettled:
            return IslandCard(art: .orb, title: "Samantha", subtitle: c.detail.isEmpty ? "Say something to start" : c.detail,
                              controls: .icons([("mic.slash.fill", "Stop listening", .stopListening)]))
        case .listening:
            return IslandCard(art: .orb, title: "Listening", subtitle: c.detail, meta: .micMeter,
                              controls: .icons([("stop.fill", "Stop", .stopListening)]))
        case .thinking:
            return IslandCard(art: .orb, title: titled("Thinking", skip: ["Thinking"]),
                              subtitle: "Thinking", controls: .icons([("xmark", "Cancel", .cancel)]))
        case .speaking:
            return IslandCard(art: .orb, title: titled("Speaking", skip: ["Speaking", "Samantha"]),
                              subtitle: c.detail, meta: .voiceMeter,
                              controls: .icons([("stop.fill", "Stop", .stopSpeaking),
                                                ("arrow.counterclockwise", "Replay", .replay)]))
        case .acting, .actingSteps:
            var sub = c.detail
            if c.stepTotal > 1 {
                let step = "step \(min(c.stepIndex, c.stepTotal)) of \(c.stepTotal)"
                sub = [c.app, step].filter { !$0.isEmpty }.joined(separator: " · ")
            } else if sub.isEmpty {
                sub = c.app
            }
            let meta: IslandCardMeta = c.stepTotal > 1
                ? .progress(Double(min(c.stepIndex, c.stepTotal)) / Double(c.stepTotal), "\(min(c.stepIndex, c.stepTotal))/\(c.stepTotal)")
                : .none
            return IslandCard(art: c.app.isEmpty ? .orb : .app(c.app), title: titled("Working"), subtitle: sub,
                              meta: meta,
                              controls: .decision(secondary: ("Stop", .stopTask), primary: ("Show", .show(c.app)),
                                                  destructive: false))
        case .needsYou:
            return IslandCard(art: .tile(symbol: "hand.raised.fill", tint: "amber"),
                              title: titled("Needs you", skip: ["Confirm"]), subtitle: c.detail,
                              controls: .decision(secondary: ("Deny", .deny), primary: ("Allow", .allow), destructive: false))
        case .error:
            if c.title == "Action not confirmed" {
                let controls: IslandCardControl = c.app.isEmpty
                    ? .icons([("xmark", "Dismiss", .dismiss)])
                    : .decision(secondary: ("Dismiss", .dismiss), primary: ("Check app", .show(c.app)), destructive: false)
                return IslandCard(art: .tile(symbol: "questionmark", tint: "amber"), title: c.title,
                                  subtitle: "The action may have happened. Check the app before trying again.", controls: controls)
            }
            return IslandCard(art: .tile(symbol: "xmark", tint: "red"), title: titled(IslandCopy.fallbackError),
                              subtitle: c.detail.isEmpty ? "Tap Retry or ask me again." : c.detail,
                              controls: .decision(secondary: ("Dismiss", .dismiss), primary: ("Retry", .retry), destructive: false))
        case .done:
            let result = c.title.isEmpty || c.title == "Done" ? (c.detail.isEmpty ? "Done" : c.detail) : c.title
            let sub = c.title.isEmpty || c.title == "Done" ? c.app : [c.detail, c.app].filter { !$0.isEmpty }.joined(separator: " · ")
            let shown = IslandCopy.compact(title: result, detail: "").title
            return IslandCard(art: c.app.isEmpty ? .orb : .appDone(c.app), title: shown,
                              valueTitle: isValueResult(shown),
                              subtitle: sub,
                              controls: .icons([("arrow.up.right", "Open", .open(c.app)), ("doc.on.doc", "Copy", .copy(result))]))
        case .nowPlaying:
            return IslandCard(art: .artwork, title: "", subtitle: "", controls: .none)  // drawn by NowPlayingCard
        case .hud, .peek, .calendar, .tray:
            return IslandCard(art: .orb, title: "", subtitle: "", controls: .none)  // drawn by their own bodies
        case .expired:
            let n = c.notice ?? IslandExpired()
            return IslandCard(art: .expiredRing, title: n.title.isEmpty ? "Request" : n.title,
                              subtitle: "Timed out. Still open in chat",
                              controls: .decision(secondary: ("Dismiss", .dismiss), primary: ("Open in chat", .open(n.app)), destructive: false))
        case .agentApproval where c.approval?.isChoice == true:
            let a = c.approval ?? IslandAgentApproval()
            return IslandCard(art: .agent(a.app), title: a.title.isEmpty ? "Choose" : a.title,
                              subtitle: a.subtitle.isEmpty ? a.agent : "\(a.agent) · \(a.subtitle)", controls: .choice(a))
        case .agentApproval:
            let a = c.approval ?? IslandAgentApproval()
            let missingScope = a.destructive && a.subtitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            let subtitle = missingScope
                ? "\(a.agent) · Target scope missing. Ask the agent to restate the affected items."
                : a.subtitle.isEmpty ? a.agent : "\(a.agent) · \(a.subtitle)"
            let approveLabel = missingScope ? "Scope required" : "Approve"
            return IslandCard(art: .agent(a.app), title: IslandCopy.compact(title: a.title.isEmpty ? "Approve?" : a.title, detail: "").title,
                              subtitle: a.destructive ? "Destructive action · \(subtitle)" : subtitle,
                              controls: .decision(secondary: ("Deny", .agentDeny(a.id)), primary: (approveLabel, .agentApprove(a.id)),
                                                  destructive: a.destructive))
        }
    }

    /// `result.type == value`: a short number-like answer ("84", "₹1,240", "3:15 pm", "72°F").
    /// Text results ("Netflix opened") stay at 15 pt.
    nonisolated static func isValueResult(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, t.count <= 14, t.contains(where: \.isNumber) else { return false }
        let letters = t.filter(\.isLetter)
        return letters.count <= 3
    }

    /// Full words for VoiceOver (compact view has no text).
    var accessibilityText: String {
        var parts: [String] = []
        if agentApproval == nil, !agents.isEmpty, ![.needsYou, .error].contains(presentation) {
            parts.append(IslandAgentAccessibility.label(running: agents.count,
                needsYou: agents.filter { $0.needs_you || $0.status == "needs_you" }.count))
        }
        if let a = agentApproval {
            parts.append("\(a.agent) needs \(a.isChoice ? "a choice" : "approval"): \(a.title). \(shortcutSpoken) to review")
            if a.destructive { parts.append("Destructive request") }
            parts.append("\(a.agent): \(a.title)")
            if !a.subtitle.isEmpty { parts.append(a.subtitle) }
        }
        parts.append("Samantha, \(presentation.spokenName)")
        if !title.isEmpty { parts.append(title) }
        if !detail.isEmpty { parts.append(detail) }
        if !circleApps.isEmpty { parts.append("Apps: " + circleApps.joined(separator: ", ")) }
        let taskCount = IslandAgentRows.taskAgents(agents).count
        let processCount = IslandAgentRows.processObservations(agents).count
        if taskCount > 0 { parts.append("\(taskCount) other agents") }
        if processCount > 0 { parts.append("\(processCount) local processes detected; task activity unknown") }
        if let r = cardRemaining { parts.append("\(r) seconds left") }
        if let n = notice { parts.append("\(n.agent) request timed out. Still open in chat") }
        return parts.joined(separator: ". ")
    }
}

/// Keeps terminal states ("Done"/"Error") visible for a beat before the next
/// listening cycle replaces them.
enum IslandDwell {
    static let holdSeconds: TimeInterval = 1.5

    static func shouldHold(current: String, since: Date, incoming: String, now: Date) -> Bool {
        guard ["done", "error"].contains(current),
              ["listening", "thinking", "speaking", "idle"].contains(incoming) else { return false }
        return now.timeIntervalSince(since) < holdSeconds
    }
}


// MARK: - Activities

extension IslandLiveActivity {
    var presentation: IslandPresentation {
        switch self {
        case .media: return .nowPlaying
        case .calendar: return .calendar
        case .tray: return .tray
        }
    }
}

extension IslandModel {
    var naturalMovement: Bool {
        get { swipe.naturalMovement }
        set { swipe.naturalMovement = newValue }
    }

    var calendarNext: IslandCalendarEvent? { CalendarLogic.next(calendarEvents, now: Date()) }

    func calendarActive(now: Date) -> Bool {
        calendarEnabled && CalendarLogic.isActive(CalendarLogic.next(calendarEvents, now: now), now: now, lead: calendarLead)
    }

    func liveActivities(now: Date) -> [IslandLiveActivity] {
        IslandDuo.order(media: mediaVisible, calendar: calendarActive(now: now), tray: trayEnabled && !tray.isEmpty, swapped: swapped)
    }

    // MARK: A2 HUD

    /// Show / update the HUD. Value changes keep the presentation (bar animates only); dismiss `hudDwell` after the last change.
    func showHUD(_ state: IslandHUDState) {
        hud = state
        hudTask?.cancel()
        let dwell = Self.hudDwell
        hudTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(dwell * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            self.hud = nil
            self.refresh(now: Date())
        }
        refresh(now: Date())
    }

    func clearHUD() {
        hudTask?.cancel()
        hud = nil
        refresh(now: Date())
    }

    // MARK: A3 Quick Peek

    func showPeek(_ item: IslandPeekItem, now: Date = Date()) {
        guard peekGate() else { return }    // screen sharing / Focus: dropped, never queued
        if peek == nil { onPeekVisible?(true) }
        let (merged, _) = IslandPeekRules.merge(current: peek, incoming: item, now: now, window: Self.peekDwell)
        peek = merged
        if !peekPinned { schedulePeekDismiss(after: Self.peekDwell) }
        refresh(now: now)
    }

    func dismissPeek() {
        peekTask?.cancel()
        peekPinned = false
        if peek != nil { onPeekVisible?(false) }
        peek = nil
        refresh(now: Date())
    }

    fileprivate func pinPeek() {
        peekPinned = true
        peekTask?.cancel()
    }

    fileprivate func schedulePeekDismiss(after s: TimeInterval) {
        peekPinned = false
        peekTask?.cancel()
        peekTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(s * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            if self.peek != nil { self.onPeekVisible?(false) }
            self.peek = nil
            self.refresh(now: Date())
        }
    }

    // MARK: A4 Calendar

    func setCalendar(_ events: [IslandCalendarEvent], now: Date = Date()) {
        if events != calendarEvents { calendarEvents = events }
        refresh(now: now)
    }

    /// Upcoming rows for the card (today + tomorrow, not ended).
    func calendarAgenda(now: Date = Date()) -> [IslandCalendarEvent] {
        calendarEvents.filter { $0.end > now }.sorted { $0.start < $1.start }
    }

    // MARK: A5 Duo / A6 gestures

    /// Swap primary and secondary live activities (click on the secondary, horizontal swipe, context menu).
    func swapActivities() {
        guard liveActivities(now: Date()).count >= 2 || secondary != nil else { return }
        swapped.toggle()
        onHaptic?()
        refresh(now: Date())
    }

    func tapSecondary() {
        guard secondary != nil else { return }
        IslandEventLog.log("clicked", kind: "duo-secondary", ["state": "compact"])
        swapActivities()
    }

    /// Scroll / two-finger swipe over the island. Returns the recognised action (tests).
    @discardableResult
    func handleScroll(dx: CGFloat, dy: CGFloat, precise: Bool, began: Bool, ended: Bool, now: Date = Date()) -> IslandSwipeTracker.Action? {
        // Over a visible HUD, vertical scroll adjusts the level (one step per 12 pt).
        if let h = hud, !cardOpen, abs(dy) >= abs(dx) {
            hudScrollAccum += dy
            let step: CGFloat = precise ? 12 : 1
            while abs(hudScrollAccum) >= step {
                let up = (hudScrollAccum > 0) != naturalMovement
                onHUDScroll?(h.kind, up)
                hudScrollAccum -= hudScrollAccum > 0 ? step : -step
            }
            return nil
        }
        guard let action = swipe.feed(dx: dx, dy: dy, precise: precise, began: began, ended: ended, now: now) else { return nil }
        switch action {
        case .swapNext, .swapPrevious:
            guard gestures.cycle, !cardOpen else { return nil }
            swapActivities()
        case .open:
            guard gestures.toggle, !cardOpen else { return nil }
            onHaptic?()
            toggleCard(byClick: false)
        case .close:
            guard gestures.toggle, cardOpen, !isSticky else { return nil }
            onHaptic?()
            pointerLeft()
        }
        return action
    }

    // MARK: A9 File tray

    func setTray(_ items: [IslandTrayItem]) {
        if items != tray { tray = items }
        refresh(now: Date())
    }

    func addToTray(_ urls: [URL]) {
        let next = IslandTrayRules.adding(urls, to: tray)
        guard next != tray else { return }
        tray = next
        onTrayChange?(tray)
        refresh(now: Date())
    }

    func removeFromTray(_ id: String) {
        tray.removeAll { $0.id == id }
        onTrayChange?(tray)
        refresh(now: Date())
    }

    func clearTray() {
        tray = []
        onTrayChange?(tray)
        if cardOpen && presentation == .tray { userOpened = false }
        refresh(now: Date())
    }

    func setTrayTargeted(_ on: Bool) {
        guard trayEnabled, on != trayTargeted else { return }
        trayTargeted = on
        if on { onHaptic?() }
        refresh(now: Date())
    }

    // MARK: 7 Agent rows

    func isStopArmed(_ id: String, now: Date = Date()) -> Bool {
        stopArmed[id].map { now.timeIntervalSince($0) < IslandAgentRows.confirmWindow } ?? false
    }

    func cancelStop(_ id: String) {
        stopArmed[id] = nil
    }

    func isDestructiveApprovalArmed(_ id: String, now: Date = Date()) -> Bool {
        destructiveApprovalArmedID == id && destructiveApprovalArmedAt.map { now.timeIntervalSince($0) < IslandAgentRows.confirmWindow } == true
    }

    /// Returns false after the first deliberate activation of a destructive approval.
    func prepareCardAction(_ action: IslandCardAction, now: Date = Date()) -> Bool {
        guard case .agentApprove(let id) = action, let approval = agentApproval,
              approval.id == id, approval.destructive else { return true }
        guard !destructiveApprovalMissingScope else {
            onAnnounce?("Approval disabled. The agent did not provide the affected targets.")
            return false
        }
        guard isDestructiveApprovalArmed(id, now: now) else {
            destructiveApprovalArmedID = id
            destructiveApprovalArmedAt = now
            onAnnounce?("Destructive request: \(approval.title). Activate Confirm approve to submit this approval.")
            refresh(now: now)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(IslandAgentRows.confirmWindow * 1_000_000_000))
                guard let self, self.destructiveApprovalArmedID == id,
                      let armedAt = self.destructiveApprovalArmedAt,
                      Date().timeIntervalSince(armedAt) >= IslandAgentRows.confirmWindow - 0.05 else { return }
                self.disarmDestructiveApproval()
                self.refresh(now: Date())
            }
            return false
        }
        disarmDestructiveApproval()
        return true
    }

    private func disarmDestructiveApproval() {
        destructiveApprovalArmedID = nil
        destructiveApprovalArmedAt = nil
    }

    /// Row buttons: Stop arms; request review opens detail; dismiss removes a stale row locally.
    func tapRowControl(_ a: IslandAgent, now: Date = Date()) {
        guard let action = IslandAgentRows.control(for: a), rowControls[a.id] == nil else { return }
        if action == .review {
            IslandEventLog.log("row-control", id: a.id, source: a.name, kind: action.rawValue)
            onRowControl?(a, action)
            return
        }
        if action == .stop && !isStopArmed(a.id, now: now) {
            stopArmed[a.id] = now
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(IslandAgentRows.confirmWindow * 1_000_000_000))
                guard let self, let t = self.stopArmed[a.id], Date().timeIntervalSince(t) >= IslandAgentRows.confirmWindow - 0.05 else { return }
                self.stopArmed[a.id] = nil
            }
            return
        }
        stopArmed[a.id] = nil
        rowControls[a.id] = IslandRowControl(action: action, since: now)
        rowStatusAtSend[a.id] = a.status
        IslandEventLog.log("row-control", id: a.id, source: a.name, kind: action.rawValue)
        onRowControl?(a, action)
    }

    /// The agent acknowledged (row changed status or went away): drop the optimistic state.
    func reconcileRowControls() {
        for (id, _) in rowControls {
            guard let row = listRows.first(where: { $0.id == id }) else { rowControls[id] = nil; rowStatusAtSend[id] = nil; continue }
            if row.status != rowStatusAtSend[id] { rowControls[id] = nil; rowStatusAtSend[id] = nil }
        }
    }

    private func reconcileRowFailures(with rows: [IslandAgent]) {
        for id in Array(rowFailures.keys) {
            guard let row = rows.first(where: { $0.id == id }) else {
                rowFailures[id] = nil; rowFailureStatus[id] = nil; rowFailureLastSeen[id] = nil
                continue
            }
            let statusChanged = row.status != rowFailureStatus[id] && row.status != "stale"
            let updated = row.last_seen_at > (rowFailureLastSeen[id] ?? 0)
            if statusChanged || updated {
                rowFailures[id] = nil; rowFailureStatus[id] = nil; rowFailureLastSeen[id] = nil
            }
        }
    }

    /// Keep a textual error until a later publisher update acknowledges the row.
    func failRowControl(_ id: String) {
        let action = rowControls[id]?.action
        let row = listRows.first { $0.id == id }
        rowControls[id] = nil
        rowStatusAtSend[id] = nil
        rowFailures[id] = "Not sent · Try again"
        rowFailureStatus[id] = row?.status
        rowFailureLastSeen[id] = row?.last_seen_at ?? 0
        flash(id)
        if let row {
            let verb = action?.rawValue ?? "control"
            onAnnounce?("Could not send \(verb) for \(row.name.isEmpty ? row.app : row.name). Not sent. Try again.")
        }
    }

    fileprivate func expireRowControls(now: Date) {
        reconcileRowControls()
        for (id, c) in rowControls where now.timeIntervalSince(c.since) > IslandAgentRows.ackTimeout {
            failRowControl(id)
        }
    }

    fileprivate func flash(_ id: String) {
        rowFlash.insert(id)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 400_000_000)
            self?.rowFlash.remove(id)
        }
    }
}
