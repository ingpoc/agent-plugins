import XCTest
@testable import CUAService

final class IslandLocalHarnessScannerTests: XCTestCase {
    func testRowsUseOnlyAllowlistedNamesAndPositivePIDs() {
        let rows = IslandLocalHarnessScanner.rows(processList: "PID COMM\n42 codex\n0 claude\n43 claude\n57 /Applications/Comet.app/Contents/MacOS/Comet\n99 unrecognized-agent\n")

        XCTAssertEqual(rows.map(\.id), ["local:codex:42", "local:claude:43", "local:Comet:57"])
        XCTAssertEqual(rows.map(\.name), ["Codex", "Claude Code", "Comet"])
        XCTAssertTrue(rows.allSatisfy { $0.status == "process" && $0.parent_id == nil && $0.controls.isEmpty })
        XCTAssertEqual(rows.map(\.title), ["PID 42", "PID 43", "PID 57"])
    }
}
