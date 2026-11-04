import AppKit
import Foundation

/// `island_layout` rows (schema 1, developer-observability 0.1.7): the island frame plus overlay
/// frames as rectangles only — no titles, text, screenshots or audio. Global screen points, origin
/// top-left. Emitted on change only; move triggers are coalesced to one per 250 ms (latest wins).
struct IslandLayoutRect: Equatable {
    var x: Double, y: Double, w: Double, h: Double

    init(_ r: CGRect) { x = Double(r.minX); y = Double(r.minY); w = Double(r.width); h = Double(r.height) }

    var dict: [String: Any] { ["x": Self.round(x), "y": Self.round(y), "w": Self.round(w), "h": Self.round(h)] }

    static func round(_ v: Double) -> Double { (v * 10).rounded() / 10 }

    func contains(_ o: IslandLayoutRect, tolerance: Double = 2) -> Bool {
        o.x >= x - tolerance && o.y >= y - tolerance && o.x + o.w <= x + w + tolerance && o.y + o.h <= y + h + tolerance
    }
}

struct IslandLayoutOverlay: Equatable {
    var kind: String
    var id: String
    var rect: IslandLayoutRect
    var visible: Bool
    var expectedInsideIsland: Bool

    var dict: [String: Any] {
        var d = rect.dict
        d["kind"] = kind; d["id"] = id; d["visible"] = visible; d["expected_inside_island"] = expectedInsideIsland
        return d
    }
}

struct IslandLayoutGlyph: Equatable {
    var kind: String
    var id: String
    var rect: IslandLayoutRect
    var alpha: Double

    var dict: [String: Any] {
        var d = rect.dict
        d["kind"] = kind; d["id"] = id; d["alpha"] = alpha
        return d
    }
}

struct IslandLayoutDetails: Equatable {
    var origin: CGPoint
    var ringOuterRect: IslandLayoutRect?
    var dotRect: IslandLayoutRect?
    var voiceSlotRect: IslandLayoutRect?
    var voiceContents: String
    var glyphs: [IslandLayoutGlyph]
    var status: String
    var appIdentity: [String]
}

struct IslandLayoutEvent: Equatable {
    static let schema = 1
    static let moveTriggers: Set<String> = ["move", "resize", "overlay_move"]

    var trigger: String
    var sessionId: String
    var turnId: String?
    var version: String
    var screenId: String
    var screen: IslandLayoutRect
    var scale: Double
    var island: IslandLayoutRect
    var islandVisible: Bool
    var overlays: [IslandLayoutOverlay]
    var details: IslandLayoutDetails

    /// AppKit (origin bottom-left of the primary screen) → global top-left points.
    static func topLeft(_ r: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: r.minX, y: primaryHeight - r.maxY, width: r.width, height: r.height)
    }

    /// Same content regardless of trigger: change-only dedupe compares this.
    func sameGeometry(_ o: IslandLayoutEvent) -> Bool {
        screenId == o.screenId && screen == o.screen && scale == o.scale && island == o.island
            && islandVisible == o.islandVisible && overlays == o.overlays && details == o.details && sessionId == o.sessionId
    }

    func payload(now: Date) -> [String: Any] {
        var screenDict = screen.dict
        screenDict["id"] = screenId
        screenDict["scale"] = scale
        var islandDict = island.dict
        islandDict["visible"] = islandVisible
        return [
            "ts": Self.iso(now),
            "ts_epoch": now.timeIntervalSince1970,
            "event": "island_layout",
            "kind": "island_layout",
            "source": "cuaservice",
            "schema": Self.schema,
            "session_id": sessionId,
            "turn_id": turnId as Any? ?? NSNull(),
            "version": version,
            "trigger": trigger,
            "screen": screenDict,
            "island": islandDict,
            "overlays": overlays.map(\.dict),
            "island_origin": ["x": IslandLayoutRect.round(Double(details.origin.x)), "y": IslandLayoutRect.round(Double(details.origin.y))],
            "ring_outer_rect": details.ringOuterRect?.dict as Any? ?? NSNull(),
            "dot_rect": details.dotRect?.dict as Any? ?? NSNull(),
            "voice_slot": ["rect": details.voiceSlotRect?.dict as Any? ?? NSNull(), "contents": details.voiceContents],
            "glyphs": details.glyphs.map(\.dict),
            "status": details.status,
            "app_identity": details.appIdentity,
        ]
    }

    static func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = .current
        return f.string(from: date)
    }
}

/// Builds island geometry from the model, dedupes and coalesces, then appends to the activity log.
@MainActor
final class IslandLayoutLogger {
    static let shared = IslandLayoutLogger()
    static let coalesce: TimeInterval = 0.25

    var clock: () -> Date = Date.init
    var sink: ([String: Any]) -> Void = { SamanthaActivityLog.islandLayout($0) }
    /// Runs `block` after `delay` (tests drive it by hand).
    var schedule: (TimeInterval, @escaping @MainActor () -> Void) -> Void = { delay, block in
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { MainActor.assumeIsolated { block() } }
    }
    var enabled: Bool = ProcessInfo.processInfo.environment["ACU_ISLAND_LAYOUT_LOG"] != "0"

