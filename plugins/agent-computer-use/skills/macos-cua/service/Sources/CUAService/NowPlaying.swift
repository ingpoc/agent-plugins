import AppKit
import CoreAudio
import Foundation

// MARK: - Now Playing (parity A1)
//
// System-wide now playing via MediaRemote. On macOS 15.4+ MediaRemote is entitlement-gated for
// third-party apps, so we use mediaremote-adapter (BSD-3, vendored): Apple's /usr/bin/perl is
// entitled and loads our MediaRemoteAdapter.framework, which streams JSON lines. If the adapter's
// self-test fails we fall back to polling Music/Spotify with AppleScript (only while that app is
// already running — never launches it). We never start playback or touch the output device/volume
// on our own; commands are sent only from the user's clicks on the card.

struct NowPlayingInfo: Equatable {
    var bundleID: String = ""
    var title: String = ""
    var artist: String = ""
    var album: String = ""
    var duration: Double = 0
    var elapsed: Double = 0
    /// When `elapsed` was sampled.
    var timestamp: Date = Date()
    var playing = false
    var rate: Double = 1
    var shuffle: Int = 0          // MediaRemote: 1 off, 2/3 on
    var repeatMode: Int = 0       // 1 off, 2 one, 3 all
    var isLiked = false
    var artworkHash: Int = 0
    var artwork: NSImage? = nil
    /// Artwork average colours (left/right halves) for Colored / Gradient progress; not part of equality.
    var tints: [NSColor] = []
    /// Compact waveform colour: dominant artwork colour lifted to ≥ 60 % luminance (nil = white).
    var waveTint: NSColor?
    /// Like/favorite works only for Music (AppleScript on a user click).
    var likeAvailable: Bool { bundleID == NowPlayingRules.musicBundle }

    static func == (a: NowPlayingInfo, b: NowPlayingInfo) -> Bool {
        a.bundleID == b.bundleID && a.title == b.title && a.artist == b.artist && a.album == b.album
            && a.duration == b.duration && a.elapsed == b.elapsed && a.timestamp == b.timestamp
            && a.playing == b.playing && a.rate == b.rate && a.shuffle == b.shuffle
            && a.repeatMode == b.repeatMode && a.isLiked == b.isLiked && a.artworkHash == b.artworkHash
    }

    /// Elapsed now, extrapolated while playing.
    func elapsed(at now: Date) -> Double {
        guard playing else { return min(elapsed, duration > 0 ? duration : elapsed) }
        let e = elapsed + now.timeIntervalSince(timestamp) * (rate > 0 ? rate : 1)
        return duration > 0 ? min(max(0, e), duration) : max(0, e)
    }

    var trackKey: String { "\(bundleID)|\(title)|\(artist)|\(album)" }
    var shuffleOn: Bool { shuffle > 1 }
    var repeatOn: Bool { repeatMode > 1 }
}

enum NowPlayingCommand: Equatable {
    case togglePlayPause, next, previous, toggleShuffle, toggleRepeat, seek(Double)

    /// mediaremote-adapter `send` IDs (kMRTogglePlayPause = 2, next 4, previous 5, shuffle 6, repeat 7).
    var adapterArgs: [String] {
        switch self {
        case .togglePlayPause: return ["send", "2"]
        case .next: return ["send", "4"]
        case .previous: return ["send", "5"]
        case .toggleShuffle: return ["send", "6"]
        case .toggleRepeat: return ["send", "7"]
        case .seek(let s): return ["seek", String(Int64(max(0, s) * 1_000_000))]
        }
    }
}

@MainActor
protocol NowPlayingSource: AnyObject {
    var onUpdate: ((NowPlayingInfo?) -> Void)? { get set }
    var name: String { get }
    func start()
    func stop()
    func send(_ command: NowPlayingCommand)
}

// MARK: - Stream parsing (pure)

enum NowPlayingParser {
    /// Merge one adapter `stream` line into the current payload dictionary.
    /// Returns nil when the line isn't a data message.
    static func merge(line: Data, into payload: inout [String: Any]) -> Bool {
        guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              (obj["type"] as? String) == "data",
              let p = obj["payload"] as? [String: Any] else { return false }
        if (obj["diff"] as? Bool) == true {
            for (k, v) in p {
                if v is NSNull { payload[k] = nil } else { payload[k] = v }
            }
        } else {
            payload = p
        }
        return true
    }

