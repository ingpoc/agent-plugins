import XCTest
@testable import CUAService

final class IslandGateInputParserTests: XCTestCase {
    func testXInputs() {
        for input in ["@name", "name", "x.com/name", "https://www.twitter.com/name", "https://x.com/name/status/123"] {
            XCTAssertEqual(IslandGateInputParser.parseX(input)?.handle, "name", input)
            XCTAssertNil(IslandGateInputParser.parseX(input)?.link, input)
        }
        let status = IslandGateInputParser.parseX("twitter.com/name/status/123/extra")
        XCTAssertEqual(status?.handle, "name")
        XCTAssertNil(status?.link)
        for input in ["", "@", "abcdefghijklmnop", "name/status/nope", "name/extra", "https://example.com/name"] {
            XCTAssertNil(IslandGateInputParser.parseX(input), input)
        }
    }
    func testLinkedInProfileOnlyInputs() {
        for input in ["Guru-Name", "linkedin.com/in/alice-abc", "https://www.linkedin.com/in/alice-abc", "https://uk.linkedin.com/in/alice-abc/"] {
            XCTAssertNotNil(IslandGateInputParser.parseLinkedIn(input), input)
        }
        for input in ["http://linkedin.com/in/alice", "https://evil.com/in/alice", "linkedin.com/posts/alice_abc", "https://www.linkedin.com/feed/update/urn:li:activity:123", "https://usa.linkedin.com/in/alice"] {
            XCTAssertNil(IslandGateInputParser.parseLinkedIn(input), input)
        }
    }
    func testLinkedInVanityProfileForms() {
        for input in ["Some-Name", "linkedin.com/in/Some-Name", "https://uk.linkedin.com/in/Some-Name/"] {
            XCTAssertEqual(IslandGateInputParser.parseLinkedIn(input)?.path.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "/")), "in/some-name", input)
        }
        for input in ["ab", "some_name", "https://evil.com/in/some-name"] {
            XCTAssertNil(IslandGateInputParser.parseLinkedIn(input), input)
        }
    }
}
