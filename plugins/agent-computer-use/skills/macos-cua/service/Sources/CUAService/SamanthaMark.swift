import SwiftUI

/// Samantha's own harness mark (Guru, 2026-11-04 11:09 IST: "the style of samantha should be distinct").
/// She is a voice agent, so every variant is built from voice: her own violet-to-rose colour (not the
/// cyan working dot, nor a status colour), on the island's black circle. Never a white plugin tile
/// (comet-control, context-ledger) and never ACU's app icon. Vector only, so it is crisp at 1x and 2x.
enum SamanthaMarkVariant: String, CaseIterable {
    /// A: a violet-to-rose voice orb with three white voice bars.
    case orb
    /// B: one continuous sound wave, a gradient ribbon across the black circle.
    case wave
    /// C: a white rounded "S" inside a violet-to-rose gradient ring.
    case monogram

    /// Review default until Guru picks; `ACU_SAMANTHA_MARK=orb|wave|monogram` previews another.
    nonisolated(unsafe) static var current: SamanthaMarkVariant =
        ProcessInfo.processInfo.environment["ACU_SAMANTHA_MARK"].flatMap(SamanthaMarkVariant.init(rawValue:)) ?? .orb
}

enum SamanthaMarkPalette {
    static let violet = Color(red: 0xA0 / 255, green: 0x6B / 255, blue: 0xFF / 255)  // #A06BFF
    static let rose = Color(red: 0xFF / 255, green: 0x6F / 255, blue: 0xB5 / 255)    // #FF6FB5
    static var gradient: LinearGradient {
        LinearGradient(colors: [violet, rose], startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

/// Drawn inside the 24 pt harness circle (`SamanthaSourceCircle` keeps the black fill, key line and badges).
struct SamanthaMark: View {
    var variant: SamanthaMarkVariant = .current
    var diameter: CGFloat = IslandBarLayout.circleDiameter

    var body: some View {
        switch variant {
        case .orb:
            ZStack {
                Circle().fill(SamanthaMarkPalette.gradient)
                    .frame(width: diameter * 0.75, height: diameter * 0.75)
                HStack(spacing: diameter / 16) {
                    ForEach(Array([0.22, 0.4, 0.28].enumerated()), id: \.offset) { _, h in
                        Capsule().fill(Color.white)
                            .frame(width: diameter / 12, height: diameter * h)
                    }
                }
            }
        case .wave:
            SamanthaWaveShape()
                .stroke(SamanthaMarkPalette.gradient, style: StrokeStyle(lineWidth: diameter / 12, lineCap: .round, lineJoin: .round))
                .frame(width: diameter * 0.62, height: diameter * 0.42)
        case .monogram:
            ZStack {
                Circle().strokeBorder(SamanthaMarkPalette.gradient, lineWidth: diameter / 14)
                    .frame(width: diameter - 2, height: diameter - 2)
                Text("S")
                    .font(.system(size: diameter * 0.5, weight: .heavy, design: .rounded))
                    .foregroundStyle(Color.white)
                    .offset(y: -diameter / 48)
            }
        }
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
