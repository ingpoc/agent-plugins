import SwiftUI

/// Samantha's own harness mark (Guru, 2026-11-04 11:09 IST: "the style of samantha should be distinct";
/// 11:54 IST picked B, the wave). She is a voice agent, so her mark is a voice: one tapered sound wave in
/// her own violet-to-rose, on the island's black circle. Never a white plugin tile (comet-control,
/// context-ledger) and never ACU's app icon. Vector, so it is crisp at 1x and 2x.
enum SamanthaMarkPalette {
    static let violet = Color(red: 0xBF / 255, green: 0x5A / 255, blue: 0xF2 / 255)  // #BF5AF2
    static let rose = Color(red: 0xFF / 255, green: 0x64 / 255, blue: 0x82 / 255)    // #FF6482
    /// Left to right across the wave (UI & UX, 2026-11-04 11:57 IST).
    static var gradient: LinearGradient {
        LinearGradient(colors: [violet, rose], startPoint: .leading, endPoint: .trailing)
    }
}

/// Drawn inside the 24 pt harness circle (`SamanthaSourceCircle` keeps the black fill, key line and badges).
/// UI & UX spec (2026-11-04 11:57 IST): stroke ≥ 1.5 pt at 1x (2 pt in the 24 pt circle) with round caps,
/// spanning 60% of the diameter, vertically centred.
struct SamanthaMark: View {
    var diameter: CGFloat = IslandBarLayout.circleDiameter
    static func strokeWidth(diameter: CGFloat) -> CGFloat { max(1.5, diameter / 12) }
    static let spanFraction: CGFloat = 0.6
    static let heightFraction: CGFloat = 0.42

    var body: some View {
        SamanthaWaveShape()
            .stroke(SamanthaMarkPalette.gradient,
                    style: StrokeStyle(lineWidth: Self.strokeWidth(diameter: diameter), lineCap: .round, lineJoin: .round))
            .frame(width: diameter * Self.spanFraction, height: diameter * Self.heightFraction)
            .frame(width: diameter, height: diameter)   // centred in the circle
    }
}

/// One and a half periods of a tapered sine: quiet at the ends, loud in the middle, like a spoken word.
struct SamanthaWaveShape: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        let steps = 48
        for i in 0...steps {
            let t = CGFloat(i) / CGFloat(steps)
            let envelope = sin(t * .pi)
            let y = rect.midY - sin(t * .pi * 3) * envelope * rect.height / 2
            let pt = CGPoint(x: rect.minX + t * rect.width, y: y)
            if i == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
        }
        return p
    }
}
