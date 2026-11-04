import AppKit
import QuartzCore
import SwiftUI

/// The island's one motion vocabulary (polish bar §7). Reduce Motion swaps every spring / scale for a
/// 0.15 s cross-fade. `reduce` is the SwiftUI environment value OR'd with
/// `NSWorkspace.accessibilityDisplayShouldReduceMotion` (observed live by the controller).
enum IslandMotion {
    static let crossfade = Animation.easeInOut(duration: 0.15)
    static let openSpring = Animation.spring(response: 0.36, dampingFraction: 0.7)
    static let closeSpring = Animation.spring(response: 0.45, dampingFraction: 1.0)
    static let widthCurve = Animation.smooth(duration: 0.35)
    /// Card content leaves in the first 120 ms of a close, before the critically damped shape settles.
    static let contentOut = Animation.easeOut(duration: 0.12)

    static func open(cardOpen: Bool, reduce: Bool) -> Animation { reduce ? crossfade : (cardOpen ? openSpring : closeSpring) }
    static func width(reduce: Bool) -> Animation { reduce ? crossfade : widthCurve }
    /// Operated app icons added or removed: a ~250 ms spring on the pill width (crossfade with Reduce Motion).
    static let operatedSpring = Animation.spring(response: 0.25, dampingFraction: 0.9)
    static func operatedWidth(reduce: Bool) -> Animation { reduce ? crossfade : operatedSpring }
    static func pop(reduce: Bool) -> Animation { reduce ? crossfade : openSpring }
    static func target(_ on: Bool, reduce: Bool) -> Animation { reduce ? crossfade : (on ? openSpring : closeSpring) }
    /// Scale-in transitions become plain fades.
    static func usesScale(reduce: Bool) -> Bool { !reduce }
    static func popTransition(reduce: Bool, anchor: UnitPoint = .center) -> AnyTransition {
        reduce ? .opacity : .scale(scale: 0.6, anchor: anchor).combined(with: .opacity)
    }
    /// Harness circles on the left (Codex, Cursor, Grok, Samantha, "+N"): ONE shared motion for every
    /// harness, Samantha included (UI & UX, 2026-11-04 11:57 / 12:24 IST).
    /// - Curve and duration: `widthCurve`, `.smooth(duration: 0.35)`, the same transaction that slides the
    ///   neighbours, so the entering slot (circle + 4 pt) opens while they slide.
    /// - Enter: scale 0.6 → 1 and opacity 0 → 1, the whole circle as one layer, kept centred in its own
    ///   opening slot (`harnessFrame`); leave is the exact reverse and the slot closes. Drawn above the
    ///   neighbours that slide (`harnessDrawOrder`).
    /// - Reduce Motion: no slide, no scale; the layout snaps and only the entering / leaving circle fades
    ///   over 0.15 s (`crossfade`).
    static func harness(reduce: Bool) -> Animation? { reduce ? nil : widthCurve }
    static let harnessEnterScale: CGFloat = 0.6
    static let harnessSlot: CGFloat = IslandBarLayout.circleDiameter + IslandBarLayout.circleSpacing
    static func harnessTransition(reduce: Bool, rect: CGRect,
                                  trailingGap: CGFloat = IslandBarLayout.circleSpacing) -> AnyTransition {
        if reduce { return AnyTransition.opacity.animation(crossfade) }
        return .modifier(active: HarnessSlotEffect(progress: 0, rect: rect, trailingGap: trailingGap),
                         identity: HarnessSlotEffect(progress: 1, rect: rect, trailingGap: trailingGap))
    }
    /// Where a circle whose slot is `progress` open (0 closed … 1 open) is drawn. The slot opens between the
    /// neighbour that slides out (`harnessSlot × progress`, same curve) and what sits `trailingGap` to its
    /// right (pill or circle); the circle takes the free space in the same ratio as at rest
    /// (`circleSpacing` : `trailingGap`), so it lands exactly on `r` and never sits under either side once
    /// the opened slot is wider than the circle. Never under the right-hand side, even at the very start.
    static func harnessFrame(final r: CGRect, progress v: CGFloat,
                             trailingGap g: CGFloat = IslandBarLayout.circleSpacing) -> CGRect {
        let d = r.width * (harnessEnterScale + (1 - harnessEnterScale) * v)
        let lead = IslandBarLayout.circleSpacing
        let left = r.minX - lead + harnessSlot * (1 - v)       // the sliding neighbour's trailing edge
        let right = r.maxX + g                                 // pill / next circle, fixed
        let free = right - left - d
        let midX = min(left + free * lead / (lead + g) + d / 2, right - d / 2)
        return CGRect(x: midX - d / 2, y: r.midY - d / 2, width: d, height: d)
    }
    /// What sits right of harness slot `index`: the pill for the first circle, the previous circle otherwise.
    static func harnessTrailingGap(rects: [CGRect], index: Int, shape: CGRect) -> CGFloat {
        index == 0 ? shape.minX - rects[0].maxX : rects[index - 1].minX - rects[index].maxX
    }
    /// Draw order: nearer the pill is on top, and a circle that stays ranks by the farther-out of its old and
    /// new slot. The circle entering or leaving is always nearer the pill than the neighbours its slot pushes,
    /// and a leaving circle keeps its last rank, so it stays above them on turn-on AND turn-off.
    static func harnessDrawOrder(id: String, slot index: Int, previous: [String]) -> Double {
        -Double(max(index, previous.firstIndex(of: id) ?? index))
    }
    /// Circles are placed with `.offset` inside the window-sized ZStack, so their layout frame sits at the
    /// origin; the scale anchor must be the drawn circle's centre in that frame's unit space.
    static func harnessAnchor(_ r: CGRect) -> UnitPoint {
        guard r.width > 0, r.height > 0 else { return .center }
        return UnitPoint(x: r.midX / r.width, y: r.midY / r.height)
    }
    static var systemReduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
}

