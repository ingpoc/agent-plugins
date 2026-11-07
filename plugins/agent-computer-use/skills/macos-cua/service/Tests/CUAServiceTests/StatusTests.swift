import AVFoundation
import Darwin
import XCTest
@testable import CUAService

private final class GateStatusFixture {
    private(set) var path: String
    private var fd: Int32 = -1

    init(reply: Data?, delay: TimeInterval = 0) throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        path = dir.appendingPathComponent("gate.sock").path
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { throw NSError(domain: "fixture", code: 1) }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in bytes.enumerated() { raw[i] = b }
            raw[bytes.count] = 0
        }
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard rc == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        chmod(path, 0o600)
        guard listen(fd, 4) == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        let serverFD = fd
        Thread.detachNewThread {
            let client = accept(serverFD, nil, nil)
            guard client >= 0 else { return }
            defer { Darwin.close(client) }
            var noSignal: Int32 = 1
            _ = setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            var incoming = [UInt8](repeating: 0, count: 2048)
            _ = Darwin.read(client, &incoming, incoming.count)
            if delay > 0 { Thread.sleep(forTimeInterval: delay) }
            guard var reply else { return }
            if reply.last != 0x0A { reply.append(0x0A) }
            reply.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let count = Darwin.write(client, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                    if count <= 0 { return }
                    offset += count
                }
            }
        }
    }

    deinit {
        if fd >= 0 { Darwin.close(fd) }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
    }
}

final class StatusStore: IslandGateStoring {
    var values: [String: Data] = [:]
    private(set) var writes = 0
    private(set) var deletes = 0

    func read(_ name: String) throws -> Data? { values[name] }
    func write(_ data: Data, name: String) throws { writes += 1; values[name] = data }
    func delete(_ name: String) throws { deletes += 1; values.removeValue(forKey: name) }
}

final class StatusTests: XCTestCase {
    @MainActor
    func testVoiceControlRejectsInvalidOperationsAndReturnsSupervisorState() async throws {
        var seen: [String] = []
        let handler: @MainActor (String) async -> [String: Any] = { operation in
            seen.append(operation)
            return ["ok": true, "running": operation == "start", "gateway_port": 8766]
        }
        let result = try await MethodRouter.handleVoiceControl("start", handler: handler)
        XCTAssertEqual(result["running"] as? Bool, true)
        XCTAssertEqual(result["gateway_port"] as? Int, 8766)
        do {
            _ = try await MethodRouter.handleVoiceControl("restart", handler: handler)
            XCTFail("invalid operation must fail")
        } catch {
            XCTAssertEqual(seen, ["start"])
        }
    }

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

    func testLiveStatusSidecarDoesNotReplaceStoredEntitlement() throws {
        let live = IslandGateLiveStatus(state: "error", panel_visible: false,
            panel_frame: .init(x: 1, y: 2, width: 640, height: 320))
        let status = CUAServiceStatus.islandStatus(store: StatusStore(), live: live)
        XCTAssertEqual(status.state, "locked")
        XCTAssertEqual(status.live, live)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(status)) as? [String: Any])
        XCTAssertEqual(object["state"] as? String, "locked")
        XCTAssertEqual((object["live"] as? [String: Any])?["state"] as? String, "error")
    }

    func testLiveStatusClientFallsBackForMissingSocket() {
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: "/no/such/cua-service.sock", timeout: 0.01))
    }

    func testLiveStatusClientDecodesMetadataFromFakeUnixServer() throws {
        let fixture = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"unlocked","panel_visible":"true","panel_frame":"3,4,640,320"}}"#.utf8))
        let result = IslandGateLiveStatusClient.fetch(socketPath: fixture.path, timeout: 0.5)
        XCTAssertEqual(result, .init(state: "unlocked", panel_visible: true,
                                     panel_frame: .init(x: 3, y: 4, width: 640, height: 320)))
    }

    func testLiveStatusClientAcceptsMissingPanelFrameWhenHidden() throws {
        let fixture = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"locked","panel_visible":"false","panel_frame":"null"}}"#.utf8))
        let result = IslandGateLiveStatusClient.fetch(socketPath: fixture.path, timeout: 0.5)
        XCTAssertEqual(result, .init(state: "locked", panel_visible: false, panel_frame: nil))
        let encoded = try JSONEncoder().encode(try XCTUnwrap(result))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertTrue(json["panel_frame"] is NSNull)
    }

    func testLiveStatusClientRejectsMissingFrameWhenVisible() throws {
        let fixture = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"unlocked","panel_visible":"true","panel_frame":"null"}}"#.utf8))
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: fixture.path, timeout: 0.5))
    }

    func testLiveStatusClientFallsBackForOldServerAndMalformedReplies() throws {
        let oldServer = try GateStatusFixture(reply: Data(#"{"status":404,"body":{"error":"missing"}}"#.utf8))
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: oldServer.path, timeout: 0.5))
        let malformed = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"unlocked"}}"#.utf8))
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: malformed.path, timeout: 0.5))
        let invalidFrame = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"unlocked","panel_visible":"false","panel_frame":"0,0,NaN,0"}}"#.utf8))
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: invalidFrame.path, timeout: 0.5))
    }

    func testLiveStatusClientUsesOneDeadlineForSlowReply() throws {
        let fixture = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"unlocked","panel_visible":"true","panel_frame":"3,4,640,320"}}"#.utf8), delay: 0.35)
        let started = ProcessInfo.processInfo.systemUptime
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: fixture.path, timeout: 0.08))
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - started, 0.25)
    }

    func testLiveStatusClientRejectsUntrustedSocketPermissions() throws {
        let fixture = try GateStatusFixture(reply: Data(#"{"status":200,"body":{"state":"unlocked","panel_visible":"true","panel_frame":"3,4,640,320"}}"#.utf8))
        chmod(fixture.path, 0o666)
        XCTAssertNil(IslandGateLiveStatusClient.fetch(socketPath: fixture.path, timeout: 0.5))
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
