import AppKit
import QuartzCore
import XCTest
@testable import CUAService

/// Guru 2026-11-01 19:46 IST: "Account details…" opened "Your island is on" and the card kept swinging up and
/// down, clipping "Shown on the Agent Plugins site" behind Done. The window first laid the card out at a
/// 500 pt placeholder height (centred, 35 pt low) and the wave's withAnimation(repeatForever) caught the
/// shrink to fit. The window must open at its content size and then stay still, wave and all.
@MainActor
final class IslandGateWindowStillTests: XCTestCase {
    private func bigLayerFrames(_ root: CALayer) -> [CGRect] {
        var out: [CGRect] = []
        func walk(_ l: CALayer) {
            let p = l.presentation() ?? l
            let f = root.convert(p.bounds, from: p)
            if f.width > 100 { out.append(f.integral) }   // the 2 pt wave capsules are meant to move
            (l.sublayers ?? []).forEach(walk)
        }
        walk(root)
        return out
    }

    func testAccountDetailsWindowOpensAtContentSizeAndStaysStill() throws {
        for name in ["unlocked", "trial-3days"] {
            let gate = try IslandGateFixtures.gate(name)
            gate.liveMotion = true                     // the live wave, not the fixture still
            gate.show()
            let window = try XCTUnwrap(NSApp.windows.first { $0.title == "Unlock Dynamic Island" && $0.isVisible })
            defer { window.close() }
            let content = try XCTUnwrap(window.contentView)
            let root = try XCTUnwrap(content.layer)
            // First frame: already at content size, the card at the top (no placeholder layout to move from).
            XCTAssertEqual(window.frame.height, content.fittingSize.height, accuracy: 0.5, name)
            XCTAssertFalse(bigLayerFrames(root).contains { $0.minY < 0 || $0.maxY > content.bounds.height + 1 }, "\(name): nothing outside the window")
            let first = bigLayerFrames(root).filter { $0.size == content.bounds.size }.map(\.minY)
            XCTAssertTrue(first.allSatisfy { $0 == 0 }, "\(name): card starts at the top, not offset \(first)")
            RunLoop.main.run(until: Date().addingTimeInterval(0.3))
            let settledFrame = window.frame, settledSize = content.fittingSize, settled = bigLayerFrames(root)
            // 5 s would match the live check; 2 s covers three wave periods (0.65 s autoreverse).
            for _ in 0..<20 {
                RunLoop.main.run(until: Date().addingTimeInterval(0.1))
                XCTAssertEqual(window.frame, settledFrame, "\(name): window frame moved")
                XCTAssertEqual(content.fittingSize, settledSize, "\(name): content size changed")
                XCTAssertEqual(bigLayerFrames(root), settled, "\(name): card content moved")
            }
        }
    }
}
