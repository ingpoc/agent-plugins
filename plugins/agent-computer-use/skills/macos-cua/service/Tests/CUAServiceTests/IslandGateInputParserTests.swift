import XCTest
@testable import CUAService

final class IslandGateInputParserTests: XCTestCase {
    func testXInputs() {
        for input in ["@name", "name", "x.com/name", "https://www.twitter.com/name", "http://mobile.x.com/name"] {
            XCTAssertEqual(IslandGateInputParser.parseX(input)?.handle, "name", input)
            XCTAssertNil(IslandGateInputParser.parseX(input)?.link, input)
        }
        let status = IslandGateInputParser.parseX("twitter.com/name/status/123/extra")
        XCTAssertEqual(status?.handle, "name")
        XCTAssertEqual(status?.link?.absoluteString, "https://x.com/name/status/123")
        for input in ["", "@", "abcdefghijklmnop", "name/status/nope", "name/extra", "https://example.com/name"] {
            XCTAssertNil(IslandGateInputParser.parseX(input), input)
        }
    }
    func testLinkedInPostLinks() {
        for input in ["linkedin.com/posts/alice_abc", "https://www.linkedin.com/feed/update/urn:li:activity:123"] {
            XCTAssertNotNil(IslandGateInputParser.parseLinkedIn(input), input)
        }
        for input in ["linkedin.com/in/alice", "http://linkedin.com/posts/a", "https://evil.com/posts/a", "linkedin.com/feed/update/urn:li:activity:nope", "linkedin.com/posts/"] {
            XCTAssertNil(IslandGateInputParser.parseLinkedIn(input), input)
        }
    }
}