    private var islandPart: (screenId: String, screen: IslandLayoutRect, scale: Double, island: IslandLayoutRect,
                             visible: Bool, overlays: [IslandLayoutOverlay], details: IslandLayoutDetails)?
    private var cursor: IslandLayoutOverlay?
    private var last: IslandLayoutEvent?
    private var lastEmit: Date?
    private var pending: IslandLayoutEvent?
    private var timerArmed = false

    static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
    }

    /// Island geometry (global top-left). `overlays` are the in-island target ring and app icons plus the
    /// outside harness circles and plugin marks.
    func island(trigger: String, screenId: String, screen: CGRect, scale: CGFloat, island: CGRect, visible: Bool,
                overlays: [IslandLayoutOverlay], details: IslandLayoutDetails? = nil) {
        let empty = IslandLayoutDetails(origin: CGPoint(x: island.minX, y: island.minY), ringOuterRect: nil, dotRect: nil,
                                        voiceSlotRect: nil, voiceContents: "none", glyphs: [], status: "unknown", appIdentity: [])
        islandPart = (screenId, IslandLayoutRect(screen), Double(scale), IslandLayoutRect(island), visible, overlays, details ?? empty)
        submit(trigger: trigger)
    }

    /// The agent cursor panel (global top-left); nil rect hides it.
    func cursorOverlay(trigger: String, rect: CGRect?, visible: Bool) {
        if let rect {
            cursor = IslandLayoutOverlay(kind: "agent_cursor", id: "agent_cursor", rect: IslandLayoutRect(rect),
                                         visible: visible, expectedInsideIsland: false)
        } else if var c = cursor {
            c.visible = false
            cursor = c
        }
        guard islandPart != nil else { return }
        submit(trigger: trigger)
    }

    private func build(trigger: String) -> IslandLayoutEvent? {
        guard let p = islandPart else { return nil }
        return IslandLayoutEvent(trigger: trigger, sessionId: SamanthaActivityLog.currentSessionId, turnId: nil,
                                 version: Self.version, screenId: p.screenId, screen: p.screen, scale: p.scale,
                                 island: p.island, islandVisible: p.visible,
                                 overlays: p.overlays + (cursor.map { [$0] } ?? []), details: p.details)
    }

    private func submit(trigger: String) {
        guard enabled, let event = build(trigger: trigger) else { return }
        if let last, event.sameGeometry(last) { pending = nil; return }
        let now = clock()
        if IslandLayoutEvent.moveTriggers.contains(trigger), let lastEmit, now.timeIntervalSince(lastEmit) < Self.coalesce {
            pending = event
            if !timerArmed {
                timerArmed = true
                schedule(Self.coalesce - now.timeIntervalSince(lastEmit)) { [weak self] in self?.flushPending() }
            }
            return
        }
        pending = nil
        emit(event, now: now)
    }

    func flushPending() {
        timerArmed = false
        guard let event = pending else { return }
        pending = nil
        if let last, event.sameGeometry(last) { return }
        emit(event, now: clock())
    }

    private func emit(_ event: IslandLayoutEvent, now: Date) {
        last = event
        lastEmit = now
        sink(event.payload(now: now))
    }

    /// Test hook.
    func reset() { islandPart = nil; cursor = nil; last = nil; lastEmit = nil; pending = nil; timerArmed = false }
}

extension IslandLayoutLogger {
    /// Overlay rects for the compact island, from the same layout constants the view draws with.
    /// `shape` is in panel-view coordinates (origin top-left of the panel); `origin` maps it to global top-left.
    static func overlays(model: IslandModel, shape: CGRect, origin: CGPoint, scale: CGFloat = 1) -> [IslandLayoutOverlay] {
        func global(_ r: CGRect) -> IslandLayoutRect { IslandLayoutRect(r.offsetBy(dx: origin.x, dy: origin.y)) }
        var out: [IslandLayoutOverlay] = []
        if model.showsTargetMark, let target = model.pluginTarget {
            let ring = IslandBarLayout.targetRingFrame(shape: shape, scale: scale)
            let kind: String
            if case .app(let name) = target, name == "Comet" { kind = "comet_circle" } else { kind = "target_ring" }
            out.append(IslandLayoutOverlay(kind: kind, id: kind, rect: global(ring), visible: true, expectedInsideIsland: true))
        }
        if model.showsOperatedApps {
            for (item, r) in zip(model.operatedApps, IslandBarLayout.operatedAppRects(shape: shape, count: model.operatedApps.count, scale: scale)) {
                out.append(IslandLayoutOverlay(kind: "app_icon", id: item.id, rect: global(r), visible: true,
                                               expectedInsideIsland: true))
            }
        }
        if let voice = model.voiceSlotContent {
            let apps = model.showsOperatedApps ? model.operatedApps.count : 0
            let r = IslandBarLayout.voiceSlotRect(shape: shape, apps: apps, controlled: model.showsTargetMark, content: voice, scale: scale)
            out.append(IslandLayoutOverlay(kind: "voice_slot", id: "voice_slot_\(voice.rawValue)", rect: global(r), visible: true,
                                           expectedInsideIsland: true))
        }
        if !model.cardOpen {
            let circles = IslandBarLayout.circleRects(shape: shape, count: model.circles.count, scale: scale)
            for (item, r) in zip(model.circles, circles) {
                let kind: String
                switch item {
                case .plugin: kind = "plugin_mark"
                case .app: kind = "app_circle"
                case .overflow: kind = "overflow_circle"
                }
                out.append(IslandLayoutOverlay(kind: kind, id: item.id, rect: global(r), visible: true,
                                               expectedInsideIsland: false))
            }
            if model.showAgentCircle {
                let groups = model.sourceGroups.compactMap { group in model.sourceGroupSlots[group.id].map { (group, $0) } }
                for (group, r) in zip(groups, IslandBarLayout.agentRects(shape: shape, slots: groups.map(\.1), scale: scale)) {
                    out.append(IslandLayoutOverlay(kind: "harness_circle", id: group.0.id, rect: global(r),
                                                   visible: true, expectedInsideIsland: false))
                }
            }
        }
        return out
    }

