import AppKit
import Foundation

// Phase A2–A9 activity types and their pure logic (evidence/p7/*.md, polish bar evidence/p7/polish-bar.md).
// Everything here is UI-free so it can be unit-tested without a window.

// MARK: - A2 HUD

struct IslandHUDState: Equatable {
    enum Kind: String, Equatable { case volume, brightness }
    var kind: Kind
    var value: Double          // 0…1
    var muted: Bool = false

    /// Leading SF Symbol; `variableValue` fills the waves / rays with the level.
    var symbol: String {
        switch kind {
        case .volume: return muted || value <= 0.001 ? "speaker.slash.fill" : "speaker.wave.3.fill"
        case .brightness: return "sun.max.fill"
        }
    }

    /// Bar fill (muted reads as empty, like the system HUD).
    var fraction: Double { muted ? 0 : max(0, min(1, value)) }

    var spoken: String {
        let pct = Int((fraction * 100).rounded())
        switch kind {
        case .volume: return muted ? "Volume muted" : "Volume \(pct) percent"
        case .brightness: return "Brightness \(pct) percent"
        }
    }
}

/// NX_SYSDEFINED media keys (IOKit ev_keymap.h) and the step rules the system uses.
enum MediaKeyLogic {
    static let soundUp = 0, soundDown = 1, brightnessUp = 2, brightnessDown = 3, mute = 7
    static let volumeKeys: Set<Int> = [soundUp, soundDown, mute]
    static let brightnessKeys: Set<Int> = [brightnessUp, brightnessDown]
    /// NSEvent.EventType.systemDefined subtype for aux control buttons.
    static let auxSubtype: Int16 = 8

    struct Key: Equatable {
        var code: Int
        var down: Bool
        var isRepeat: Bool
    }

    /// data1 = keyCode << 16 | keyFlags; keyFlags high byte 0x0A = down, 0x0B = up; bit 0 = repeat.
    static func decode(data1: Int) -> Key {
        let code = (data1 & 0xFFFF_0000) >> 16
        let flags = data1 & 0xFFFF
        let state = (flags & 0xFF00) >> 8
        return Key(code: code, down: state == 0x0A, isRepeat: flags & 0x1 == 1)
    }

    /// System step: 1/16 per press; Option+Shift = 1/64 (quarter step). Snaps to the step grid.
    static func step(fine: Bool) -> Double { fine ? 1.0 / 64.0 : 1.0 / 16.0 }

    static func next(_ current: Double, up: Bool, fine: Bool) -> Double {
        let s = step(fine: fine)
        let grid = (current / s).rounded()
        let snapped = abs(grid * s - current) < 0.002 ? grid : (up ? floor(current / s) : ceil(current / s))
        let v = (snapped + (up ? 1 : -1)) * s
        return max(0, min(1, v))
    }
}

// MARK: - A3 Quick Peek

struct IslandPeekItem: Equatable, Identifiable {
    var id: String
    var source: String         // "Grok Bot", "System", app name
    var app: String = ""       // app whose icon leads (name or bundle id); empty = symbol
    var symbol: String = ""    // SF Symbol when there is no app icon (e.g. "airpodspro")
    var title: String          // sender / headline, one line
    var body: String = ""      // never shown in compact; VoiceOver + card only
    var at: Date = Date()
    var count: Int = 1

    var spoken: String {
        [title, body].filter { !$0.isEmpty }.joined(separator: ". ") + (count > 1 ? ". \(count) notifications" : "")
    }
}

enum IslandPeekRules {
    /// Same source within the dwell window updates the badge count in place (no re-animate).
    static func merge(current: IslandPeekItem?, incoming: IslandPeekItem, now: Date, window: TimeInterval) -> (IslandPeekItem, animate: Bool) {
        if let c = current, c.source == incoming.source, now.timeIntervalSince(c.at) < window {
            var m = incoming
            m.count = c.count + 1
            m.id = c.id
            return (m, false)
        }
        return (incoming, true)
    }