/// Enter / leave of one harness circle as ONE animatable value, so scale, opacity and position stay locked
/// to the slot that is opening or closing on the same curve.
struct HarnessSlotEffect: ViewModifier, Animatable {
    var progress: CGFloat
    let rect: CGRect
    var trailingGap: CGFloat = IslandBarLayout.circleSpacing
    var animatableData: CGFloat {
        get { progress }
        set { progress = newValue }
    }
    func body(content: Content) -> some View {
        let f = IslandMotion.harnessFrame(final: rect, progress: progress, trailingGap: trailingGap)
        content
            .compositingGroup()
            .scaleEffect(f.width / rect.width, anchor: IslandMotion.harnessAnchor(rect))
            .opacity(Double(progress))
            .offset(x: f.midX - rect.midX)
    }
}

/// The island silhouette as one animatable path inside a FIXED-size container: the spring interpolates
/// `rect` / `radius` at render time (no per-frame relayout of the content, which keeps its own pinned frames).
struct IslandShapePath: InsettableShape {
    var rect: CGRect
    var radius: CGFloat
    var inset: CGFloat = 0
    var animatableData: AnimatablePair<AnimatablePair<CGFloat, CGFloat>, AnimatablePair<AnimatablePair<CGFloat, CGFloat>, CGFloat>> {
        get { AnimatablePair(AnimatablePair(rect.minX, rect.minY), AnimatablePair(AnimatablePair(rect.width, rect.height), radius)) }
        set {
            rect = CGRect(x: newValue.first.first, y: newValue.first.second,
                          width: newValue.second.first.first, height: newValue.second.first.second)
            radius = newValue.second.second
        }
    }
    func path(in _: CGRect) -> Path {
        let r = rect.insetBy(dx: inset, dy: inset)
        guard r.width > 0, r.height > 0 else { return Path() }
        return Path(roundedRect: r, cornerRadius: max(0, min(radius - inset, r.height / 2, r.width / 2)), style: .continuous)
    }
    func inset(by amount: CGFloat) -> IslandShapePath { var c = self; c.inset += amount; return c }
}