    static func details(model: IslandModel, shape: CGRect, origin: CGPoint, scale: CGFloat) -> IslandLayoutDetails {
        func global(_ r: CGRect) -> IslandLayoutRect { IslandLayoutRect(r.offsetBy(dx: origin.x, dy: origin.y)) }
        func glyph(_ kind: String, _ id: String, _ rect: CGRect) -> IslandLayoutGlyph {
            IslandLayoutGlyph(kind: kind, id: id, rect: global(rect), alpha: 1)
        }
        var glyphs: [IslandLayoutGlyph] = []
        let ring = model.showsTargetMark ? IslandBarLayout.targetRingOuterRect(shape: shape, scale: scale) : nil
        if ring != nil {
            let targetID: String
            switch model.pluginTarget {
            case .symbol(let name): targetID = name
            case .app(let name): targetID = name
            case nil: targetID = "target"
            }
            glyphs.append(glyph("target", targetID, IslandBarLayout.centeredGlyphRect(
                in: IslandBarLayout.targetRingFrame(shape: shape, scale: scale),
                size: max(IslandTargetGlyph.size, IslandBarLayout.targetRingDiameter * 0.6), scale: scale)))
        }
        if model.showsOperatedApps {
            for (item, rect) in zip(model.operatedApps, IslandBarLayout.operatedAppRects(shape: shape, count: model.operatedApps.count, scale: scale)) {
                glyphs.append(glyph("app", item.id, rect))
            }
        }
        let dot = model.compactDot == nil ? nil :
            (model.showsTargetMark && !model.showsOperatedApps && model.voiceSlotContent == nil
             ? IslandBarLayout.targetOnlyDotRect(shape: shape, scale: scale)
             : IslandBarLayout.compactDotRect(shape: shape, scale: scale))
        let voice = model.voiceSlotContent.map {
            IslandBarLayout.voiceSlotRect(shape: shape, apps: model.showsOperatedApps ? model.operatedApps.count : 0,
                                          controlled: model.showsTargetMark, content: $0, scale: scale)
        }
        if !model.cardOpen {
            for (item, rect) in zip(model.circles, IslandBarLayout.circleRects(shape: shape, count: model.circles.count, scale: scale)) {
                if case .plugin(let name, _, _) = item {
                    glyphs.append(glyph("plugin", name, IslandBarLayout.centeredGlyphRect(in: rect, size: IslandBarLayout.circleGlyph, scale: scale)))
                }
            }
            if model.showAgentCircle {
                let groups = model.sourceGroups.compactMap { group in model.sourceGroupSlots[group.id].map { (group, $0) } }
                for (group, rect) in zip(groups, IslandBarLayout.agentRects(shape: shape, slots: groups.map(\.1), scale: scale)) {
                    glyphs.append(glyph("harness", group.0.id,
                                        IslandBarLayout.centeredGlyphRect(in: rect, size: IslandBarLayout.circleGlyph, scale: scale)))
                }
            }
        }
        let appIdentity = model.pluginUses.filter { $0.plugin == "acu" }.compactMap { use -> String? in
            guard let app = use.target_app?.trimmingCharacters(in: .whitespacesAndNewlines), !app.isEmpty else { return nil }
            return app
        }.sorted()
        return IslandLayoutDetails(origin: CGPoint(x: shape.minX + origin.x, y: shape.minY + origin.y),
                                   ringOuterRect: ring.map { global($0) }, dotRect: dot.map { global($0) },
                                   voiceSlotRect: voice.map { global($0) }, voiceContents: model.voiceSlotContent?.rawValue ?? "none",
                                   glyphs: glyphs, status: model.kind, appIdentity: appIdentity)
    }
}
