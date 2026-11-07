import Darwin
import Foundation
import XCTest
@testable import CUAService

/// 0.2.86: Samantha's logs rotate in the writer, Swift and Python sharing one protocol. Temp dirs only.
final class CUALogRotationTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("cua-rot-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        XCTAssertFalse(dir.path.hasPrefix(FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache").path))
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: dir) }

    private func gunzip(_ url: URL) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/gunzip")
        p.arguments = ["-c", url.path]
        let pipe = Pipe()
        p.standardOutput = pipe
        try p.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        XCTAssertEqual(p.terminationStatus, 0, "valid gzip: \(url.lastPathComponent)")
        return String(decoding: data, as: UTF8.self)
    }

    private func allLines(_ live: URL) throws -> [String] {
        var out: [String] = []
        for i in stride(from: CUALogRotation.keep, through: 1, by: -1) {
            let gz = CUALogRotation.rotatedURL(live, i)
            if FileManager.default.fileExists(atPath: gz.path) { out += try gunzip(gz).split(separator: "\n").map(String.init) }
        }
        if let s = try? String(contentsOf: live, encoding: .utf8) { out += s.split(separator: "\n").map(String.init) }
        return out
    }

    func testNamesAndDefaults() {
        XCTAssertEqual(CUALogRotation.maxBytes, 50 * 1024 * 1024)
        XCTAssertEqual(CUALogRotation.keep, 5)
        XCTAssertEqual(CUALogRotation.rotatedURL(URL(fileURLWithPath: "/x/samantha-activity.jsonl"), 1).lastPathComponent,
                       "samantha-activity.1.jsonl.gz")
        XCTAssertEqual(CUALogRotation.rotatedURL(URL(fileURLWithPath: "/x/voice.log"), 5).lastPathComponent, "voice.5.log.gz")
        XCTAssertEqual(CUALogRotation.crc32(Data("123456789".utf8)), 0xCBF4_3926)
    }

    func testRollsAtLimitKeepsFiveGzippedAndLeavesSocketsAndLocksAlone() throws {
        let live = dir.appendingPathComponent("samantha-activity.jsonl")
        let sock = dir.appendingPathComponent("cua-service.sock")
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        _ = withUnsafeMutableBytes(of: &addr.sun_path) { strlcpy($0.baseAddress!.assumingMemoryBound(to: CChar.self), sock.path, $0.count) }
        let bound = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
        XCTAssertEqual(bound, 0)
        let others = ["cua-service.sock.lock", "cua-service.relaunch.lock", "visual-focus.lock", "island-events.jsonl"]
            .map { dir.appendingPathComponent($0) }
        for o in others { try Data("keep".utf8).write(to: o) }
        func sig(_ u: URL) -> [AnyHashable] {
            let a = try! FileManager.default.attributesOfItem(atPath: u.path)
            return [a[.systemFileNumber] as! AnyHashable, a[.modificationDate] as! AnyHashable]
        }
        let before = ([sock] + others).map(sig)
        for i in 0..<400 {
            CUALogRotation.append(Data("{\"i\":\(i),\"pad\":\"\(String(repeating: "p", count: 80))\"}\n".utf8), to: live, limit: 4096)
        }
        XCTAssertEqual(([sock] + others).map(sig), before, "sockets, locks and other files untouched")
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasPrefix("samantha-activity.") && $0 != live.lastPathComponent }
        XCTAssertEqual(names.sorted(), (1...5).map { "samantha-activity.\($0).jsonl.gz" } + [live.lastPathComponent + ".rotation.lock"])
        let nums = try allLines(live).map { line -> Int in
            let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
            return obj["i"] as! Int
        }
        XCTAssertEqual(nums, Array((400 - nums.count)..<400), "newest rows kept in order")
    }

    /// Swift threads and a Python voice_cua process append to one file at once, across rotations.
    func testSwiftAndPythonWritersShareTheFileAcrossRotations() throws {
        let live = dir.appendingPathComponent("samantha-activity.jsonl")
        let python = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../runtime/voice-cua/python").standardized
        let script = """
        import sys; sys.path.insert(0, \(String(reflecting: python.path)))
        from pathlib import Path
        from voice_cua import log_rotation
        for i in range(600):
            log_rotation.append(Path(sys.argv[1]), ('{"w":"py","i":%d,"pad":"%s"}\\n' % (i, "q" * 60)).encode(), limit=40960)
        """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "-c", script, live.path]
        try p.run()
        DispatchQueue.concurrentPerform(iterations: 3) { t in
            for i in 0..<200 {
                CUALogRotation.append(Data("{\"w\":\"sw\(t)\",\"i\":\(i),\"pad\":\"\(String(repeating: "s", count: 60))\"}\n".utf8),
                                      to: live, limit: 40960)
            }
        }
        p.waitUntilExit()
        XCTAssertEqual(p.terminationStatus, 0)
        var seen = Set<String>()
        for line in try allLines(live) {
            let obj = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any], "whole JSON line")
            XCTAssertTrue(seen.insert("\(obj["w"]!)-\(obj["i"]!)").inserted, "no duplicate")
        }
        XCTAssertEqual(seen.count, 600 + 3 * 200, "nothing lost")
        let gz = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".gz") }
        XCTAssertGreaterThanOrEqual(gz.count, 2, "it rotated under contention")
        XCTAssertTrue(try FileManager.default.contentsOfDirectory(atPath: dir.path).allSatisfy { !$0.hasSuffix(".tmp") })
    }
}
