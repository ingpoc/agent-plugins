import XCTest
@testable import CUAService

final class IslandAccessibilityTests: XCTestCase {
    func testAnnouncementThrottleUsesInjectedClock() {
        var now = Date(timeIntervalSince1970: 100)
        var throttle = IslandAnnouncementThrottle(clock: { now })
        XCTAssertEqual(throttle.offer("one", priority: .medium)?.text, "one")
        now.addTimeInterval(2.99)
        XCTAssertNil(throttle.offer("two", priority: .medium))
        XCTAssertNil(throttle.drain())
        now.addTimeInterval(0.01)
        XCTAssertEqual(throttle.drain()?.text, "two")
        XCTAssertNil(throttle.nextDue)
    }

    func testAnnouncementThrottleNeverLetsRoutineSwallowApproval() {
        var now = Date(timeIntervalSince1970: 100)
        var throttle = IslandAnnouncementThrottle(clock: { now })
        XCTAssertNotNil(throttle.offer("focus", priority: .medium))
        now.addTimeInterval(0.5)
        XCTAssertNil(throttle.offer("Codex needs approval", priority: .high))
        now.addTimeInterval(0.5)
        XCTAssertNil(throttle.offer("done", priority: .medium))
        XCTAssertEqual(throttle.nextDue, Date(timeIntervalSince1970: 103))
        now = Date(timeIntervalSince1970: 103)
        let delivered = throttle.drain()
        XCTAssertEqual(delivered?.text, "Codex needs approval")
        XCTAssertEqual(delivered?.priority, .high)
        // Still at most one per 3 s after the drain.
        now.addTimeInterval(1)
        XCTAssertNil(throttle.offer("later", priority: .high))
        XCTAssertEqual(throttle.nextDue, Date(timeIntervalSince1970: 106))
    }

    func testAnnouncementPriorities() {
        for trigger in [IslandAnnouncementTrigger.approval, .choice, .needsYou] {
            XCTAssertEqual(trigger.priority, .high)
        }
        for trigger in [IslandAnnouncementTrigger.error, .done, .expiry] {
            XCTAssertEqual(trigger.priority, .medium)
        }
    }

    func testAgentBadges() {
        XCTAssertFalse(IslandAgentBadge.showsCircle(rowCount: 0))
        XCTAssertTrue(IslandAgentBadge.showsCircle(rowCount: 1))
        XCTAssertNil(IslandAgentBadge.count(needsYou: 0))
        XCTAssertEqual(IslandAgentBadge.count(needsYou: 3), 3)
    }

    func testAgentLabels() {
        XCTAssertEqual(IslandAgentAccessibility.label(running: 0, needsYou: 0), "Agents: 0 running")
        XCTAssertEqual(IslandAgentAccessibility.label(running: 1, needsYou: 1), "Agents: 1 running, 1 needs you")
        XCTAssertEqual(IslandAgentAccessibility.label(running: 2, needsYou: 2), "Agents: 2 running, 2 need you")
    }
}

@MainActor
final class IslandAccessibilityModelTests: XCTestCase {
    func testShortcutPersistsKeyCodeAndModifiers() {
        let suite = "cua.shortcut.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
        let shortcut = IslandShortcut(keyCode: 11, modifiers: 0x900, character: "b")
        shortcut.save(store)
        XCTAssertEqual(IslandShortcut.read(store), shortcut)
        XCTAssertTrue(IslandShortcut.read(store).spoken.contains("B"))
        store.set(["keyCode": 11, "modifiers": 0, "character": "b"], for: IslandSettingsSchema.shortcut)
        XCTAssertEqual(IslandShortcut.read(store), .default)
    }

