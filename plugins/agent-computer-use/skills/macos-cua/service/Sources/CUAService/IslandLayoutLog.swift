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

    /// AppKit (origin bottom-left of the primary screen) → global top-left points.
    static func topLeft(_ r: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: r.minX, y: primaryHeight - r.maxY, width: r.width, height: r.height)
    }

    /// Same content regardless of trigger: change-only dedupe compares this.
    func sameGeometry(_ o: IslandLayoutEvent) -> Bool {
        screenId == o.screenId && screen == o.screen && scale == o.scale && island == o.island
            && islandVisible == o.islandVisible && overlays == o.overlays && sessionId == o.sessionId
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
                             visible: Bool, overlays: [IslandLayoutOverlay])?
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
                overlays: [IslandLayoutOverlay]) {
        islandPart = (screenId, IslandLayoutRect(screen), Double(scale), IslandLayoutRect(island), visible, overlays)
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
                                 overlays: p.overlays + (cursor.map { [$0] } ?? []))
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
    static func overlays(model: IslandModel, shape: CGRect, origin: CGPoint) -> [IslandLayoutOverlay] {
        func global(_ r: CGRect) -> IslandLayoutRect { IslandLayoutRect(r.offsetBy(dx: origin.x, dy: origin.y)) }
        var out: [IslandLayoutOverlay] = []
        if model.showsTargetMark, let target = model.pluginTarget {
            let d = IslandPluginTargetMark.ringDiameter
            let ring = CGRect(x: shape.minX + IslandBarLayout.slotInset + 8 - d / 2,
                              y: shape.minY + (IslandBarLayout.compactHeight - d) / 2, width: d, height: d)
            let kind: String
            if case .app(let name) = target, name == "Comet" { kind = "comet_circle" } else { kind = "target_ring" }
            out.append(IslandLayoutOverlay(kind: kind, id: kind, rect: global(ring), visible: true, expectedInsideIsland: true))
        }
        if model.showsOperatedApps {
            for (i, r) in IslandBarLayout.operatedAppRects(shape: shape, count: model.operatedApps.count).enumerated() {
                out.append(IslandLayoutOverlay(kind: "app_icon", id: "app_icon_\(i)", rect: global(r), visible: true,
                                               expectedInsideIsland: true))
            }
        }
        if !model.cardOpen {
            let circles = IslandBarLayout.circleRects(shape: shape, count: model.circles.count)
            for (i, (item, r)) in zip(model.circles, circles).enumerated() {
                let kind: String
                switch item {
                case .plugin: kind = "plugin_mark"
                case .app: kind = "app_circle"
                case .overflow: kind = "overflow_circle"
                }
                out.append(IslandLayoutOverlay(kind: kind, id: "\(kind)_\(i)", rect: global(r), visible: true,
                                               expectedInsideIsland: false))
            }
            if model.showAgentCircle {
                for (i, r) in IslandBarLayout.agentRects(shape: shape, count: model.sourceGroups.count).enumerated() {
                    out.append(IslandLayoutOverlay(kind: "harness_circle", id: "harness_circle_\(i)", rect: global(r),
                                                   visible: true, expectedInsideIsland: false))
                }
            }
        }
        return out
    }
}