/// Per-frame probe of the island shape size (in-process, no screen capture). Each SwiftUI animation
/// frame re-evaluates `body` with the interpolated value, so the sample stream IS the frame timeline.
final class IslandMotionRecorder: @unchecked Sendable {
    static let shared = IslandMotionRecorder()
    struct Sample: Equatable { var t: CFTimeInterval; var w: CGFloat; var h: CGFloat }
    private let lock = NSLock()
    private var buf: [Sample] = []
    private var on = false
    private var last: Sample?
    /// Most recent probed size (read once per display tick by the harness / service sampler).
    var latest: Sample? { lock.lock(); defer { lock.unlock() }; return last }
    var enabled: Bool {
        get { lock.lock(); defer { lock.unlock() }; return on }
        set { lock.lock(); on = newValue; if !newValue { buf.removeAll() }; lock.unlock() }
    }
    func note(_ w: CGFloat, _ h: CGFloat) {
        lock.lock(); defer { lock.unlock() }
        guard on else { return }
        last = Sample(t: CACurrentMediaTime(), w: w, h: h)
        if let l = buf.last, l.w == w, l.h == h { return }
        buf.append(Sample(t: CACurrentMediaTime(), w: w, h: h))
        if buf.count > 6000 { buf.removeFirst(2000) }
    }
    func drain() -> [Sample] { lock.lock(); defer { lock.unlock() }; let b = buf; buf.removeAll(); return b }
}

struct IslandMotionProbe: ViewModifier, Animatable {
    var size: CGSize
    var animatableData: AnimatablePair<CGFloat, CGFloat> {
        get { AnimatablePair(size.width, size.height) }
        set { size = CGSize(width: newValue.first, height: newValue.second) }
    }
    func body(content: Content) -> some View {
        IslandMotionRecorder.shared.note(size.width, size.height)
        // A body that is exactly `content` is folded away by SwiftUI (never re-evaluated); an identity
        // effect keeps it in the graph at no visual cost.
        return content.opacity(1)
    }
}

/// Frame-timing analysis of one motion segment (the harness drives 60 Hz ticks; the Mini's LG panel is 100 Hz).
struct IslandFrameTiming: Equatable {
    var frames: Int
    var durationMs: Double
    var from: CGFloat
    var to: CGFloat
    var peak: CGFloat
    var overshootPt: CGFloat
    var overshootPct: Double
    var peakMs: Double       // first frame at the extreme value (≈ target time for non-overshooting curves)
    var settleMs: Double     // last frame further than 0.5 pt (1 px @2x) from the target
    var settle1PctMs: Double // classic settling time: stays within 1 % of the travel
    var stalls: Int          // tick gaps > 1.5 refresh intervals + repeated frames mid-motion
    var worstGapMs: Double

    static func analyze(_ s: [IslandMotionRecorder.Sample], refresh: Double = 1.0 / 60.0, dimension: KeyPath<IslandMotionRecorder.Sample, CGFloat> = \.w) -> IslandFrameTiming? {
        guard s.count >= 2 else { return nil }
        let from = s[0][keyPath: dimension], to = s[s.count - 1][keyPath: dimension]
        let up = to >= from
        let vals = s.map { $0[keyPath: dimension] }
        let peak = up ? vals.max()! : vals.min()!
        let over = up ? max(0, peak - to) : max(0, to - peak)
        // Settled = last sample further than 0.5 pt from the target.
        let lastOff = s.lastIndex { abs($0[keyPath: dimension] - to) > 0.5 } ?? 0
        let settle = (s[min(lastOff + 1, s.count - 1)].t - s[0].t) * 1000
        let peakAt = vals.firstIndex(of: peak) ?? 0
        let travel = abs(to - from)
        let band = max(0.5, travel * 0.01)
        let lastOut = s.lastIndex { abs($0[keyPath: dimension] - to) > band } ?? 0
        let settle1 = (s[min(lastOut + 1, s.count - 1)].t - s[0].t) * 1000
        var stalls = 0, worst = 0.0
        // Motion window: first to last value change; a repeated value inside it is a dropped frame.
        let firstMove = (1..<s.count).first { s[$0][keyPath: dimension] != s[$0 - 1][keyPath: dimension] } ?? 1
        let lastMove = (1..<s.count).last { s[$0][keyPath: dimension] != s[$0 - 1][keyPath: dimension] } ?? 0
        for i in 1..<s.count {
            let g = s[i].t - s[i - 1].t
            worst = max(worst, g)
            if g > refresh * 1.5 { stalls += 1 }
            else if i > firstMove, i < lastMove, s[i][keyPath: dimension] == s[i - 1][keyPath: dimension] { stalls += 1 }
        }
        return IslandFrameTiming(frames: s.count, durationMs: (s[s.count - 1].t - s[0].t) * 1000, from: from, to: to, peak: peak,
                                 overshootPt: over, overshootPct: travel > 0 ? Double(over / travel) * 100 : 0,
                                 peakMs: (s[peakAt].t - s[0].t) * 1000, settleMs: settle, settle1PctMs: settle1, stalls: stalls, worstGapMs: worst * 1000)
    }

