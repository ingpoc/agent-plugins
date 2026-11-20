import XCTest
@testable import CUAService

final class SamanthaMenuStatusTests: XCTestCase {
    func testFailureRemainsVisibleUntilTheNextAttempt() {
        var status = SamanthaMenuStatus()
        status.fail(category: "helper_missing", detail: "Bundled voice helper is missing.")

        XCTAssertEqual(status.label, "Samantha — Failed")
        XCTAssertEqual(status.failureText, "Samantha failed — helper_missing: Bundled voice helper is missing.")

        status.beginAttempt()
        XCTAssertNil(status.failureText)
        XCTAssertEqual(status.label, "Samantha")
    }

    func testSuccessfulStartClearsFailure() {
        var status = SamanthaMenuStatus()
        status.fail(category: "mic_denied", detail: "Microphone access was not granted.")

        status.startSucceeded()

        XCTAssertNil(status.failureText)
        XCTAssertEqual(status.label, "Samantha")
    }
}
