import SwiftUI

/// Samantha's voice slot (Guru 2026-11-04 14:17 IST, "A plus C's shape cue"; UI & UX spec).
/// Colour says who speaks, shape backs it up:
/// - Guru speaking (listening): 5 white bars growing up from the baseline.
/// - Samantha speaking: 5 centred, symmetric bars in her #BF5AF2 → #FF6482 gradient, left to right.
/// - Thinking: 3 dots, 3.5 pt across, 6 pt apart, opacity 100 / 60 / 35 % stepping along on a 0.9 s loop.
/// Bars are 3 pt wide with a 2 pt gap (23 pt slot), 3 … 14 pt tall from the level. The meter never lights the dot.
/// Transitions: listening ↔ thinking and thinking ↔ speaking morph bars ↔ dots over 200 ms; speaking ↔ listening
/// crossfades the colour (and baseline) over 150 ms. Reduce Motion: static 62 % dots and a 0.15 s crossfade.
enum IslandVoiceSlotDrawing {
    struct Mark: Equatable {
        var rect: CGRect
        var corner: CGFloat
        var opacity: Double
        /// 0 = white, 1 = Samantha's gradient colour at this mark's x.
        var gradient: Double
    }

    static let samanthaFrom = (r: 0xBF / 255.0, g: 0x5A / 255.0, b: 0xF2 / 255.0)
    static let samanthaTo = (r: 0xFF / 255.0, g: 0x64 / 255.0, b: 0x82 / 255.0)
    static let pulse: [Double] = [1, 0.6, 0.35]
    static let pulsePeriod: TimeInterval = 0.9
    static let reducedDotOpacity = 0.62

    /// (morph: 0 bars … 1 dots, speaker: 0 Guru … 1 Samantha) for a resting slot.
    static func pose(_ c: IslandVoiceSlotContent) -> (morph: Double, speaker: Double) {
        switch c {
        case .listening, .none: return (0, 0)
        case .thinking: return (1, 0)
        case .speaking: return (0, 1)
        }
    }

    static func duration(from: IslandVoiceSlotContent, to: IslandVoiceSlotContent) -> TimeInterval {
        Set([from, to]) == Set([.listening, .speaking]) ? 0.15 : 0.2
    }

    static func ease(_ t: Double) -> Double {
        let x = max(0, min(1, t))
        return x < 0.5 ? 2 * x * x : 1 - pow(-2 * x + 2, 2) / 2
    }

    /// Pose `elapsed` seconds after the slot changed `from` → `to` (no Reduce Motion).
    static func pose(from: IslandVoiceSlotContent?, to: IslandVoiceSlotContent, elapsed: TimeInterval) -> (morph: Double, speaker: Double) {
        let b = pose(to)
        guard let from, from != to else { return b }
        let a = pose(from)
        let p = ease(elapsed / duration(from: from, to: to))
        return (a.morph + (b.morph - a.morph) * p, a.speaker + (b.speaker - a.speaker) * p)
    }

    /// Per-dot opacity at time `t`: the 100 % step walks left → right, 0.3 s per step, eased between steps.
    static func dotOpacities(time t: TimeInterval, reduceMotion: Bool) -> [Double] {
        if reduceMotion { return [reducedDotOpacity, reducedDotOpacity, reducedDotOpacity] }
        let steps = (t / (pulsePeriod / 3))
        let k = Int(floor(steps)), f = ease(steps - floor(steps))
        return (0..<3).map { j in
            let a = pulse[((j - k) % 3 + 3) % 3], b = pulse[((j - k - 1) % 3 + 3) % 3]
            return a + (b - a) * f
        }
    }

    static func barHeight(_ level: Double) -> CGFloat {
        IslandBarLayout.voiceMeterMinHeight
            + CGFloat(max(0, min(1, level))) * (IslandBarLayout.voiceMeterMaxHeight - IslandBarLayout.voiceMeterMinHeight)
    }

    /// The five marks in slot coordinates (origin = slot's top-left, 16 pt tall). Bars 1 / 2 / 3 become the
    /// three dots; bars 0 and 4 fold into the outer dots and fade out.
    static func marks(levels: [Double], morph: Double, speaker: Double, dots: [Double]) -> [Mark] {
        let L = IslandBarLayout.self
        let h = L.voiceSlotHeight, m = max(0, min(1, morph)), s = max(0, min(1, speaker))
        let dd = L.voiceDotDiameter
        let dotCentres = (0..<3).map { L.voiceDotsWidth / 2 + CGFloat($0 - 1) * L.voiceDotSpacing }
        return (0..<L.voiceMeterBars).map { i in
            let level = i < levels.count ? levels[i] : 0
            let bh = barHeight(level)
            let bx = CGFloat(i) * (L.voiceMeterBarWidth + L.voiceMeterBarGap)
            // Guru: bottom-aligned 1 pt above the slot's bottom; Samantha: centred.
            let by = (h - 1 - bh) * (1 - CGFloat(s)) + ((h - bh) / 2) * CGFloat(s)
            let bar = CGRect(x: bx, y: by, width: L.voiceMeterBarWidth, height: bh)
            let j = min(2, max(0, i - 1))
            let dot = CGRect(x: dotCentres[j] - dd / 2, y: (h - dd) / 2, width: dd, height: dd)
            let k = CGFloat(m)
            let r = CGRect(x: bar.minX + (dot.minX - bar.minX) * k, y: bar.minY + (dot.minY - bar.minY) * k,
                           width: bar.width + (dot.width - bar.width) * k, height: bar.height + (dot.height - bar.height) * k)
            let outer = i == 0 || i == L.voiceMeterBars - 1
            let dotOpacity = j < dots.count ? dots[j] : 1
            let opacity = outer ? 1 - m : 1 + (dotOpacity - 1) * m
            return Mark(rect: r, corner: min(r.width, r.height) / 2, opacity: opacity, gradient: s)
        }
    }

