import XCTest
@testable import CUAService

/// Two ACU menu bar icons (2026-11-04 11:18 IST): a second launch of the installed app hands off and exits.
final class SingleInstanceTests: XCTestCase {
    private let id = CUAServiceSingleInstance.bundleID

    func testSecondCopyOfTheAppHandsOffToTheRunningOne() {
        XCTAssertEqual(CUAServiceSingleInstance.decide(ownBundleID: id, ownPID: 7185, runningPIDs: [3996, 7185], lockAcquired: true),
                       .handOff(existing: 3996), "another running copy wins even if the lock file was wiped")
    }

    func testLockHeldElsewhereHandsOff() {
        XCTAssertEqual(CUAServiceSingleInstance.decide(ownBundleID: id, ownPID: 7185, runningPIDs: [7185], lockAcquired: false),
                       .handOff(existing: nil))
    }

    func testOnlyCopyWithTheLockProceeds() {
        XCTAssertEqual(CUAServiceSingleInstance.decide(ownBundleID: id, ownPID: 3996, runningPIDs: [3996], lockAcquired: true), .proceed)
    }

    func testTestAndDevBinariesAreNotGuarded() {
        XCTAssertEqual(CUAServiceSingleInstance.decide(ownBundleID: nil, ownPID: 1, runningPIDs: [2], lockAcquired: false), .proceed)
        XCTAssertEqual(CUAServiceSingleInstance.decide(ownBundleID: "com.example.other", ownPID: 1, runningPIDs: [2], lockAcquired: false), .proceed)
    }

    func testLockIsExclusiveAndLivesOutsideTheCache() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("acu-single-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let path = dir.appendingPathComponent("instance.lock").path
        let first = CUAServiceSingleInstance.acquire(path: path)
        XCTAssertGreaterThanOrEqual(first, 0)
        XCTAssertEqual(CUAServiceSingleInstance.acquire(path: path), -1, "a second holder is refused")
        close(first)
        let again = CUAServiceSingleInstance.acquire(path: path)
        XCTAssertGreaterThanOrEqual(again, 0)
        close(again)
        XCTAssertTrue(CUAServiceSingleInstance.lockPath(home: URL(fileURLWithPath: "/Users/x"))
            .hasPrefix("/Users/x/Library/Application Support/CUAService/"), "not under ~/.cache")
    }
}
