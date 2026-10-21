import Darwin
import Foundation

/// Local-peer channel for `island-post` and Samantha's helper (no Keychain read in the client, so no
/// macOS Keychain prompt). Unix domain socket in `~/Library/Application Support/CUAService/` (dir 0700,
/// socket 0600). Every connection's peer uid is checked with getpeereid() and must equal ours; the hub
/// then authorizes the source by Settings (toggle/cap) plus "the user has opted that source in" (its
/// Keychain token exists — CUAService reads it itself, the item trusts this app, so no prompt).
///
/// Protocol: one JSON line per connection
///   → {"method":"POST","path":"/api/island/publish","query":{...},"body":{...}}
///   ← {"status":200,"body":{...}}
final class IslandHubSocketServer: @unchecked Sendable {
    private let handler: @Sendable (IslandHub.Request) async -> IslandHub.Response
    private let queue = DispatchQueue(label: "island-hub-socket")
    private var fd: Int32 = -1
    private let active = NSLock()
    private var activeCount = 0
    static let maxConnections = 96
    private var source: DispatchSourceRead?
    private(set) var path: String?

    init(handler: @escaping @Sendable (IslandHub.Request) async -> IslandHub.Response) {
        self.handler = handler
    }

    static func defaultPath() -> String? {
        guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
        return base.appendingPathComponent("CUAService/island.sock").path
    }

    /// Returns the bound path or nil.
    func start(path: String) -> String? {
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        chmod(dir, 0o700)
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { return nil }
        let s = socket(AF_UNIX, SOCK_STREAM, 0)
        guard s >= 0 else { return nil }
        // Only replace a stale socket file (never a regular file someone put there).
        var st = stat()
        if lstat(path, &st) == 0 {
            guard (st.st_mode & S_IFMT) == S_IFSOCK else { close(s); return nil }
            unlink(path)
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in bytes.enumerated() { raw[i] = b }
            raw[bytes.count] = 0
        }
        let old = umask(0o177)
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(s, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        umask(old)
        guard rc == 0 else { close(s); return nil }
        chmod(path, 0o600)
        guard listen(s, 16) == 0 else { close(s); unlink(path); return nil }
        _ = fcntl(s, F_SETFL, fcntl(s, F_GETFL) | O_NONBLOCK)
        fd = s
        self.path = path
        let src = DispatchSource.makeReadSource(fileDescriptor: s, queue: queue)
        src.setEventHandler { [weak self] in self?.acceptAll() }
        src.resume()
        source = src
        return path
    }

    func stop() {
        source?.cancel()
        source = nil
        if fd >= 0 { close(fd); fd = -1 }
        if let p = path { unlink(p) }
        path = nil
    }

    private func acceptAll() {
        while true {
            let c = accept(fd, nil, nil)
            if c < 0 { return }
            var uid: uid_t = 0, gid: gid_t = 0
            active.lock()
            let full = activeCount >= Self.maxConnections
            if !full { activeCount += 1 }
            active.unlock()
            if full { close(c); continue }   // connection cap (long-polls park)
            guard getpeereid(c, &uid, &gid) == 0, uid == getuid() else {
                release()
                close(c)
                SamanthaActivityLog.startup(phase: "island_socket", status: "fail", detail: "peer uid rejected")
                continue
            }
            // Accepted fds inherit O_NONBLOCK from the listener on Darwin: make it blocking so the
            // read loop can't see EAGAIN before the client's line arrives.
            _ = fcntl(c, F_SETFL, fcntl(c, F_GETFL) & ~O_NONBLOCK)
            var one: Int32 = 1
            setsockopt(c, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
            var tv = timeval(tv_sec: 5, tv_usec: 0)  // request read only; the reply may park (long-poll)
            setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
            let me = self
            DispatchQueue.global(qos: .userInitiated).async { [handler] in
                Self.serve(c, handler: handler) { me.release() }
            }
        }
    }

    private func release() {
        active.lock(); activeCount -= 1; active.unlock()
    }

    private static func serve(_ c: Int32, handler: @escaping @Sendable (IslandHub.Request) async -> IslandHub.Response,
                              done: @escaping @Sendable () -> Void) {
        var buf = Data()
        var chunk = [UInt8](repeating: 0, count: 8192)
        while buf.count <= 70_000, !buf.contains(0x0A) {
            let n = read(c, &chunk, chunk.count)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { break }
            buf.append(contentsOf: chunk[0..<n])
        }
        let line = buf.firstIndex(of: 0x0A).map { buf[..<$0] } ?? buf[...]
        guard let req = parse(Data(line)) else {
            write(c, .err(400, "bad request")); close(c); done(); return
        }
        Task {
            let r = await handler(req)
            write(c, r)
            close(c)
            done()
        }
    }

    static func parse(_ line: Data) -> IslandHub.Request? {
        guard line.count <= 70_000,
              let j = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              let method = j["method"] as? String, let path = j["path"] as? String else { return nil }
        var q: [String: String] = [:]
        for (k, v) in (j["query"] as? [String: Any]) ?? [:] { q[k] = "\(v)" }
        let body = j["body"].flatMap { try? JSONSerialization.data(withJSONObject: $0) } ?? Data()
        var r = IslandHub.Request(method: method.uppercased(), path: path, query: q, body: body)
        r.peerVerified = true
        return r
    }

    private static func write(_ c: Int32, _ r: IslandHub.Response) {
        let obj: [String: Any] = ["status": r.status, "body": r.json]
        var d = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
        d.append(0x0A)
        d.withUnsafeBytes { p in
            var off = 0
            while off < p.count {
                let n = Darwin.write(c, p.baseAddress! + off, p.count - off)
                if n <= 0 { break }
                off += n
            }
        }
    }
}
