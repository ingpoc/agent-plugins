import AVFoundation
import Darwin
import XCTest
@testable import CUAService

final class StatusStore: IslandGateStoring {
    var values: [String: Data] = [:]
    private(set) var writes = 0
    private(set) var deletes = 0

    func read(_ name: String) throws -> Data? { values[name] }
    func write(_ data: Data, name: String) throws { writes += 1; values[name] = data }
    func delete(_ name: String) throws { deletes += 1; values.removeValue(forKey: name) }
}

final class StatusTests: XCTestCase {
    func testStatusJSONHasStableRequiredShape() throws {
        let value = CUAServiceStatus(
            version: "1.2.3", build: "42", bundleId: "com.ingpoc.cua-service", path: "/Applications/Agent Computer Use.app",
            running: true,
            permissions: .init(accessibility: true, screen_recording: false, microphone: "not_determined"),
            island: .init(state: "checking", provider: nil, handle: nil, expires_at: nil),
            claude_desktop: IslandClaudeDesktopStatus.current(),
            ok: true
        )
        let data = try JSONEncoder().encode(value)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(object.keys), Set(["version", "build", "bundleId", "path", "running", "permissions", "island", "claude_desktop", "ok"]))
        let permissions = try XCTUnwrap(object["permissions"] as? [String: Any])
        XCTAssertEqual(Set(permissions.keys), Set(["accessibility", "screen_recording", "microphone"]))
        XCTAssertEqual(permissions["microphone"] as? String, "not_determined")
        let island = try XCTUnwrap(object["island"] as? [String: Any])
        XCTAssertEqual(Set(island.keys), Set(["state", "provider", "handle", "expires_at"]))
        XCTAssertEqual(island["state"] as? String, "checking")
        XCTAssertTrue(object["running"] as? Bool == true)
        XCTAssertTrue(object["ok"] as? Bool == true)
    }

    func testMicrophoneStatusIsReadOnlyMapping() {
        XCTAssertEqual(CUAServiceStatus.microphoneStatus(.authorized), "granted")
        XCTAssertEqual(CUAServiceStatus.microphoneStatus(.denied), "denied")
        XCTAssertEqual(CUAServiceStatus.microphoneStatus(.restricted), "denied")
        XCTAssertEqual(CUAServiceStatus.microphoneStatus(.notDetermined), "not_determined")
    }

    func testClaudeDesktopStatusShapeEncodesMetadataOnly() throws {
        IslandClaudeDesktopStatus.update(pid: nil,
            snapshot: .init(rows: [.init(status: .working, title: "Task"), .init(status: .idle, title: "Old")],
                             modelLabel: "Model: Sonnet 5.5 Medium", chatTabActive: true),
            readAt: Date(timeIntervalSince1970: 1_700_000_000), attempts: 2, lastError: nil)
        let status = IslandClaudeDesktopStatus.current()
        XCTAssertNil(status.attached_pid)
        XCTAssertEqual(status.row_count, 2)
        XCTAssertEqual(status.rows_by_status["working"], 1)
        XCTAssertEqual(status.rows_by_status["idle"], 1)
        XCTAssertEqual(status.rows_by_status["error"], 0)
        XCTAssertTrue(status.model_picker_seen)
        XCTAssertTrue(status.manual_accessibility.set)
        XCTAssertEqual(status.manual_accessibility.attempts, 2)
        XCTAssertTrue(status.chat_tab_active)
        XCTAssertTrue(status.last_read_at?.hasSuffix("Z") == true)
        let data = try JSONEncoder().encode(status)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNil(object["attached_pid"] as? Int)
        XCTAssertNotNil(object["attached_pid"] as? NSNull)
        XCTAssertNil(object["last_read_at"] as? NSNull)
        XCTAssertNil(String(data: data, encoding: .utf8)?.range(of: "Task"))
    }

    func testReadyRequiresRunningServiceAndAllRequiredPermissions() {
        let ready = CUAServiceStatus.Permissions(accessibility: true, screen_recording: true, microphone: "granted")
        XCTAssertTrue(CUAServiceStatus.isReady(running: true, permissions: ready))
        XCTAssertFalse(CUAServiceStatus.isReady(running: false, permissions: ready))
        XCTAssertFalse(CUAServiceStatus.isReady(running: true, permissions: .init(accessibility: false, screen_recording: true, microphone: "granted")))
        XCTAssertFalse(CUAServiceStatus.isReady(running: true, permissions: .init(accessibility: true, screen_recording: false, microphone: "granted")))
        XCTAssertFalse(CUAServiceStatus.isReady(running: true, permissions: .init(accessibility: true, screen_recording: true, microphone: "denied")))
        XCTAssertFalse(CUAServiceStatus.isReady(running: true, permissions: .init(accessibility: true, screen_recording: true, microphone: "not_determined")))
    }

    func testIslandStatusReadsStoreWithoutMutatingIt() {
        let store = StatusStore()
        XCTAssertEqual(CUAServiceStatus.islandStatus(store: store).state, "locked")
        store.values["claim-id"] = Data("claim".utf8)
        XCTAssertEqual(CUAServiceStatus.islandStatus(store: store).state, "checking")
        XCTAssertEqual(store.writes, 0)
        XCTAssertEqual(store.deletes, 0)
    }

    func testRunningStatusUsesLockWithoutChangingIt() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let socket = directory.appendingPathComponent("service.sock").path
        let lockPath = socket + ".lock"
        XCTAssertFalse(CUAServiceStatus.serviceIsRunning(socketPath: socket))
        FileManager.default.createFile(atPath: lockPath, contents: Data())
        let fd = Darwin.open(lockPath, O_RDONLY)
        XCTAssertGreaterThanOrEqual(fd, 0)
        XCTAssertEqual(flock(fd, LOCK_EX | LOCK_NB), 0)
        XCTAssertTrue(CUAServiceStatus.serviceIsRunning(socketPath: socket))
        _ = flock(fd, LOCK_UN)
        Darwin.close(fd)
        XCTAssertFalse(CUAServiceStatus.serviceIsRunning(socketPath: socket))
    }
}