    /// Split a per-tick stream into motion segments: a time gap > `gap`, or `still` unchanged ticks, ends one.
    /// Each segment keeps one resting tick on either side (the from / to values).
    static func segments(_ s: [IslandMotionRecorder.Sample], gap: Double = 0.12, still: Int = 8) -> [[IslandMotionRecorder.Sample]] {
        var out: [[IslandMotionRecorder.Sample]] = [], cur: [IslandMotionRecorder.Sample] = []
        var run = 0
        // Sub-0.05 pt spring tails count as rest, so an open → dwell → close splits into two segments.
        func moved(_ a: IslandMotionRecorder.Sample, _ b: IslandMotionRecorder.Sample) -> Bool { abs(a.w - b.w) > 0.05 || abs(a.h - b.h) > 0.05 }
        func flush() {
            while cur.count > 2, !moved(cur[cur.count - 1], cur[cur.count - 2]) { cur.removeLast() }
            if cur.count > 1, cur.indices.dropFirst().contains(where: { moved(cur[$0], cur[$0 - 1]) }) { out.append(cur) }
        }
        for x in s {
            if let l = cur.last {
                if x.t - l.t > gap { flush(); cur = [] ; run = 0 }
                else if !moved(x, l) {
                    run += 1
                    if run >= still { flush(); cur = [x]; continue }
                } else {
                    if run >= still || cur.count == 1 { cur = [l] }
                    run = 0
                }
            }
            cur.append(x)
        }
        flush()
        return out
    }

    var row: String {
        // A swap that returns to its start width (Duo pinch) has no travel: report the dip, not a %.
        let pct = abs(to - from) < 1 ? "dip" : String(format: "%.1f", overshootPct)
        return String(format: "%d | %.0f | %.0f→%.0f | %.0f | %.1f | %@ | %.0f | %.0f | %.0f | %d | %.1f", frames, durationMs, from, to, peak, overshootPt,
                      pct, peakMs, settle1PctMs, settleMs, stalls, worstGapMs)
    }
    static let header = "frames | duration ms | from→to pt | peak pt | overshoot pt | overshoot % | peak ms | settle ms (1 %) | settle ms (±0.5 pt) | stalls (>25 ms gap) | worst gap ms"
}

/// First-appearance cost (SF Symbol rasterising, view-type instantiation, text layout) cost the first
/// open of each activity 1–2 dropped frames. Render every activity once, offscreen, shortly after
/// launch so the first real open is warm. Fixture data only; nothing is shown.
@MainActor
enum IslandPrewarm {
    static func run() {
        IslandModel.hudDwell = max(IslandModel.hudDwell, 0.1)
        let states: [(IslandModel) -> Void] = [
            { $0.showHUD(.init(kind: .volume, value: 0.5)) },
            { $0.showHUD(.init(kind: .volume, value: 0.5, muted: true)) },
            { $0.showHUD(.init(kind: .brightness, value: 0.5)) },
            { $0.showPeek(IslandPeekItem(id: "w", source: "Island", symbol: "bell.fill", title: "Warm")) },
            { m in m.idleActivity = "agents"
                m.apply(IslandSnapshot(kind: "idle", agents: [IslandAgent(id: "w:1", name: "Island", title: "Warm", status: "running", controls: ["stop"]),
                                                              IslandAgent(id: "w:2", name: "Island", title: "Warm", status: "error", controls: ["retry"])]))
                m.toggleCard() },
            { m in m.setTrayTargeted(true) },
        ]
        for s in states {
            let m = IslandModel()
            m.trayEnabled = true
            s(m)
            let r = ImageRenderer(content: SamanthaIslandBar(model: m))
            r.scale = NSScreen.main?.backingScaleFactor ?? 2
            _ = r.cgImage
            m.clearHUD()
            m.dismissPeek()
        }
    }
}