    /// Empty payload, or neither a title nor an app: nothing is playing.
    static func isCleared(_ p: [String: Any]) -> Bool {
        p.isEmpty || ((p["title"] as? String ?? "").isEmpty && p["bundleIdentifier"] == nil && p["parentApplicationBundleIdentifier"] == nil)
    }

    static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    static let isoPlain = ISO8601DateFormatter()

    static func info(from p: [String: Any], artworkCache: inout (hash: Int, image: NSImage?)) -> NowPlayingInfo? {
        guard let title = p["title"] as? String, !title.isEmpty,
              let bundle = (p["parentApplicationBundleIdentifier"] as? String) ?? (p["bundleIdentifier"] as? String)
        else { return nil }
        var i = NowPlayingInfo()
        i.bundleID = bundle
        i.title = title
        i.artist = p["artist"] as? String ?? ""
        i.album = p["album"] as? String ?? ""
        i.duration = (p["duration"] as? NSNumber)?.doubleValue ?? 0
        i.elapsed = (p["elapsedTime"] as? NSNumber)?.doubleValue ?? 0
        if let ts = p["timestamp"] as? String {
            i.timestamp = isoFormatter.date(from: ts) ?? isoPlain.date(from: ts) ?? Date()
        }
        i.playing = (p["playing"] as? Bool) ?? false
        i.rate = (p["playbackRate"] as? NSNumber)?.doubleValue ?? (i.playing ? 1 : 0)
        i.shuffle = (p["shuffleMode"] as? NSNumber)?.intValue ?? 0
        i.repeatMode = (p["repeatMode"] as? NSNumber)?.intValue ?? 0
        i.isLiked = (p["isLiked"] as? Bool) ?? false
        if let b64 = p["artworkData"] as? String {
            let h = b64.hashValue
            if h != artworkCache.hash {
                artworkCache = (h, Data(base64Encoded: b64).flatMap(NSImage.init(data:)))
            }
            i.artworkHash = h
            i.artwork = artworkCache.image
        }
        return i
    }
}

// MARK: - mediaremote-adapter source

/// Stream parsing state, confined to one serial queue (JSON merge + artwork decode stay off main).
private final class AdapterStreamState: @unchecked Sendable {
    var buffer = Data()
    var payload: [String: Any] = [:]
    var artworkCache: (hash: Int, image: NSImage?) = (0, nil)

    /// Returns one result per complete line: .some(info) = now playing, .some(nil) = nothing playing.
    func consume(_ d: Data) -> [NowPlayingInfo?] {
        buffer.append(d)
        var out: [NowPlayingInfo?] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<nl)
            buffer.removeSubrange(buffer.startIndex...nl)
            guard NowPlayingParser.merge(line: line, into: &payload) else { continue }
            if let info = NowPlayingParser.info(from: payload, artworkCache: &artworkCache) {
                out.append(info)
            } else if NowPlayingParser.isCleared(payload) {
                out.append(nil)   // explicit "nothing playing" (empty payload / no title and no app)
            }                     // else: partial payload — ignore, never treat as a stop
        }
        if buffer.count > 8_000_000 { buffer.removeAll() }  // runaway guard
        return out
    }
}

@MainActor
final class MediaRemoteAdapterSource: NowPlayingSource {
    let name = "mediaremote-adapter"
    var onUpdate: ((NowPlayingInfo?) -> Void)?
    /// Stream keeps dying without data: the controller switches to the AppleScript fallback.
    var onGiveUp: (() -> Void)?
    struct Paths {
        var script: URL
        var framework: URL
        var testClient: URL?
    }
    let paths: Paths
    private var process: Process?
    private var pipe: Pipe?
    private let parseQueue = DispatchQueue(label: "now-playing.adapter.parse")
    private let sendQueue = DispatchQueue(label: "now-playing.adapter.send")  // one command at a time
    private var state = AdapterStreamState()
    private var failures = 0
    private var stopped = false
    static let maxFailures = 5

    init(paths: Paths) { self.paths = paths }

