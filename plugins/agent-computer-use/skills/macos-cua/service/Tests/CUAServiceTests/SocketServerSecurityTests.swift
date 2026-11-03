import Darwin
import Foundation
import Logging
import NIOCore
import XCTest
@testable import CUAService

final class SocketServerSecurityTests: XCTestCase {
    private func endpoint() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("rpc-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o755])
        return dir.appendingPathComponent("rpc.sock")
    }

    private func server(_ path: URL, allowedUID: uid_t = getuid(), readTimeout: TimeAmount = .seconds(10),
                        connectionLimit: Int = 64, handlerDelay: UInt64 = 0) -> SocketServer {
        SocketServer(socketPath: path.path, logger: Logger(label: "rpc-security-test"),
                     allowedUID: allowedUID, readTimeout: readTimeout, connectionLimit: connectionLimit) { request in
            if handlerDelay > 0 { try? await Task.sleep(nanoseconds: handlerDelay) }
            return .success(id: request.id, ["method": request.method])
        }
    }

    private func connect(_ path: URL) throws -> Int32 {
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.ENFILE) }
        var timeout = timeval(tv_sec: 1, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
        var noSignal: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout.size(ofValue: noSignal)))
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bytes = Array(path.path.utf8) + [0]
        guard bytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            Darwin.close(fd); throw XCTSkip("temporary socket path is too long")
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { $0.copyBytes(from: bytes) }
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard rc == 0 else { let error = errno; Darwin.close(fd); throw POSIXError(POSIXErrorCode(rawValue: error)!) }
        return fd
    }

    private func send(_ data: Data, to fd: Int32) {
        XCTAssertEqual(data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }, data.count)
    }

    private func frame(_ body: Data) -> Data {
        var size = UInt32(body.count).littleEndian
        var result = Data(bytes: &size, count: 4)
        result.append(body)
        return result
    }

    func testEndpointIsOwnerOnly() async throws {
        let path = try endpoint()
        let s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        for (url, expected) in [(path.deletingLastPathComponent(), 0o700), (path, 0o600)] {
            let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
            XCTAssertEqual((attrs[.posixPermissions] as? NSNumber)?.intValue, expected)
        }
    }

    func testOversizedHeaderClosesConnectionWithoutBody() async throws {
        let path = try endpoint(), s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        send(Data([0xff, 0xff, 0xff, 0xff]), to: fd)
        var byte: UInt8 = 0
        let n = Darwin.read(fd, &byte, 1)
        XCTAssertTrue(n == 0 || (n == -1 && errno == ECONNRESET), "oversized header must close, not wait for a body")
    }

    func testValidRequestStillReturnsAResponse() async throws {
        let path = try endpoint(), s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        send(frame(Data(#"{"jsonrpc":"2.0","method":"ping","id":1}"#.utf8)), to: fd)
        try assertResponse(fd)
    }

    private func readExactly(_ count: Int, from fd: Int32) throws -> Data {
        var result = Data()
        while result.count < count {
            var bytes = [UInt8](repeating: 0, count: count - result.count)
            let n = Darwin.read(fd, &bytes, bytes.count)
            guard n > 0 else { throw POSIXError(.ECONNRESET) }
            result.append(contentsOf: bytes.prefix(n))
        }
        return result
    }

    private func assertResponse(_ fd: Int32) throws {
        let header = try readExactly(4, from: fd)
        let count = header.withUnsafeBytes { Int(UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self))) }
        guard count < 1024 else { XCTFail("unexpected test response size"); return }
        let value = try JSONSerialization.jsonObject(with: readExactly(count, from: fd)) as? [String: Any]
        XCTAssertEqual((value?["result"] as? [String: String])?["method"], "ping")
    }

    private func assertClosed(_ fd: Int32, file: StaticString = #filePath, line: UInt = #line) {
        var byte: UInt8 = 0
        let n = Darwin.read(fd, &byte, 1)
        XCTAssertTrue(n == 0 || (n == -1 && errno == ECONNRESET), "connection must close", file: file, line: line)
    }

    func testKernelPeerCredentialsRejectDifferentAllowedUID() async throws {
        let path = try endpoint(), s = server(path, allowedUID: getuid() + 1)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        assertClosed(fd)
    }

    func testIncompleteNewConnectionTimesOut() async throws {
        let path = try endpoint(), s = server(path, readTimeout: .milliseconds(100))
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        send(Data([20, 0]), to: fd)
        assertClosed(fd)
    }

    func testEmptyNewConnectionTimesOut() async throws {
        let path = try endpoint(), s = server(path, readTimeout: .milliseconds(100))
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        assertClosed(fd)
    }

    func testHealthyPersistentConnectionSurvivesIdleGap() async throws {
        let path = try endpoint(), s = server(path, readTimeout: .milliseconds(100))
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        let ping = frame(Data(#"{"jsonrpc":"2.0","method":"ping","id":1}"#.utf8))
        send(ping, to: fd); try assertResponse(fd)
        try await Task.sleep(nanoseconds: 250_000_000)
        send(ping, to: fd); try assertResponse(fd)
    }

    func testMalformedJSONClosesConnection() async throws {
        let path = try endpoint(), s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        send(frame(Data("{".utf8)), to: fd)
        assertClosed(fd)
    }

    func testStartupPreservesNonSocketFile() throws {
        let path = try endpoint(), s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try Data("keep".utf8).write(to: path)
        XCTAssertThrowsError(try s.start())
        XCTAssertEqual(try String(contentsOf: path), "keep")
    }

    func testOversizedFrameIsRejectedBeforePayloadArrives() {
        var buffer = Data([0xff, 0xff, 0xff, 0xff])
        XCTAssertThrowsError(try FrameCodec.decode(from: &buffer))
    }
    func testConnectionLimitAndReleasedSlot() async throws {
        let path = try endpoint(), s = server(path, connectionLimit: 1)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let first = try connect(path)
        let ping = frame(Data(#"{"jsonrpc":"2.0","method":"ping","id":1}"#.utf8))
        send(ping, to: first); try assertResponse(first)
        let excess = try connect(path); defer { Darwin.close(excess) }
        assertClosed(excess)
        Darwin.close(first)
        try await Task.sleep(nanoseconds: 100_000_000)
        let replacement = try connect(path); defer { Darwin.close(replacement) }
        send(ping, to: replacement); try assertResponse(replacement)
    }

    func testZeroLengthFrameClosesConnection() async throws {
        let path = try endpoint(), s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        send(Data([0, 0, 0, 0]), to: fd)
        assertClosed(fd)
    }

    func testStartupPreservesSymlinkTarget() throws {
        let path = try endpoint(), s = server(path)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        let target = path.deletingLastPathComponent().appendingPathComponent("target")
        try Data("keep".utf8).write(to: target)
        try FileManager.default.createSymbolicLink(at: path, withDestinationURL: target)
        XCTAssertThrowsError(try s.start())
        XCTAssertEqual(try String(contentsOf: target), "keep")
    }

    func testCompleteSlowRequestDoesNotExpire() async throws {
        let path = try endpoint(), s = server(path, readTimeout: .milliseconds(100), handlerDelay: 250_000_000)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        send(frame(Data(#"{"jsonrpc":"2.0","method":"ping","id":1}"#.utf8)), to: fd)
        try assertResponse(fd)
    }

    func testExcessConcurrentRequestsCloseConnection() async throws {
        let path = try endpoint(), s = server(path, handlerDelay: 250_000_000)
        defer { s.stop(); try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        try s.start()
        let fd = try connect(path); defer { Darwin.close(fd) }
        let ping = frame(Data(#"{"jsonrpc":"2.0","method":"ping","id":1}"#.utf8))
        var batch = Data()
        for _ in 0..<9 { batch.append(ping) }
        send(batch, to: fd)
        assertClosed(fd)
    }

}