    /// "now", "2m", "1h" — 11 pt mono 45 %.
    static func age(_ at: Date, now: Date) -> String {
        let s = max(0, Int(now.timeIntervalSince(at)))
        return s < 60 ? "now" : s < 3600 ? "\(s / 60)m" : "\(s / 3600)h"
    }

    /// Compact peek time: nothing for the first minute (no "now"), then "1m", "2h".
    static func ageLabel(_ at: Date, now: Date) -> String? {
        now.timeIntervalSince(at) < 60 ? nil : age(at, now: now)
    }

    static func compactWidth(_ item: IslandPeekItem, now: Date) -> CGFloat {
        let title = (item.title as NSString).size(withAttributes: [.font: NSFont.systemFont(ofSize: 13, weight: .semibold)]).width
        var width = IslandBarLayout.slotInset + 16 + 8 + ceil(title) + 10
        if item.count > 1 {
            let count = (String(item.count) as NSString).size(withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .semibold)]).width
            width += 8 + max(16, ceil(count) + (item.count > 9 ? 8 : 0))
        }
        if let age = ageLabel(item.at, now: now) {
            width += 8 + ceil((age as NSString).size(withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)]).width)
        }
        return min(IslandBarLayout.peekWidth, max(IslandBarLayout.idleWidth, width))
    }

    /// Success glyphs draw in system green (#30D158).
    static func isSuccess(_ symbol: String) -> Bool { symbol.hasPrefix("checkmark") }
}

// MARK: - A4 Calendar

struct IslandCalendarEvent: Equatable, Identifiable {
    var id: String
    var title: String
    var start: Date
    var end: Date
    var location: String = ""
    var calendar: String = ""
    var colorHex: String = ""
    var meetingURL: URL? = nil
    var allDay: Bool = false
}

enum CalendarLogic {
    /// Meeting links Join may open (zoom/meet/teams/webex/facetime), found in url/location/notes.
    static func meetingURL(in texts: [String]) -> URL? {
        let hosts = ["zoom.us", "meet.google.com", "teams.microsoft.com", "teams.live.com", "webex.com", "facetime.apple.com", "whereby.com"]
        let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
        for t in texts where !t.isEmpty {
            let ns = t as NSString
            for m in detector?.matches(in: t, range: NSRange(location: 0, length: ns.length)) ?? [] {
                guard let u = m.url, let h = u.host?.lowercased(), u.scheme?.hasPrefix("http") == true else { continue }
                if hosts.contains(where: { h == $0 || h.hasSuffix("." + $0) }) { return u }
            }
        }
        return nil
    }

    /// Next event that matters: not all-day, not ended; earliest start.
    static func next(_ events: [IslandCalendarEvent], now: Date) -> IslandCalendarEvent? {
        events.filter { !$0.allDay && $0.end > now }.sorted { $0.start < $1.start }.first
    }

    /// Compact countdown activity: from T-lead until 5 min after start (or the end, if sooner).
    static func isActive(_ e: IslandCalendarEvent?, now: Date, lead: TimeInterval) -> Bool {
        guard let e else { return false }
        let tail = min(e.end, e.start.addingTimeInterval(300))
        return now >= e.start.addingTimeInterval(-lead) && now < tail
    }

    /// Ring fill: 1 at T-lead → 0 at start (stays 0 after start).
    static func ringFraction(_ e: IslandCalendarEvent, now: Date, lead: TimeInterval) -> Double {
        guard lead > 0 else { return 0 }
        return max(0, min(1, e.start.timeIntervalSince(now) / lead))
    }

    /// Red only in the last 5 minutes before start (and while it's starting).
    static func isUrgent(_ e: IslandCalendarEvent, now: Date) -> Bool {
        let s = e.start.timeIntervalSince(now)
        return s <= 300 && now < e.end
    }

    /// "in 8 min", "now", "in 1 h 5 min", "ends in 20 min".
    /// Compact trail: "8m", "1h", "now" (11 pt mono).
    static func shortCountdown(_ e: IslandCalendarEvent, now: Date) -> String {
        let s = e.start.timeIntervalSince(now)
        if s <= 0 { return "now" }
        let m = Int((s / 60).rounded(.up))
        return m < 60 ? "\(m)m" : "\(m / 60)h"
    }