    /// Bundled layout: Resources/mediaremote-adapter.pl, Frameworks/MediaRemoteAdapter.framework,
    /// Helpers/MediaRemoteAdapterTestClient.
    static func bundled(_ bundle: Bundle = .main) -> Paths? {
        let c = bundle.bundleURL.appendingPathComponent("Contents")
        let script = c.appendingPathComponent("Resources/mediaremote-adapter.pl")
        let fw = c.appendingPathComponent("Frameworks/MediaRemoteAdapter.framework")
        guard FileManager.default.fileExists(atPath: script.path), FileManager.default.fileExists(atPath: fw.path) else { return nil }
        let test = c.appendingPathComponent("Helpers/MediaRemoteAdapterTestClient")
        return Paths(script: script, framework: fw, testClient: FileManager.default.isExecutableFile(atPath: test.path) ? test : nil)
    }

    /// Adapter self-test (exit 0 = MediaRemote reachable through perl). Off the main thread.
    nonisolated static func selfTest(_ p: Paths) async -> Bool {
        guard let test = p.testClient else { return false }   // broken bundle → AppleScript fallback
        return await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .utility).async {
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/perl")
                proc.arguments = [p.script.path, p.framework.path, test.path, "test"]
                proc.standardOutput = FileHandle.nullDevice
                proc.standardError = FileHandle.nullDevice
                do { try proc.run() } catch { cont.resume(returning: false); return }
                let deadline = Date().addingTimeInterval(8)
                while proc.isRunning && Date() < deadline { usleep(50_000) }
                if proc.isRunning { proc.terminate(); proc.waitUntilExit(); cont.resume(returning: false); return }
                cont.resume(returning: proc.terminationStatus == 0)
            }
        }
    }

    func start() {
        stopped = false
        launch()
    }

    private func launch() {
        guard !stopped, process == nil else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/perl")
        proc.arguments = [paths.script.path, paths.framework.path, "stream", "--debounce=80"]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        let st = AdapterStreamState()
        state = st
        let q = parseQueue
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { h.readabilityHandler = nil; return }   // EOF
            q.async {
                let results = st.consume(d)
                guard !results.isEmpty else { return }
                Task { @MainActor [weak self] in self?.deliver(results, from: st) }
            }
        }
        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in self?.terminated(proc) }
        }
        do {
            try proc.run()
            process = proc
            pipe = out
        } catch {
            out.fileHandleForReading.readabilityHandler = nil
            SamanthaActivityLog.startup(phase: "now_playing", status: "fail", detail: "adapter launch: \(error.localizedDescription)")
            terminated(nil)
        }
    }

    private func deliver(_ results: [NowPlayingInfo?], from st: AdapterStreamState) {
        guard !stopped, st === state else { return }   // stale stream after stop/restart
        failures = 0
        for r in results { onUpdate?(r) }
    }

    private func terminated(_ proc: Process?) {
        if let proc, proc !== process { return }
        teardownPipe()
        process = nil
        guard !stopped else { return }
        failures += 1
        if failures >= Self.maxFailures {
            SamanthaActivityLog.startup(phase: "now_playing", status: "fail", detail: "adapter stream keeps exiting; falling back")
            onGiveUp?()
            return
        }
        let delay = min(30, Double(failures) * 2)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            self?.launch()
        }
    }

    private func teardownPipe() {
        pipe?.fileHandleForReading.readabilityHandler = nil
        try? pipe?.fileHandleForReading.close()
        pipe = nil
    }

    func stop() {
        stopped = true
        let p = process
        process = nil
        p?.terminationHandler = nil
        teardownPipe()
        if let p, p.isRunning { p.terminate() }
        state = AdapterStreamState()
    }

    /// User clicks only. Serialized; each perl child is reaped before the next.
    func send(_ command: NowPlayingCommand) {
        let args = [paths.script.path, paths.framework.path] + command.adapterArgs
        sendQueue.async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/perl")
            proc.arguments = args
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            guard (try? proc.run()) != nil else { return }
            proc.waitUntilExit()
        }
    }
}

// MARK: - AppleScript fallback (Music / Spotify, only while running)