    func testStateEventsAnnounceWithPriority() {
        let now = Date()
        func capture(_ snapshot: IslandSnapshot) -> (String, IslandAnnouncementPriority) {
            let model = IslandModel()
            var text = ""
            var priority = IslandAnnouncementPriority.medium
            model.onAnnounce = { text = $0; priority = model.announcementPriority }
            model.apply(snapshot, now: now)
            return (text, priority)
        }
        for kind in ["approval", "choice"] {
            let a = IslandAgentApproval(id: kind, agent: "Agent", app: "App", title: "Review", kind: kind)
            let result = capture(IslandSnapshot(kind: "idle", agent_approval: a))
            XCTAssertTrue(result.0.contains("Review"))
            XCTAssertEqual(result.1, .high)
        }
        let urgent = capture(IslandSnapshot(kind: "idle", agents: [IslandAgent(id: "u", name: "Agent", title: "Review", status: "needs_you")]))
        XCTAssertTrue(urgent.0.contains("Review"))
        XCTAssertEqual(urgent.1, .high)
        for kind in ["error", "done"] {
            let result = capture(IslandSnapshot(kind: kind, title: "Result"))
            XCTAssertFalse(result.0.isEmpty)
            XCTAssertEqual(result.1, .medium)
        }
        let model = IslandModel()
        var texts: [String] = []
        model.onAnnounce = { texts.append($0) }
        var a = IslandAgentApproval(id: "expiring", agent: "Agent", app: "App", title: "Review")
        a.expires_at = now.addingTimeInterval(70).timeIntervalSince1970
        model.apply(IslandSnapshot(kind: "idle", agent_approval: a), now: now)
        _ = model.tick(now: now.addingTimeInterval(11))
        XCTAssertTrue(texts.contains { $0.contains("seconds remaining") })
    }

    func testApprovalLabelPrecedesOtherContent() {
        let model = IslandModel()
        model.shortcutSpoken = "Option-Command-B"
        let approval = IslandAgentApproval(id: "x", agent: "Agent", app: "App", title: "Delete branch")
        model.apply(IslandSnapshot(kind: "idle", agents: [IslandAgent(id: "a", name: "Other", status: "running")], agent_approval: approval))
        XCTAssertTrue(model.accessibilityText.hasPrefix("Agent needs approval: Delete branch"))
        XCTAssertTrue(model.accessibilityText.contains("Option-Command-B to review"))
    }

    func testVoiceOverPeekSurvivesPointerExit() {
        let model = IslandModel()
        model.voiceOverEnabled = { true }
        model.apply(IslandSnapshot(kind: "error", title: "Could not open Notes"))
        XCTAssertTrue(model.cardOpen)
        model.pointerLeft()
        XCTAssertTrue(model.cardOpen, "VoiceOver hold must last until an explicit dismissal")
        model.dismissCard()
        XCTAssertFalse(model.cardOpen)
    }

    func testEmptyRosterOpensAndSurvivesLastAgentLeaving() {
        let model = IslandModel()
        model.openAgentList()
        XCTAssertTrue(model.cardOpen)
        XCTAssertEqual(model.cardMode, .agents)
        XCTAssertTrue(model.listRows.isEmpty)
        let other = IslandModel()
        other.apply(IslandSnapshot(kind: "ready", title: "Samantha", agents: [IslandAgent(id: "a", name: "Codex", title: "Build", status: "running")]))
        other.openAgentList()
        other.apply(IslandSnapshot(kind: "ready", title: "Samantha", agents: []))
        XCTAssertTrue(other.cardOpen)
        XCTAssertEqual(other.cardMode, .agents)
    }

    func testAgentCircleCountsAgentsAndUnseenTimeoutsOnly() {
        let model = IslandModel()
        model.apply(IslandSnapshot(kind: "idle"))
        XCTAssertFalse(model.showAgentCircle)
        // A timed-out request keeps its hollow ring until the roster is opened (never dropped silently).
        var timedOut = IslandSnapshot(kind: "idle")
        timedOut.expired = [IslandExpired(id: "old", agent: "Codex", app: "Codex", title: "Old request")]
        model.apply(timedOut)
        XCTAssertTrue(model.showAgentCircle)
        model.apply(IslandSnapshot(kind: "idle"))
        XCTAssertFalse(model.showAgentCircle)
        model.apply(IslandSnapshot(kind: "idle", agents: [IslandAgent(id: "a", name: "Codex", title: "Build", status: "running")]))
        XCTAssertTrue(model.showAgentCircle)
    }
}
