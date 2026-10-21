import CoreGraphics
import Foundation

// Comet-swoop motion for the agent cursor.
//
// Arc geometry, Fitts timing, InOutCubic ease and trail spec are adapted from
// Cua Driver's `comet_swoop` style (trycua/cua, MIT License,
// Copyright (c) Cua AI, Inc.):
//   libs/cua-driver/rust/crates/cua-cursor-motion/src/{spec,path,effects,geom}.rs
// https://github.com/trycua/cua

/// Agent cursor motion style (`cursor.motion.style`).
enum CursorMotionStyle: String, CaseIterable {
    case cometSwoop = "comet_swoop"
    case linear = "linear"

    static let `default`: CursorMotionStyle = .cometSwoop
}

struct TrailSegment: Equatable {
    let a: CGPoint
    let b: CGPoint
    let width: CGFloat
    let alpha: CGFloat
}

/// Pure motion math (no AppKit state) so it is unit-testable.
enum CometMotion {
    // Cua `knob_shape(params, 0.24, 0.2)` with default handles 0.3 / 0.3.
    static let startHandle: CGFloat = 0.3
    static let endHandle: CGFloat = 0.3
    static let arcSize: CGFloat = 0.24
    static let arcFlow: CGFloat = 0.2
    /// Shorter moves glide straight (an arc on a tiny hop reads as jitter).
    static let minArcDistance: CGFloat = 24
    /// Fast (default) glide: same budget as the legacy linear glide.
    static let fastDuration: TimeInterval = 0.12
    /// Fast trail lookback (Cua's 0.18 s scaled to the 0.12 s glide).
    static let fastTrailSecs: TimeInterval = 0.08
    /// Recording mode: Cua's exact trail lookback.
    static let recordingTrailSecs: TimeInterval = 0.18
    /// Target width used for Fitts when the caller has no element frame.
    static let defaultTargetWidth: CGFloat = 32

    /// Our pointer is 21 pt vs Cua's ~28 pt reference: trail widths/anchor scale.
    static let pointerScale: CGFloat = 21.0 / 28.0
    static let trailSteps = 26
    static let trailTailWidth: CGFloat = 2 * pointerScale
    static let trailHeadWidth: CGFloat = 12 * pointerScale
    static let trailAlpha: CGFloat = 0.38
    static let trailFadeLen: CGFloat = 60
    static let trailAnchorOffset: CGFloat = 16 * pointerScale

    /// Cua `Duration::fitts(1.15)`: (150 + 120 log2(D/max(W,4) + 1)) clamped
    /// to 300...1000 ms, then x1.15. Returns seconds.
    static func fittsSeconds(distance d: CGFloat, targetWidth w: CGFloat) -> TimeInterval {
        let ms = 150 + 120 * log2(Double(d) / Double(max(w, 4)) + 1)
        return min(max(ms, 300), 1000) * 1.15 / 1000
    }

    static func duration(
        style: CursorMotionStyle, recording: Bool,
        distance: CGFloat, targetWidth: CGFloat?
    ) -> TimeInterval {
        guard style == .cometSwoop, recording else { return fastDuration }
        return fittsSeconds(distance: distance, targetWidth: targetWidth ?? defaultTargetWidth)
    }

    static func trailSeconds(recording: Bool) -> TimeInterval {
        recording ? recordingTrailSecs : fastTrailSecs
    }

    static func easeInOutCubic(_ p: Double) -> Double {
        let t = min(1, max(0, p))
        return t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2
    }

    static func easeOutCubic(_ p: Double) -> Double {
        let t = min(1, max(0, p))
        return 1 - pow(1 - t, 3)
    }