@MainActor
final class AppleScriptNowPlayingSource: NowPlayingSource {
    let name = "applescript"
    var onUpdate: ((NowPlayingInfo?) -> Void)?
    private var timer: Timer?
    private var polling = false
    private let queue = DispatchQueue(label: "now-playing.applescript")
    nonisolated private static let apps = [("com.apple.Music", "Music"), ("com.spotify.client", "Spotify")]

    func start() {
        guard timer == nil else { return }
        let t = Timer(timeInterval: 1.5, repeats: true) { [weak self] _ in Task { @MainActor in self?.poll() } }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        poll()
    }

    func stop() { timer?.invalidate(); timer = nil }

    nonisolated private static func running(_ bundle: String) -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundle).isEmpty
    }

    /// Scripts run off the main thread; only apps that are already running are asked (never launched).
    private func poll() {
        guard !polling else { return }
        polling = true
        queue.async { [weak self] in
            let info = Self.read()
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.polling = false
                if self.timer != nil { self.onUpdate?(info) }
            }
        }
    }

    nonisolated private static func read() -> NowPlayingInfo? {
        for (bundle, app) in apps where running(bundle) && AutomationGate.allowed(bundle) {
            let script = """
            tell application "\(app)"
              if player state is stopped then return ""
              set t to current track
              return (name of t) & "\\n" & (artist of t) & "\\n" & (album of t) & "\\n" & (duration of t) & "\\n" & (player position) & "\\n" & (player state as string)
            end tell
            """
            var err: NSDictionary?
            guard let out = NSAppleScript(source: script)?.executeAndReturnError(&err).stringValue, !out.isEmpty else { continue }
            let f = out.components(separatedBy: "\n")
            guard f.count >= 6 else { continue }
            var i = NowPlayingInfo()
            i.bundleID = bundle
            i.title = f[0]; i.artist = f[1]; i.album = f[2]
            let dur = Double(f[3].replacingOccurrences(of: ",", with: ".")) ?? 0
            i.duration = bundle == "com.spotify.client" ? dur / 1000 : dur
            i.elapsed = Double(f[4].replacingOccurrences(of: ",", with: ".")) ?? 0
            i.playing = f[5].contains("playing")
            return i
        }
        return nil
    }

    func send(_ command: NowPlayingCommand) {
        let verb: String
        switch command {
        case .togglePlayPause: verb = "playpause"
        case .next: verb = "next track"
        case .previous: verb = "previous track"
        case .seek(let s): verb = "set player position to \(s)"
        case .toggleShuffle, .toggleRepeat: return   // not scriptable uniformly; the adapter path handles these
        }
        queue.async {
            guard let (_, app) = Self.apps.first(where: { Self.running($0.0) && AutomationGate.allowed($0.0) }) else { return }
            var err: NSDictionary?
            NSAppleScript(source: "tell application \"\(app)\" to \(verb)")?.executeAndReturnError(&err)
        }
    }
}

// MARK: - Rules (pure, unit-tested)

enum NowPlayingRules {
    static let musicBundle = "com.apple.Music"
    static let spotifyBundle = "com.spotify.client"

    /// Media source setting: automatic / system = any player; music / spotify = that app only.
    static func accepts(_ info: NowPlayingInfo, source: String) -> Bool {
        switch source {
        case "music": return info.bundleID == musicBundle
        case "spotify": return info.bundleID == spotifyBundle
        default: return true
        }
    }

    /// Show while playing; after a pause keep it `idleDuration` s, then collapse.
    static func visible(_ info: NowPlayingInfo?, pausedAt: Date?, idleDuration: Double, frontmost: String?,
                        hideWhileSourceActive: Bool, now: Date) -> Bool {
        guard let info else { return false }
        if hideWhileSourceActive, let f = frontmost, f == info.bundleID { return false }
        if info.playing { return true }
        guard let p = pausedAt else { return false }
        return now.timeIntervalSince(p) < idleDuration
    }

    private static let extraWords = ["remaster", "remastered", "live", "version", "edit", "mono", "stereo", "deluxe",
                                     "explicit", "clean", "bonus", "anniversary", "mix", "remix", "feat", "ft.",
                                     "with ", "radio", "single", "extended", "acoustic", "demo", "mixed"]

