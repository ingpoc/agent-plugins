import XCTest
@testable import CUAService

final class IslandLocalHarnessScannerTests: XCTestCase {
    func testRowsUseOnlyAllowlistedNamesAndPositivePIDs() {
        let rows = IslandLocalHarnessScanner.rows(processList: "PID COMM\n42 codex\n0 claude\n43 claude\n99 unrecognized-agent\n")

        XCTAssertEqual(rows.map(\.id), ["local:codex:42", "local:claude:43"])
        XCTAssertEqual(rows.map(\.name), ["Codex", "Claude Code"])
        XCTAssertTrue(rows.allSatisfy { $0.status == "harness" && $0.parent_id == nil && $0.controls.isEmpty })
    }
}
