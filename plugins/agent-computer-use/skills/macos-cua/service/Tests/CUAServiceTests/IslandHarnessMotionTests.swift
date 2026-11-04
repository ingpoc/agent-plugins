import XCTest
@testable import CUAService

@MainActor
final class IslandHarnessMotionTests: XCTestCase {
    private let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task", status: "running", last_seen_at: 100)
    private let samantha = IslandAgent(id: "agent:Samantha", name: "Samantha", app: "Samantha", title: "Voice", status: "running", last_seen_at: 100)

    func testHarnessEnterAndExitKeepTheCircleInItsAssignedSlot() {
        let model = IslandModel()
        model.idleActivity = "duo"
        model.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        let codexSlot = try! XCTUnwrap(model.sourceGroupSlots["Codex"])
        let codexBefore = IslandBarLayout.agentRects(shape: model.shapeRect, slots: [codexSlot])[0]

        model.apply(IslandSnapshot(kind: "listening", agents: [codex, samantha]))
        let samanthaSlot = try! XCTUnwrap(model.sourceGroupSlots[IslandSourceGroups.samantha])
        let codexAfter = IslandBarLayout.agentRects(shape: model.shapeRect, slots: [codexSlot])[0]
        let samanthaRect = IslandBarLayout.agentRects(shape: model.shapeRect, slots: [samanthaSlot])[0]
        XCTAssertEqual(codexAfter.minX, codexBefore.minX, accuracy: 0.001)
        XCTAssertGreaterThan(samanthaSlot, codexSlot, "a new circle joins at the outermost left slot")

        for step in 0...20 {
            let progress = CGFloat(step) / 20
            let diameter = samanthaRect.width * (IslandMotion.harnessEnterScale
                + (1 - IslandMotion.harnessEnterScale) * progress)
            let frame = CGRect(x: samanthaRect.midX - diameter / 2, y: samanthaRect.midY - diameter / 2,
                               width: diameter, height: diameter)
            XCTAssertGreaterThanOrEqual(codexAfter.minX - frame.maxX, IslandBarLayout.circleSpacing - 0.001,
                                        "no overlap while entering at step \(step)")
        }

        model.apply(IslandSnapshot(kind: "idle", agents: [codex]))
        let codexAfterRemoval = IslandBarLayout.agentRects(shape: model.shapeRect, slots: [codexSlot])[0]
        XCTAssertEqual(codexAfterRemoval.minX, codexBefore.minX, accuracy: 0.001,
                       "removing one source never moves the remaining circle")
        for step in 0...20 {
            let progress = CGFloat(step) / 20
            let diameter = samanthaRect.width * (1 - (1 - IslandMotion.harnessEnterScale) * progress)
            let frame = CGRect(x: samanthaRect.midX - diameter / 2, y: samanthaRect.midY - diameter / 2,
                               width: diameter, height: diameter)
            XCTAssertGreaterThanOrEqual(codexAfterRemoval.minX - frame.maxX, IslandBarLayout.circleSpacing - 0.001,
                                        "no overlap while leaving at step \(step)")
        }
    }

    func testHarnessTransitionScalesAndFadesWithoutChangingItsSlot() {
        XCTAssertEqual(IslandMotion.harnessEnterScale, 0.6)
        XCTAssertEqual(IslandMotion.harness(reduce: false), IslandMotion.widthCurve)
        XCTAssertNil(IslandMotion.harness(reduce: true))
        _ = IslandMotion.harnessTransition(reduce: false)
    }
}