    /// "Hide media title extras": drop "(Remastered 2011)", "[Explicit]", "- 2009 Remaster", "(feat. X)".
    static func stripExtras(_ title: String) -> String {
        var t = title
        let bracket = try! NSRegularExpression(pattern: #"\s*[\(\[]([^\)\]]*)[\)\]]"#)
        for m in bracket.matches(in: t, range: NSRange(t.startIndex..., in: t)).reversed() {
            guard let r = Range(m.range, in: t), let inner = Range(m.range(at: 1), in: t) else { continue }
            let s = t[inner].lowercased()
            if extraWords.contains(where: { s.contains($0) }) { t.removeSubrange(r) }
        }
        if let dash = t.range(of: " - ", options: .backwards) {
            let tail = t[dash.upperBound...].lowercased()
            if extraWords.contains(where: { tail.contains($0) }) { t.removeSubrange(dash.lowerBound...) }
        }
        let out = t.trimmingCharacters(in: .whitespaces)
        return out.isEmpty ? title : out
    }

    static func clock(_ s: Double) -> String {
        let v = max(0, Int(s.rounded(.down)))
        return v >= 3600 ? String(format: "%d:%02d:%02d", v / 3600, (v / 60) % 60, v % 60)
                         : String(format: "%d:%02d", v / 60, v % 60)
    }
}

// MARK: - Artwork colour (Progress colour: Colored / Gradient)

enum ArtworkColor {
    /// Dominant colour: 16×16 sample, 12 hue buckets weighted by saturation × brightness (near-greys
    /// and near-black ignored); the heaviest bucket's mean. Falls back to the plain average.
    static func dominant(_ image: NSImage?) -> NSColor? {
        guard let image, let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let w = 16, h = 16
        var px = [UInt8](repeating: 0, count: w * h * 4)
        guard let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return nil }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
        var weight = [Double](repeating: 0, count: 12)
        var sums = [(Double, Double, Double)](repeating: (0, 0, 0), count: 12)
        for i in stride(from: 0, to: px.count, by: 4) {
            let r = Double(px[i]) / 255, g = Double(px[i + 1]) / 255, b = Double(px[i + 2]) / 255
            let mx = max(r, g, b), mn = min(r, g, b)
            let sat = mx > 0 ? (mx - mn) / mx : 0
            guard mx > 0.15, sat > 0.15 else { continue }
            var hue: CGFloat = 0, s: CGFloat = 0, v: CGFloat = 0, a: CGFloat = 0
            NSColor(red: r, green: g, blue: b, alpha: 1).getHue(&hue, saturation: &s, brightness: &v, alpha: &a)
            let k = min(11, Int(Double(hue) * 12))
            let wt = sat * mx
            weight[k] += wt
            sums[k].0 += r * wt; sums[k].1 += g * wt; sums[k].2 += b * wt
        }
        guard let best = weight.indices.max(by: { weight[$0] < weight[$1] }), weight[best] > 0 else { return average(image) }
        let t = weight[best]
        return NSColor(red: sums[best].0 / t, green: sums[best].1 / t, blue: sums[best].2 / t, alpha: 1)
    }

    /// Relative luminance (sRGB, linearised).
    static func luminance(_ c: NSColor) -> Double {
        guard let s = c.usingColorSpace(.sRGB) else { return 1 }
        func lin(_ v: CGFloat) -> Double { let x = Double(v); return x <= 0.04045 ? x / 12.92 : pow((x + 0.055) / 1.055, 2.4) }
        return 0.2126 * lin(s.redComponent) + 0.7152 * lin(s.greenComponent) + 0.0722 * lin(s.blueComponent)
    }

    /// Mix toward white until luminance ≥ `floor` (legible on black). Hue is kept.
    static func luminanceFloor(_ c: NSColor, _ floor: Double) -> NSColor {
        guard let s = c.usingColorSpace(.sRGB) else { return c }
        if luminance(s) >= floor { return s }
        var lo = 0.0, hi = 1.0
        for _ in 0..<16 {
            let t = (lo + hi) / 2
            let m = s.blended(withFraction: CGFloat(t), of: .white) ?? .white
            if luminance(m) >= floor { hi = t } else { lo = t }
        }
        return s.blended(withFraction: CGFloat(hi), of: .white) ?? .white
    }

