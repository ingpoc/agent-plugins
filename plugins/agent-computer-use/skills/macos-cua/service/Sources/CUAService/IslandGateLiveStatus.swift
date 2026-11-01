import Darwin
import Foundation

struct IslandGateLiveStatus: Codable, Equatable {
    struct Frame: Codable, Equatable {
        let x: Double
        let y: Double
        let width: Double
        let height: Double
    }

    let state: String
    let panel_visible: Bool
    let panel_frame: Frame?

    private enum CodingKeys: String, CodingKey { case state, panel_visible, panel_frame }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(state, forKey: .state)
        try container.encode(panel_visible, forKey: .panel_visible)
        try container.encode(panel_frame, forKey: .panel_frame)
    }
}

enum IslandGateLiveStatusClient {
    private static let maximumReplyBytes = 70_000

    static func fetch(socketPath: String = IslandHubSocketServer.defaultPath() ?? "", timeout: TimeInterval = 0.25) -> IslandGateLiveStatus? {
        guard !socketPath.isEmpty else { return nil }
        var st = stat()
        guard lstat(socketPath, &st) == 0,
              (st.st_mode & S_IFMT) == S_IFSOCK,
              st.st_uid == getuid(), (st.st_mode & 0o077) == 0 else { return nil }

        let deadline = monotonicNow() + max(0.01, min(timeout, 1.0))
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var one: Int32 = 1
        guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size)) == 0 else { return nil }
        let oldFlags = fcntl(fd, F_GETFL)
        guard oldFlags >= 0, fcntl(fd, F_SETFL, oldFlags | O_NONBLOCK) == 0 else { return nil }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { return nil }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in bytes.enumerated() { raw[i] = b }
            raw[bytes.count] = 0
        }
        let connectResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        if connectResult != 0 {
            guard errno == EINPROGRESS, wait(fd, events: Int16(POLLOUT), deadline: deadline), socketError(fd) == 0 else { return nil }
        }

        let request: [String: Any] = ["method": "GET", "path": "/api/island/gate-status"]
        guard let encoded = try? JSONSerialization.data(withJSONObject: request) else { return nil }
        var line = encoded
        line.append(0x0A)
        var offset = 0
        while offset < line.count {
            guard wait(fd, events: Int16(POLLOUT), deadline: deadline) else { return nil }
            let count = line.withUnsafeBytes { raw in
                Darwin.write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
            }
            if count > 0 { offset += count; continue }
            if count < 0, errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK { continue }
            return nil
        }

        var response = Data()
        var chunk = [UInt8](repeating: 0, count: 16_384)
        while response.count < maximumReplyBytes {
            guard wait(fd, events: Int16(POLLIN), deadline: deadline) else { return nil }
            let count = chunk.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress!, $0.count) }
            if count > 0 {
                if let newline = chunk[..<count].firstIndex(of: 0x0A) {
                    response.append(contentsOf: chunk[..<newline])
                    break
                }
                response.append(contentsOf: chunk[..<count])
                continue
            }
            if count < 0, errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK { continue }
            return nil
        }
        guard response.count < maximumReplyBytes,
              let object = (try? JSONSerialization.jsonObject(with: response)) as? [String: Any],
              object["status"] as? Int == 200,
              let body = object["body"] as? [String: String],
              let state = body["state"], !state.isEmpty,
              let visibleText = body["panel_visible"], let visible = Bool(visibleText),
              let frameText = body["panel_frame"] else { return nil }
        let frame: IslandGateLiveStatus.Frame?
        if frameText == "null" {
            guard !visible else { return nil }
            frame = nil
        } else {
            let parts = frameText.split(separator: ",").compactMap { Double($0) }
            guard parts.count == 4, parts.allSatisfy(\.isFinite), parts[2] > 0, parts[3] > 0 else { return nil }
            frame = .init(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
        }
        return IslandGateLiveStatus(state: state, panel_visible: visible, panel_frame: frame)
    }

    private static func monotonicNow() -> TimeInterval {
        var time = timespec()
        clock_gettime(CLOCK_MONOTONIC, &time)
        return TimeInterval(time.tv_sec) + TimeInterval(time.tv_nsec) / 1_000_000_000
    }

    private static func wait(_ fd: Int32, events: Int16, deadline: TimeInterval) -> Bool {
        while true {
            let remaining = deadline - monotonicNow()
            guard remaining > 0 else { return false }
            var descriptor = pollfd(fd: fd, events: events, revents: 0)
            let milliseconds = Int32(max(1, min(1000, ceil(remaining * 1000))))
            let result = poll(&descriptor, 1, milliseconds)
            if result > 0 { return descriptor.revents & (events | Int16(POLLERR | POLLHUP | POLLNVAL)) != 0 }
            if result == 0 { return false }
            if errno != EINTR { return false }
        }
    }

    private static func socketError(_ fd: Int32) -> Int32 {
        var value: Int32 = 0
        var length = socklen_t(MemoryLayout<Int32>.size)
        guard getsockopt(fd, SOL_SOCKET, SO_ERROR, &value, &length) == 0 else { return errno }
        return value
    }
}
