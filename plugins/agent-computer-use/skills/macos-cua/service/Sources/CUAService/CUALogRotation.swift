import Darwin
import Foundation

/// Size-based rotation for Samantha's logs, done by the writer (0.2.86). Same protocol as the Python
/// writer (`voice_cua/log_rotation.py`), so CUAService and voice_cua can append to one file:
/// - every append opens the path `O_APPEND`, takes a shared `flock` on that open file and checks it is still
///   the file at the path (same inode), reopening if a rotation renamed it, so no row lands in a rotated copy;
/// - the persistent `<log>.rotation.lock` serializes rotations and compression across both writers;
///   an exclusive log `flock` protects the inode check, archive shift, and rename.
/// Only the exact log names are renamed or deleted, never a socket, lock or other file in the folder.
enum CUALogRotation {
    static let maxBytes: Int = 50 * 1024 * 1024
    static let keep = 5
    private static let threadLock = NSLock()

    /// `VOICE_CUA_LOG_MAX_BYTES` overrides the limit (tests only).
    static var limit: Int {
        if let raw = ProcessInfo.processInfo.environment["VOICE_CUA_LOG_MAX_BYTES"], let v = Int(raw), v > 0 { return v }
        return maxBytes
    }

    static func rotatedURL(_ url: URL, _ index: Int, gz: Bool = true) -> URL {
        let ext = url.pathExtension
        let stem = url.deletingPathExtension().lastPathComponent
        let name = ext.isEmpty ? "\(stem).\(index)" : "\(stem).\(index).\(ext)"
        return url.deletingLastPathComponent().appendingPathComponent(gz ? name + ".gz" : name)
    }

    /// One `O_APPEND` write under a shared lock, then rotation if the file is now full.
    static func append(_ data: Data, to url: URL, limit: Int = CUALogRotation.limit) {
        threadLock.lock()
        let size: Int
        do {
            defer { threadLock.unlock() }
            guard let fd = openLive(url, LOCK_SH) else { return }
            defer { flock(fd, LOCK_UN); Darwin.close(fd) }
            data.withUnsafeBytes { buf in
                if let base = buf.baseAddress { _ = Darwin.write(fd, base, buf.count) }
            }
            var st = stat()
            size = fstat(fd, &st) == 0 ? Int(st.st_size) : 0
        }
        if size >= limit { rotateIfNeeded(url, limit: limit) }
    }

    /// An `O_APPEND` descriptor on the live file for a child's stdout/stderr (rotating first if full).
    static func appendHandle(_ url: URL, limit: Int = CUALogRotation.limit) -> FileHandle? {
        rotateIfNeeded(url, limit: limit)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let fd = Darwin.open(url.path, O_WRONLY | O_CREAT | O_APPEND, S_IRUSR | S_IWUSR)
        return fd >= 0 ? FileHandle(fileDescriptor: fd, closeOnDealloc: true) : nil
    }

    @discardableResult
    static func rotateIfNeeded(_ url: URL, limit: Int = CUALogRotation.limit) -> Bool {
        var st = stat()
        guard lstat(url.path, &st) == 0, Int(st.st_size) >= limit else { return false }
        threadLock.lock()
        defer { threadLock.unlock() }
        let lockFD = Darwin.open(url.path + ".rotation.lock", O_WRONLY | O_CREAT, S_IRUSR | S_IWUSR)
        guard lockFD >= 0 else { return false }
        defer { flock(lockFD, LOCK_UN); Darwin.close(lockFD) }
        guard flock(lockFD, LOCK_EX) == 0 else { return false }
        let rotated: Bool
        do {
            guard let fd = openLive(url, LOCK_EX) else { return false }
            defer { flock(fd, LOCK_UN); Darwin.close(fd) }
            var cur = stat()
            guard fstat(fd, &cur) == 0, Int(cur.st_size) >= limit else { return false }  // rotated by another writer
            shift(url)
            rotated = Darwin.rename(url.path, rotatedURL(url, 1, gz: false).path) == 0
        }
        if rotated { gzipFile(rotatedURL(url, 1, gz: false), to: rotatedURL(url, 1)) }
        return rotated
    }

    private static func sameFile(_ fd: Int32, _ url: URL) -> Bool {
        var a = stat(), b = stat()
        guard fstat(fd, &a) == 0, lstat(url.path, &b) == 0 else { return false }
        return a.st_dev == b.st_dev && a.st_ino == b.st_ino
    }

    private static func openLive(_ url: URL, _ op: Int32) -> Int32? {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        for _ in 0..<50 {
            let fd = Darwin.open(url.path, O_WRONLY | O_CREAT | O_APPEND, S_IRUSR | S_IWUSR)
            guard fd >= 0 else { return nil }
            flock(fd, op)
            if sameFile(fd, url) { return fd }
            flock(fd, LOCK_UN)
            Darwin.close(fd)
        }
        return nil
    }

    private static func shift(_ url: URL) {
        let fm = FileManager.default
        for i in 1...keep where fm.fileExists(atPath: rotatedURL(url, i, gz: false).path) {
            gzipFile(rotatedURL(url, i, gz: false), to: rotatedURL(url, i))
        }
        unlink(rotatedURL(url, keep).path)
        for i in stride(from: keep - 1, through: 1, by: -1) where fm.fileExists(atPath: rotatedURL(url, i).path) {
            Darwin.rename(rotatedURL(url, i).path, rotatedURL(url, i + 1).path)
        }
    }

    /// gzip (RFC 1952) via Foundation's raw DEFLATE, then rename over `dst` and remove `src`.
    static func gzipFile(_ src: URL, to dst: URL) {
        guard let raw = try? Data(contentsOf: src),
              let deflated = try? (raw as NSData).compressed(using: .zlib) as Data else { return }
        var out = Data([0x1f, 0x8b, 0x08, 0x00, 0, 0, 0, 0, 0x00, 0x03])
        out.append(deflated)
        var crc = crc32(raw).littleEndian, size = UInt32(truncatingIfNeeded: raw.count).littleEndian
        withUnsafeBytes(of: &crc) { out.append(contentsOf: $0) }
        withUnsafeBytes(of: &size) { out.append(contentsOf: $0) }
        let tmp = dst.deletingLastPathComponent().appendingPathComponent(dst.lastPathComponent + ".tmp")
        guard (try? out.write(to: tmp)) != nil, Darwin.rename(tmp.path, dst.path) == 0 else { return }
        unlink(src.path)
    }

    private static let crcTable: [UInt32] = (0..<256).map { n -> UInt32 in
        var c = UInt32(n)
        for _ in 0..<8 { c = (c & 1) != 0 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
        return c
    }

    static func crc32(_ data: Data) -> UInt32 {
        var c: UInt32 = 0xFFFF_FFFF
        data.withUnsafeBytes { (buf: UnsafeRawBufferPointer) in
            for b in buf { c = crcTable[Int((c ^ UInt32(b)) & 0xFF)] ^ (c >> 8) }
        }
        return c ^ 0xFFFF_FFFF
    }
}