    static func color(_ mark: Mark) -> Color {
        let t = Double(max(0, min(1, mark.rect.midX / IslandBarLayout.voiceMeterWidth)))
        let a = samanthaFrom, b = samanthaTo
        let g = (r: a.r + (b.r - a.r) * t, g: a.g + (b.g - a.g) * t, b: a.b + (b.b - a.b) * t)
        let w = mark.gradient
        return Color(red: 1 + (g.r - 1) * w, green: 1 + (g.g - 1) * w, blue: 1 + (g.b - 1) * w)
    }

    /// Reduce Motion crossfade: (old layer opacity, new layer opacity) `elapsed` s after the change.
    /// The old pose leaves in 50 ms; the new one waits 80 ms (the pill has moved its dot by then) and
    /// fades in over 120 ms, so neither ever touches the dot while the width crossfades.
    static let reducedFadeOut: TimeInterval = 0.05
    static let reducedFadeInDelay: TimeInterval = 0.08
    static let reducedFadeIn: TimeInterval = 0.12
    static func reducedFade(elapsed: TimeInterval) -> (out: Double, in: Double) {
        (max(0, 1 - elapsed / reducedFadeOut), min(1, max(0, (elapsed - reducedFadeInDelay) / reducedFadeIn)))
    }

    /// Right edge of the drawn content (for the mid-frame "never touches the dot" check).
    static func contentMaxX(levels: [Double], morph: Double, speaker: Double) -> CGFloat {
        marks(levels: levels, morph: morph, speaker: speaker, dots: [1, 1, 1])
            .filter { $0.opacity > 0.1 }.map(\.rect.maxX).max() ?? 0
    }
}

/// The swappable voice slot view (meter / thinking dots); `.none` keeps the reserved slot blank.
struct IslandVoiceSlotView: View {
    let content: IslandVoiceSlotContent
    let levels: [Double]
    let reduceMotion: Bool
    @State private var from: IslandVoiceSlotContent?
    @State private var changedAt = Date.distantPast
    @State private var shown: IslandVoiceSlotContent?

    var body: some View {
        if content == .none {
            Color.clear.frame(width: IslandBarLayout.voiceSlotReservedWidth, height: IslandBarLayout.voiceSlotHeight)
        } else {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 0.1 : 1.0 / 30)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            let elapsed = ctx.date.timeIntervalSince(changedAt)
            let dots = IslandVoiceSlotDrawing.dotOpacities(time: t, reduceMotion: reduceMotion)
            Canvas { g, _ in
                if reduceMotion {
                    // Crossfade, no morph: the old pose leaves in 0.05 s (before the pill has moved its dot),
                    // the new one arrives over 0.15 s.
                    let changing = from != nil && from != content
                    let f = changing ? IslandVoiceSlotDrawing.reducedFade(elapsed: elapsed) : (out: 0, in: 1)
                    if let from, changing, f.out > 0 {
                        let p = IslandVoiceSlotDrawing.pose(from)
                        draw(&g, IslandVoiceSlotDrawing.marks(levels: levels, morph: p.morph, speaker: p.speaker, dots: dots), alpha: f.out)
                    }
                    let p = IslandVoiceSlotDrawing.pose(content)
                    draw(&g, IslandVoiceSlotDrawing.marks(levels: levels, morph: p.morph, speaker: p.speaker, dots: dots), alpha: f.in)
                } else {
                    let p = IslandVoiceSlotDrawing.pose(from: from, to: content, elapsed: elapsed)
                    draw(&g, IslandVoiceSlotDrawing.marks(levels: levels, morph: p.morph, speaker: p.speaker, dots: dots), alpha: 1)
                }
            }
        }
        .frame(width: IslandBarLayout.voiceMeterWidth, height: IslandBarLayout.voiceSlotHeight, alignment: .topLeading)
        .onAppear { shown = content }
        .onChange(of: content) { new in
            from = shown
            shown = new
            changedAt = Date()
        }
        .accessibilityHidden(true)
        }
    }

    private func draw(_ g: inout GraphicsContext, _ marks: [IslandVoiceSlotDrawing.Mark], alpha: Double) {
        for m in marks where m.opacity * alpha > 0.001 {
            var c = g
            c.opacity = m.opacity * alpha
            c.fill(Path(roundedRect: m.rect, cornerRadius: m.corner), with: .color(IslandVoiceSlotDrawing.color(m)))
        }
    }
}