    static func countdown(_ e: IslandCalendarEvent, now: Date) -> String {
        let s = e.start.timeIntervalSince(now)
        if s <= 0 {
            let left = Int((e.end.timeIntervalSince(now) / 60).rounded(.up))
            return left > 0 ? (s > -60 ? "now" : "ends in \(left) min") : "ended"
        }
        let m = Int((s / 60).rounded(.up))
        return m < 60 ? "in \(m) min" : "in \(m / 60) h" + (m % 60 > 0 ? " \(m % 60) min" : "")
    }

    static func color(hex: String) -> NSColor? {
        var h = hex.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("#") { h.removeFirst() }
        guard h.count == 6, let v = Int(h, radix: 16) else { return nil }
        return NSColor(srgbRed: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255, blue: CGFloat(v & 0xFF) / 255, alpha: 1)
    }
}

// MARK: - A9 File Tray

struct IslandTrayItem: Equatable, Identifiable, Codable {
    var id: String
    var path: String
    var bookmark: Data? = nil
    var added: Date = Date()
    var name: String { (path as NSString).lastPathComponent }
    var url: URL { URL(fileURLWithPath: path) }
}

enum IslandTrayRules {
    static let maxThumbs = 4
    // Card geometry: header · 48 pt tiles + name · 12 pt gap · 44 pt Share/Clear row.
    static let thumbTop: CGFloat = 40
    static let tile: CGFloat = 48
    static let tileInset: CGFloat = 4
    static let nameHeight: CGFloat = 14
    static let rowGap: CGFloat = 12
    static let buttonRow: CGFloat = 44
    static let bottomInset: CGFloat = 6
    static var buttonTop: CGFloat { thumbTop + tile + 4 + nameHeight + rowGap }
    static var cardHeight: CGFloat { buttonTop + buttonRow + bottomInset }

    /// A QuickLook thumbnail that is mostly white or empty (a text page, a blank sheet) carries no
    /// information at 48 pt: the caller shows the file's system icon instead.
    static func isMostlyBlank(_ image: CGImage, threshold: Double = 0.85) -> Bool {
        let n = 16
        var px = [UInt8](repeating: 0, count: n * n * 4)
        guard let ctx = CGContext(data: &px, width: n, height: n, bitsPerComponent: 8, bytesPerRow: n * 4,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        ctx.interpolationQuality = .medium
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: n, height: n))
        var blank = 0
        for i in stride(from: 0, to: px.count, by: 4) {
            let a = px[i + 3]
            if a < 16 || (px[i] > 235 && px[i + 1] > 235 && px[i + 2] > 235) { blank += 1 }
        }
        return Double(blank) / Double(n * n) >= threshold
    }
    static let maxItems = 50
    /// Dedupe by path, newest first, capped.
    static func adding(_ urls: [URL], to items: [IslandTrayItem], now: Date = Date()) -> [IslandTrayItem] {
        var out = items
        for u in urls.reversed() where u.isFileURL {   // a multi-file drop keeps its order
            let p = u.standardizedFileURL.path
            out.removeAll { $0.path == p }
            let bm = try? u.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
            out.insert(IslandTrayItem(id: UUID().uuidString, path: p, bookmark: bm, added: now), at: 0)
        }
        return Array(out.prefix(maxItems))
    }

    /// Thumbs shown and the "+N" overflow.
    static func visible(_ items: [IslandTrayItem]) -> (shown: [IslandTrayItem], extra: Int) {
        (Array(items.prefix(maxThumbs)), max(0, items.count - maxThumbs))
    }

    /// Drop proximity: within 60 pt of the pill (window coords).
    static func nearPill(_ p: CGPoint, shape: CGRect) -> Bool {
        CGRect(x: shape.minX - 60, y: 0, width: shape.width + 120, height: shape.maxY + 60).contains(p)
    }
}

// MARK: - A5 Duo