    /// Average colour of a region (fraction 0…1 of width), brightened for legibility on black.
    static func average(_ image: NSImage?, from x0: Double = 0, to x1: Double = 1) -> NSColor? {
        guard let image, let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let w = 8, h = 8
        var px = [UInt8](repeating: 0, count: w * h * 4)
        guard let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return nil }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
        var r = 0.0, g = 0.0, b = 0.0, n = 0.0
        for y in 0..<h {
            for x in Int(x0 * Double(w))..<max(Int(x0 * Double(w)) + 1, Int(x1 * Double(w))) {
                let i = (y * w + x) * 4
                r += Double(px[i]); g += Double(px[i + 1]); b += Double(px[i + 2]); n += 1
            }
        }
        guard n > 0 else { return nil }
        let c = NSColor(red: r / n / 255, green: g / n / 255, blue: b / n / 255, alpha: 1)
        var hue: CGFloat = 0, sat: CGFloat = 0, bri: CGFloat = 0, a: CGFloat = 0
        c.usingColorSpace(.deviceRGB)?.getHue(&hue, saturation: &sat, brightness: &bri, alpha: &a)
        return NSColor(hue: hue, saturation: min(1, sat * 1.2), brightness: max(0.75, bri), alpha: 1)
    }
}

// MARK: - Output device glyph (read-only; we never change the device or volume)

enum OutputDeviceGlyph {
    static func current() -> (symbol: String, name: String) {
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id) == noErr else {
            return ("hifispeaker.fill", "Speakers")
        }
        var nameRef: Unmanaged<CFString>?
        size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        addr.mSelector = kAudioObjectPropertyName
        let st = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &nameRef)
        let n = st == noErr ? (nameRef?.takeRetainedValue() as String? ?? "") : ""
        var transport: UInt32 = 0
        size = UInt32(MemoryLayout<UInt32>.size)
        addr.mSelector = kAudioDevicePropertyTransportType
        _ = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &transport)
        return (symbol(name: n, transport: transport), n)
    }

    static func symbol(name: String, transport: UInt32) -> String {
        let l = name.lowercased()
        if l.contains("airpods max") { return "airpodsmax" }
        if l.contains("airpods pro") { return "airpodspro" }
        if l.contains("airpods") { return "airpods" }
        if l.contains("beats") || l.contains("headphone") || l.contains("buds") { return "headphones" }
        if l.contains("homepod") { return "homepod.fill" }
        if transport == kAudioDeviceTransportTypeAirPlay { return "airplayaudio" }
        if transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE { return "headphones" }
        if transport == kAudioDeviceTransportTypeHDMI || transport == kAudioDeviceTransportTypeDisplayPort { return "tv" }
        return "hifispeaker.fill"
    }
}

// MARK: - Controller

/// Owns the source, applies the Now Playing settings, and publishes what the island should show.
@MainActor
final class NowPlayingController: ObservableObject {
    typealias K = NowPlayingKeys
    @Published private(set) var info: NowPlayingInfo?
    @Published private(set) var visible = false
    @Published private(set) var device: (symbol: String, name: String) = ("hifispeaker.fill", "Speakers")
    private(set) var pausedAt: Date?
    private(set) var sourceName = "none"
    let store: IslandSettingsStore
    private var source: NowPlayingSource?
    private var raw: NowPlayingInfo?
    private var tintCache: (hash: Int, tints: [NSColor], wave: NSColor?) = (0, [], nil)
    var onChange: (() -> Void)?
    var frontmost: () -> String? = { NSWorkspace.shared.frontmostApplication?.bundleIdentifier }

    init(store: IslandSettingsStore) {
        self.store = store
    }

    /// Production start: adapter if its self-test passes, else AppleScript polling.
    func startAutomatic() {
        guard store.bool(K.enabled) else { return }
        guard let paths = MediaRemoteAdapterSource.bundled() else {
            use(AppleScriptNowPlayingSource(), reason: "adapter not bundled")
            return
        }
        Task { @MainActor in
            let ok = await MediaRemoteAdapterSource.selfTest(paths)
            guard self.source == nil, self.store.bool(K.enabled) else { return }
            if ok {
                let src = MediaRemoteAdapterSource(paths: paths)
                src.onGiveUp = { [weak self] in self?.use(AppleScriptNowPlayingSource(), reason: "adapter stream gave up") }
                self.use(src, reason: "self-test ok")
            }
            else { self.use(AppleScriptNowPlayingSource(), reason: "adapter self-test failed") }
        }
    }

