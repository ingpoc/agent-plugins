import XCTest
@testable import CUAService

@MainActor
final class IslandConversationTests: XCTestCase {
    /// Voice states hug the voice slot (Guru 14:17 IST): the width follows only the slot's content
    /// (meter 23 pt, thinking dots 14 pt), never loudness, and never dips through the 74 pt pinch.
    func testRoutineVoiceStatesKeepTheirWidth() async throws {
        let m = IslandModel()
        m.reduceMotion = false
        m.systemReduceMotion = false
        m.apply(IslandSnapshot(kind: "listening", title: "Listening"))
        try await Task.sleep(nanoseconds: 220_000_000)
        var dips = 0
        for kind in ["thinking", "speaking", "listening", "acting"] {
            m.apply(IslandSnapshot(kind: kind, title: kind))
            if let voice = IslandModel.voiceSlotContent(for: m.presentation, controlled: false) {
                if m.drawnWidth != IslandBarLayout.voicePillWidth(voice) { dips += 1 }
            } else if m.drawnWidth == IslandBarLayout.idleWidth { dips += 1 }
            try await Task.sleep(nanoseconds: 220_000_000)
        }
        print("conversation routine width dips: \(dips)")
        XCTAssertEqual(dips, 0)
    }

    func testSuccessStaysCompactButCanBeOpened() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "done", title: "Calculator is open", app: "Calculator"))
        XCTAssertFalse(m.cardOpen)
        m.toggleCard()
        XCTAssertTrue(m.cardOpen)
    }

    func testConversationPauseDoesNotSwapToMusicImmediately() {
        let m = IslandModel()
        let t = Date()
        m.setNowPlaying(NowPlayingTests.track(), visible: true, now: t)
        m.apply(IslandSnapshot(kind: "speaking", title: "Speaking"), now: t)
        m.apply(IslandSnapshot(kind: "listening", title: "Listening"), now: t.addingTimeInterval(0.5))
        XCTAssertEqual(m.presentation, .listening)
        m.tick(now: t.addingTimeInterval(4))
        XCTAssertEqual(m.presentation, .nowPlaying)
    }

    func testAudibleMeterExtendsConversationAndSilenceDoesNot() {
        let m = IslandModel()
        let t = Date()
        m.setNowPlaying(NowPlayingTests.track(), visible: true, now: t)
        m.apply(IslandSnapshot(kind: "listening", title: "Listening"), now: t)
        var meter = IslandSnapshot(kind: "voice_meter")
        meter.voice_side = "user"
        meter.voice_level = 0.4
        m.applyMeter(meter, now: t.addingTimeInterval(2), force: true)
        m.tick(now: t.addingTimeInterval(4))
        XCTAssertEqual(m.presentation, .listening)
        meter.voice_level = 0
        m.applyMeter(meter, now: t.addingTimeInterval(4), force: true)
        m.tick(now: t.addingTimeInterval(6))
        XCTAssertEqual(m.presentation, .nowPlaying)
    }

    func testUrgentStatesStillInterruptConversation() {
        let m = IslandModel()
        m.apply(IslandSnapshot(kind: "speaking", title: "Speaking"))
        m.apply(IslandSnapshot(kind: "reconnecting", title: "Reconnecting"))
        XCTAssertEqual(m.presentation, .reconnecting)
        m.apply(IslandSnapshot(kind: "error", title: "label not found", app: "Calculator"))
        XCTAssertEqual(m.presentation, .error)
        XCTAssertTrue(m.cardOpen)
        m.apply(IslandSnapshot(kind: "confirm", title: "Confirm", confirm_id: "request", confirm_prompt: "Allow?"))
        XCTAssertEqual(m.presentation, .needsYou)
        XCTAssertTrue(m.cardOpen)
    }
}
