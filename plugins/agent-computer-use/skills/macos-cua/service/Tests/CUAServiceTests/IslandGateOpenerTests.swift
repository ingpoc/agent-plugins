import XCTest
@testable import CUAService

@MainActor
final class IslandGateOpenerTests: XCTestCase {
    func testDefaultOpenerIsRecording() {
        XCTAssertTrue(IslandGate().urlOpener is IslandGateRecordingURLOpener)
    }

    func testSystemOpenerRefusesUnderTests() {
        let url = URL(string: "https://example.invalid/test")!
        IslandGateSystemURLOpener.refused.removeAll()
        IslandGateSystemURLOpener().open(url)
        XCTAssertEqual(IslandGateSystemURLOpener.refused, [url])
    }

    func testFixtureUsesRecordingOpener() throws {
        for name in ["01-start", "02-followed", "03-ready", "04-checking", "05-unlocked", "06-error-offline", "07-error-notfound", "08-waking", "09-linkedin", "locked-x", "locked-x-ready", "locked-x-no-launch", "locked-x-prelaunch", "locked-linkedin", "pending-follow", "trial-3days", "trial-nudge-day2", "trial-lastday", "trial-prelaunch", "trial-expired", "repost-checking", "unlocked"] {
            let gate = try IslandGateFixtures.gate(name)
            XCTAssertTrue(gate.urlOpener is IslandGateRecordingURLOpener, name)
        }
    }
    func testRealOpenValidation() {
        XCTAssertNoThrow(try IslandGateValidatedURLOpener.validate(URL(string: "https://x.com/gurusharan")!))
        XCTAssertNoThrow(try IslandGateValidatedURLOpener.validate(URL(string: "https://x.com/intent/retweet?tweet_id=123")!))
        XCTAssertNoThrow(try IslandGateValidatedURLOpener.validate(URL(string: "https://www.linkedin.com/in/guru")!))
        for value in ["http://x.com/foo", "https://placeholder.example/foo", "https://example.com/REPLACE_WITH"] {
            XCTAssertThrowsError(try IslandGateValidatedURLOpener.validate(URL(string: value)!))
        }
        for value in ["https://x.com/intent/post", "https://linkedin.com/", "https://ca.linkedin.com/posts/launch", "https://x.com/intent/retweet?tweet_id=abc"] {
            XCTAssertThrowsError(try IslandGateValidatedURLOpener.validate(URL(string: value)!))
        }
    }
}