    /// Tests inject a fake.
    func use(_ s: NowPlayingSource, reason: String = "") {
        source?.stop()
        source = s
        sourceName = s.name
        s.onUpdate = { [weak self] i in self?.ingest(i) }
        s.start()
        refreshDevice()
        SamanthaActivityLog.startup(phase: "now_playing", status: "ok", detail: "\(s.name) \(reason)")
    }

    func stop() {
        source?.stop()
        source = nil
        raw = nil
        recompute()
    }

    func ingest(_ i: NowPlayingInfo?, now: Date = Date()) {
        let wasPlaying = raw?.playing ?? false
        if let i, i.playing { pausedAt = nil }
        else if wasPlaying { pausedAt = now }
        else if pausedAt != nil, let i, i.trackKey != raw?.trackKey { pausedAt = now }  // skipped while paused: restart the idle window
        raw = i
        recompute(now: now)
    }

    /// Settings / 1 s tick / app activation.
    func recompute(now: Date = Date()) {
        var shown: NowPlayingInfo? = nil
        if store.bool(K.enabled), let r = raw, NowPlayingRules.accepts(r, source: store.bool(K.automaticSource) ? "automatic" : store.string(K.app)) {
            shown = r
            if store.bool(K.hideTitleExtras) { shown?.title = NowPlayingRules.stripExtras(r.title) }
            if r.artworkHash != tintCache.hash {
                let img = r.artwork
                tintCache = (r.artworkHash, [ArtworkColor.average(img, from: 0, to: 0.5), ArtworkColor.average(img, from: 0.5, to: 1)].compactMap { $0 },
                             ArtworkColor.dominant(img).map { ArtworkColor.luminanceFloor($0, 0.6) })
            }
            shown?.tints = tintCache.tints
            shown?.waveTint = tintCache.wave
        }
        let vis = NowPlayingRules.visible(shown, pausedAt: pausedAt, idleDuration: store.double(K.idleDuration),
                                          frontmost: frontmost(), hideWhileSourceActive: store.bool(K.hideWhileSourceActive), now: now)
        var changed = false
        if shown != info { info = shown; changed = true }
        if vis != visible { visible = vis; changed = true; if vis { refreshDevice() } }
        else if vis, now.timeIntervalSince(lastDeviceRefresh) > 5 {   // read-only: follow output changes while shown
            let before = device
            refreshDevice()
            if before != device { changed = true }
        }
        if changed { onChange?() }
    }

    func applySettings() {
        if !store.bool(K.enabled) { if source != nil { stop() } }
        else if source == nil { startAutomatic() }
        recompute()
    }

    private var lastDeviceRefresh = Date.distantPast
    func refreshDevice() {
        lastDeviceRefresh = Date()
        device = OutputDeviceGlyph.current()
    }

    /// User clicks on the card only.
    func perform(_ c: NowPlayingCommand) {
        source?.send(c)
    }
}

enum NowPlayingKeys {
    /// Flipped on when the Now Playing island activity is wired end to end (phase A1).
    static let live = true
    static let p = "island.nowPlaying."
    static let enabled = p + "enabled"
    static let idleDuration = p + "idleDuration"
    static let color = p + "color"
    static let disableCompactWaveform = p + "disableCompactWaveform"
    static let disableArtworkFlip = p + "disableArtworkFlip"
    static let hideWhileSourceActive = p + "hideWhileSourceActive"
    static let hideTitleExtras = p + "hideTitleExtras"
    static let action = p + "action"
    static let automaticSource = p + "automaticSource"
    static let app = p + "app"
}

/// Now Playing settings the view needs (mirrored from the store by IslandController).
struct NowPlayingStyle: Equatable {
    var color = "mono"             // mono | color | gradient
    var compactWaveform = true
    var artworkFlip = true
    var action = "favorite"        // favorite | repeat | copy  (shuffle was cut; old values read as favorite)
}