/// Live activities that can share the island: one primary pill + one detached 74 pt secondary.
enum IslandLiveActivity: String, Equatable, CaseIterable {
    case media, calendar, tray
}

enum IslandDuo {
    /// Ordered live activities (rank: media > calendar > tray); `swapped` rotates the first two.
    static func order(media: Bool, calendar: Bool, tray: Bool, swapped: Bool) -> [IslandLiveActivity] {
        var l: [IslandLiveActivity] = []
        if media { l.append(.media) }
        if calendar { l.append(.calendar) }
        if tray { l.append(.tray) }
        if swapped, l.count >= 2 { l.swapAt(0, 1) }
        return l
    }
}

// MARK: - A6 Gestures

/// Two-finger swipe recogniser over the island (precise scroll deltas; Alcove: NotchSwipeScrollHandler).
struct IslandSwipeTracker {
    enum Action: Equatable { case swapNext, swapPrevious, open, close }
    static let horizontalThreshold: CGFloat = 40
    static let verticalThreshold: CGFloat = 30
    static let resetGap: TimeInterval = 0.25

    var naturalMovement = false
    private(set) var dx: CGFloat = 0
    private(set) var dy: CGFloat = 0
    private var fired = false
    private var lastEvent: Date = .distantPast
    /// Haptic tick once per threshold crossing.
    private(set) var crossed = false

    /// Feed one scroll event. `ended` = gesture phase ended/cancelled (momentum ignored by the caller).
    mutating func feed(dx ex: CGFloat, dy ey: CGFloat, precise: Bool, began: Bool, ended: Bool, now: Date) -> Action? {
        if began || now.timeIntervalSince(lastEvent) > Self.resetGap { reset() }
        lastEvent = now
        crossed = false
        guard precise else { return nil }      // mouse wheels don't swipe
        if ended { reset(); return nil }
        guard !fired else { return nil }
        dx += ex
        dy += ey
        let sign: CGFloat = naturalMovement ? -1 : 1
        if abs(dx) >= Self.horizontalThreshold && abs(dx) > abs(dy) * 1.5 {
            fired = true
            crossed = true
            return dx * sign < 0 ? .swapNext : .swapPrevious
        }
        if abs(dy) >= Self.verticalThreshold && abs(dy) > abs(dx) * 1.5 {
            fired = true
            crossed = true
            // AppKit scrollingDeltaY > 0 = fingers move down (content scrolls down) with natural scrolling off in the event stream.
            return dy * sign < 0 ? .open : .close
        }
        return nil
    }

    mutating func reset() { dx = 0; dy = 0; fired = false }
}

// MARK: - 7 Agent list controls

/// Optimistic row state for agent controls and request review.
struct IslandRowControl: Equatable {
    enum Action: String, Equatable { case stop, retry, review, dismiss }
    var action: Action
    var since: Date
}

struct IslandAgentRow: Equatable, Identifiable {
    var agent: IslandAgent
    var depth: Int
    var parentName: String? = nil
    var id: String { agent.id }
}

enum IslandAgentRows {
    static let maxRows = 4
    static let rowHeight: CGFloat = 44
    static let confirmWindow: TimeInterval = 3
    static let ackTimeout: TimeInterval = 5

    /// Card hugs its rows: 16 pt padding + 44 pt rows (+ confirmation space and footer), min 96.
    static func cardHeight(rows: Int, footer: Bool, stopConfirmation: Bool = false,
                           maxVisibleRows: Int = maxRows) -> CGFloat {
        let n = CGFloat(min(max(rows, 1), max(1, maxVisibleRows)))
        return max(96, 2 * IslandBarLayout.cardPadding + n * rowHeight
            + (stopConfirmation ? 40 : 0) + (footer ? 18 : 0))
    }

    static func isProcessObservation(_ agent: IslandAgent) -> Bool {
        agent.status == "process" || agent.status == "harness"
    }

    static func processObservations(_ agents: [IslandAgent]) -> [IslandAgent] {
        agents.filter(isProcessObservation)
    }