    /// Cua bezier control points (AppKit space, y up). Bends upward for
    /// horizontal moves; vertical moves bend toward the screen centre. Control
    /// points are clamped into `screen` so the arc never leaves the display.
    static func controlPoints(
        from a: CGPoint, to b: CGPoint, screen: CGRect?
    ) -> (CGPoint, CGPoint) {
        let dx = b.x - a.x, dy = b.y - a.y
        let len = max(hypot(dx, dy), 1)
        var px = -dy / len, py = dx / len
        let flip: Bool
        if abs(py) > 1e-6 {
            flip = py < 0  // natural side: up (AppKit +y)
        } else if let screen {
            flip = (px > 0) != (a.x < screen.midX)  // toward screen centre
        } else {
            flip = false
        }
        if flip { px = -px; py = -py }
        let deflection = len < minArcDistance ? 0 : len * arcSize
        let flow = (arcFlow + 1) / 2
        let c1d = deflection * (1 - 0.5 * flow)
        let c2d = deflection * (1 - 0.5 * (1 - flow))
        var c1 = CGPoint(x: a.x + dx * startHandle + px * c1d, y: a.y + dy * startHandle + py * c1d)
        var c2 = CGPoint(x: b.x - dx * endHandle + px * c2d, y: b.y - dy * endHandle + py * c2d)
        if let screen, !screen.isNull, screen.width > 0, screen.height > 0 {
            c1 = clamp(c1, to: screen)
            c2 = clamp(c2, to: screen)
        }
        return (c1, c2)
    }

    static func bezier(_ a: CGPoint, _ c1: CGPoint, _ c2: CGPoint, _ b: CGPoint, _ t: Double) -> CGPoint {
        let t = CGFloat(min(1, max(0, t))), u = 1 - t
        let w0 = u * u * u, w1 = 3 * u * u * t, w2 = 3 * u * t * t, w3 = t * t * t
        return CGPoint(
            x: w0 * a.x + w1 * c1.x + w2 * c2.x + w3 * b.x,
            y: w0 * a.y + w1 * c1.y + w2 * c2.y + w3 * b.y
        )
    }

    /// Tip position at `elapsed` seconds. Exactly `b` once elapsed >= duration.
    static func position(
        style: CursorMotionStyle, from a: CGPoint, to b: CGPoint,
        controls: (CGPoint, CGPoint), elapsed: TimeInterval, duration: TimeInterval
    ) -> CGPoint {
        let p = duration > 0 ? elapsed / duration : 1
        if p >= 1 { return b }
        if p <= 0 { return a }
        switch style {
        case .linear:
            let e = CGFloat(easeOutCubic(p))
            return CGPoint(x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e)
        case .cometSwoop:
            return bezier(a, controls.0, controls.1, b, easeInOutCubic(p))
        }
    }

    /// Cua `effects::trail`: tail first, anchored behind the tip along the
    /// heading, width tail->head, alpha 0.38*k^2 * min(len/60, 1).
    static func trail(
        at t: TimeInterval, secs: TimeInterval, sample: (TimeInterval) -> CGPoint
    ) -> [TrailSegment] {
        let steps = trailSteps
        let eps = 1.0 / 240
        func anchor(_ time: TimeInterval) -> CGPoint {
            let p = sample(time)
            let q = sample(time - eps)
            let hx = p.x - q.x, hy = p.y - q.y
            let h = hypot(hx, hy)
            guard h > 1e-6 else { return p }
            return CGPoint(x: p.x - hx / h * trailAnchorOffset, y: p.y - hy / h * trailAnchorOffset)
        }
        let pts = (0...steps).map { i in anchor(t - secs + secs * Double(i) / Double(steps)) }
        var length: CGFloat = 0
        for i in 1..<pts.count { length += hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y) }
        let fade = min(length / trailFadeLen, 1)
        var out: [TrailSegment] = []
        for i in 1..<pts.count {
            let a = pts[i - 1], b = pts[i]
            guard hypot(b.x - a.x, b.y - a.y) > 0.3 else { continue }
            let k = CGFloat(i) / CGFloat(steps)
            out.append(TrailSegment(
                a: a, b: b,
                width: trailTailWidth + (trailHeadWidth - trailTailWidth) * k,
                alpha: trailAlpha * k * k * fade
            ))
        }
        return out
    }

    static func clamp(_ p: CGPoint, to r: CGRect) -> CGPoint {
        CGPoint(x: min(max(p.x, r.minX), r.maxX), y: min(max(p.y, r.minY), r.maxY))
    }
}
