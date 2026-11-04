import XCTest
@testable import CUAService

@MainActor
final class IslandHarnessMotionTests: XCTestCase {
    private let codex = IslandAgent(id: "agent:Codex", name: "Codex", app: "Codex", title: "Task", status: "running", last_seen_at: 100)
    private let samantha = IslandAgent(id: "agent:Samantha", name: "Samantha", app: "Samantha", title: "Voice", status: "running", last_seen_at: 100)

    private func apply(_ names: [String], to model: IslandModel, now: Date) {
        let agents = names.map {
            IslandAgent(id: "agent:\($0)", name: $0, app: $0, title: "Task", status: "running",
                        last_seen_at: now.timeIntervalSince1970)
        }
        model.apply(IslandSnapshot(kind: "idle", agents: agents), now: now)
    }

    private func waitForHarnessExit() async {
        try? await Task.sleep(nanoseconds: 250_000_000)
    }

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

    func testHarnessExitReservesAnimatedSlotAndReusesInnerSlots() async {
        let model = IslandModel()
        model.idleActivity = "duo"
        let now = Date()
        var names = ["Codex", "Cursor", "Grok Bot"]
        apply(names, to: model, now: now)

        var before = model.sourceGroupSlots
        XCTAssertEqual(Set(before.values), Set(0..<IslandSourceGroups.maxCircles))
        let leaving = try! XCTUnwrap(before.max { $0.value < $1.value })
        let survivors = before.filter { $0.key != leaving.key }
        names = names.filter { $0 != leaving.key } + ["Claude"]
        apply(names, to: model, now: now.addingTimeInterval(0.1))

        var during = model.sourceGroupSlots
        let enteringSlot = try! XCTUnwrap(during["Claude"])
        XCTAssertNotEqual(enteringSlot, leaving.value, "the outgoing slot remains reserved during its fade")
        XCTAssertEqual(Set(during.values).count, during.count, "active circles have distinct slots")
        for (id, slot) in survivors { XCTAssertEqual(during[id], slot, "survivors stay in place") }

        let shape = model.shapeRect
        let outgoing = IslandBarLayout.agentRects(shape: shape, slots: [leaving.value])[0]
        let incoming = IslandBarLayout.agentRects(shape: shape, slots: [enteringSlot])[0]
        for step in 0...20 {
            let progress = CGFloat(step) / 20
            let outgoingDiameter = outgoing.width * (1 - (1 - IslandMotion.harnessEnterScale) * progress)
            let incomingDiameter = incoming.width * (IslandMotion.harnessEnterScale
                + (1 - IslandMotion.harnessEnterScale) * progress)
            let outgoingFrame = CGRect(x: outgoing.midX - outgoingDiameter / 2, y: outgoing.midY - outgoingDiameter / 2,
                                       width: outgoingDiameter, height: outgoingDiameter)
            let incomingFrame = CGRect(x: incoming.midX - incomingDiameter / 2, y: incoming.midY - incomingDiameter / 2,
                                       width: incomingDiameter, height: incomingDiameter)
            XCTAssertFalse(outgoingFrame.intersects(incomingFrame), "exit and entry do not overlap at step \(step)")
        }

        await waitForHarnessExit()
        during = model.sourceGroupSlots
        XCTAssertEqual(during["Claude"], leaving.value, "the slot is reused after the exit transition")
        for (id, slot) in survivors { XCTAssertEqual(during[id], slot, "survivors still stay in place") }

        for cycle in 0..<6 {
            before = model.sourceGroupSlots
            let inner = try! XCTUnwrap(before.min { $0.value < $1.value })
            let nextName = "Worker \(cycle)"
            let stable = before.filter { $0.key != inner.key }
            names = names.filter { $0 != inner.key } + [nextName]
            apply(names, to: model, now: now.addingTimeInterval(1 + Double(cycle) * 0.3))
            during = model.sourceGroupSlots
            XCTAssertNotEqual(during[nextName], inner.value, "the leaving inner slot is reserved during cycle \(cycle)")
            for (id, slot) in stable { XCTAssertEqual(during[id], slot, "existing circle stays in place") }

            await waitForHarnessExit()
            let settled = model.sourceGroupSlots
            XCTAssertEqual(Set(settled.values), Set(0..<IslandSourceGroups.maxCircles), "slots stay on-panel after cycle \(cycle)")
            for (id, slot) in stable { XCTAssertEqual(settled[id], slot, "existing circle stays in place after cycle") }
        }
    }
}