    static func taskAgents(_ agents: [IslandAgent]) -> [IslandAgent] {
        agents.filter { !isProcessObservation($0) }
    }

    /// Keep children beneath their parent and rank branches by their most urgent descendant.
    static func priority(_ a: IslandAgent) -> Int {
        if a.needs_you || a.status == "needs_you" { return 0 }
        if ["error", "stale", "expired"].contains(a.status) { return 1 }
        if a.status == "running" { return 2 }
        if a.status == "done" { return 3 }
        return 4
    }

    static func hierarchical(_ agents: [IslandAgent]) -> [IslandAgentRow] {
        var children: [String: [IslandAgent]] = [:]
        var roots: [IslandAgent] = []
        let ids = Set(agents.map(\.id))
        let indices = Dictionary(agents.enumerated().map { ($0.element.id, $0.offset) }, uniquingKeysWith: { first, _ in first })
        for agent in agents {
            if let parent = agent.parent_id, parent != agent.id, ids.contains(parent) {
                children[parent, default: []].append(agent)
            } else {
                roots.append(agent)
            }
        }

        var rows: [IslandAgentRow] = []
        var visited = Set<String>()
        func branchPriority(_ agent: IslandAgent, visiting: Set<String> = []) -> Int {
            guard !visiting.contains(agent.id) else { return priority(agent) }
            var next = visiting
            next.insert(agent.id)
            return (children[agent.id] ?? []).reduce(priority(agent)) { min($0, branchPriority($1, visiting: next)) }
        }
        func ordered(_ agents: [IslandAgent]) -> [IslandAgent] {
            agents.sorted {
                let l = branchPriority($0), r = branchPriority($1)
                return l == r ? (indices[$0.id] ?? 0) < (indices[$1.id] ?? 0) : l < r
            }
        }
        func append(_ seeds: [(IslandAgent, Int)]) {
            var stack = Array(seeds.sorted {
                let l = branchPriority($0.0), r = branchPriority($1.0)
                return l == r ? (indices[$0.0.id] ?? 0) < (indices[$1.0.id] ?? 0) : l < r
            }.reversed())
            while let (agent, depth) = stack.popLast() {
                guard visited.insert(agent.id).inserted else { continue }
                let parentName = agent.parent_id.flatMap { parent in agents.first(where: { $0.id == parent })?.name }
                rows.append(IslandAgentRow(agent: agent, depth: depth, parentName: parentName))
                for child in ordered(children[agent.id] ?? []).reversed() { stack.append((child, depth + 1)) }
            }
        }
        append(roots.map { ($0, 0) })
        for agent in agents where !visited.contains(agent.id) { append([(agent, 0)]) }
        return rows
    }

    /// Pending rows open their request; they never answer it from the roster.
    static func control(for a: IslandAgent) -> IslandRowControl.Action? {
        if a.status == "stale" { return .dismiss }
        if a.id.hasPrefix("pending:") { return .review }
        if a.status == "error", a.controls.contains("retry") { return .retry }
        if a.status == "running" {
            return !a.id.hasPrefix("agent:") || a.controls.contains("stop") ? .stop : nil
        }
        return nil
    }

    /// "running 4m", "failed", "needs you", "done", or the last-seen age for a stale row.
    static func statusText(_ a: IslandAgent, elapsedExtra: TimeInterval = 0) -> String {
        switch a.status {
        case "error": return "failed"
        case "needs_you": return "needs you"
        case "done": return "done"
        case "expired": return "timed out"
        case "stale":
            let n = max(0, Int(a.elapsed_s + max(0, elapsedExtra)))
            let age = n < 60 ? "\(n)s" : n < 3600 ? "\(n / 60)m" : "\(n / 3600)h"
            return "updates stopped · last seen \(age) ago"
        case "process", "harness": return "activity unknown"
        default:
            let n = max(0, Int(a.elapsed_s + max(0, elapsedExtra)))
            let t = n < 60 ? "\(n)s" : n < 3600 ? "\(n / 60)m" : "\(n / 3600)h"
            return "running \(t)"
        }
    }
}
