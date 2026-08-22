import AppKit
import CoreGraphics
import Foundation
import Logging
import os
import ScreenCaptureKit

final class ScreenshotCapture: @unchecked Sendable {
    // `import os` also exports Logger — pin Logging.
    private let logger: Logging.Logger
    private let cacheDir: String
    /// Unbounded SCShareableContent wedged get_app_state (start/end shots) and every batched act.
    private static let sckDeadlineNs: UInt64 = 700_000_000
    /// Cap on-disk PNG residue from before/after act captures.
    private static let maxScreenshotFiles = 48
    private static let maxScreenshotBytes: Int64 = 24 * 1024 * 1024
    /// Short-TTL catalog cache — re-enumerate every SCK shot was the hot cost.
    private static let shareableTTL: TimeInterval = 1.0

    private struct CacheState {
        var cachedID: CGWindowID = 0
        var cachedPath: String?
        var cachedAt: TimeInterval = 0
    }

    private struct ShareableState {
        var content: SCShareableContent?
        var at: TimeInterval = 0
    }

    private let cache = OSAllocatedUnfairLock(initialState: CacheState())
    private let shareable = OSAllocatedUnfairLock(initialState: ShareableState())

    init(logger: Logging.Logger) {
        self.logger = logger
        self.cacheDir = NSString(
            string: "~/.cache/macos-cua/screenshots"
        ).expandingTildeInPath
        try? FileManager.default.createDirectory(
            atPath: cacheDir, withIntermediateDirectories: true
        )
        pruneScreenshotCache()
    }

    func invalidate() {
        cache.withLock { state in
            state.cachedPath = nil
            state.cachedAt = 0
        }
        shareable.withLock { state in
            state.content = nil
            state.at = 0
        }
    }

    func capture(windowID: CGWindowID, axBounds: CGRect? = nil) async -> (path: String?, cached: Bool) {
        _ = axBounds
        let hit: String? = cache.withLock { state in
            guard let path = state.cachedPath,
                  state.cachedID == windowID,
                  ProcessInfo.processInfo.systemUptime - state.cachedAt < 0.2,
                  FileManager.default.fileExists(atPath: path) else { return nil }
            return path
        }
        if let hit { return (hit, true) }

        // Window-backed pixels only. AX-rect CG of a Stage Manager stub
        // photographs wallpaper at the stale AX frame.
        if let path = captureCGWindowList(windowID: windowID) {
            remember(path, windowID: windowID)
            return (path, false)
        }
        let sck = await firstCompleted(
            deadlineNs: Self.sckDeadlineNs,
            work: { await self.captureScreenCaptureKit(windowID: windowID) }
        )
        if let sck { remember(sck, windowID: windowID) }
        return (sck, false)
    }

    private func remember(_ path: String, windowID: CGWindowID) {
        cache.withLock { state in
            state.cachedID = windowID
            state.cachedPath = path
            state.cachedAt = ProcessInfo.processInfo.systemUptime
        }
    }

    private func firstCompleted(
        deadlineNs: UInt64,
        work: @escaping @Sendable () async -> String?
    ) async -> String? {
        await withTaskGroup(of: String?.self) { group in
            group.addTask { await work() }
            group.addTask {
                try? await Task.sleep(nanoseconds: deadlineNs)
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
    }

    private func loadShareableContent() async throws -> SCShareableContent {
        let hit: SCShareableContent? = shareable.withLock { state in
            guard let content = state.content,
                  ProcessInfo.processInfo.systemUptime - state.at < Self.shareableTTL
            else { return nil }
            return content
        }
        if let hit { return hit }
        let t0 = ProcessInfo.processInfo.systemUptime
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true
        )
        let ms = Int(((ProcessInfo.processInfo.systemUptime - t0) * 1000).rounded())
        logger.info("SCShareableContent catalog ms=\(ms)")
        shareable.withLock { state in
            state.content = content
            state.at = ProcessInfo.processInfo.systemUptime
        }
        return content
    }

    /// Codex uses ScreenCaptureKit. Capture the window at filter pixel scale.
    private func captureScreenCaptureKit(windowID: CGWindowID) async -> String? {
        guard #available(macOS 14.4, *) else { return nil }
        do {
            let content = try await loadShareableContent()
            guard let window = content.windows.first(where: { $0.windowID == windowID }) else {
                return nil
            }
            let filter = SCContentFilter(desktopIndependentWindow: window)
            let frame = window.frame
            let scale = Double(filter.pointPixelScale)
            let config = SCStreamConfiguration()
            config.width = max(1, Int((frame.width * scale).rounded()))
            config.height = max(1, Int((frame.height * scale).rounded()))
            config.showsCursor = false
            config.ignoreShadowsSingleWindow = true
            config.captureResolution = .nominal
            let image = try await SCScreenshotManager.captureImage(
                contentFilter: filter, configuration: config
            )
            return writePNG(image, windowID: windowID)
        } catch {
            logger.info("ScreenCaptureKit capture failed, using CGWindowList: \(error.localizedDescription)")
            return nil
        }
    }

    private func captureCGWindowList(windowID: CGWindowID) -> String? {
        let image = CGWindowListCreateImage(
            .null,
            .optionIncludingWindow,
            windowID,
            [.boundsIgnoreFraming, .bestResolution]
        )
        guard let image else {
            logger.warning("Screenshot failed for window \(windowID)")
            return nil
        }
        return writePNG(image, windowID: windowID)
    }

    private func writePNG(_ image: CGImage, windowID: CGWindowID) -> String? {
        let filename = "cua-\(windowID)-\(Int(Date().timeIntervalSince1970 * 1000)).png"
        let path = (cacheDir as NSString).appendingPathComponent(filename)
        let url = URL(fileURLWithPath: path)
        guard let dest = CGImageDestinationCreateWithURL(
            url as CFURL, "public.png" as CFString, 1, nil
        ) else { return nil }
        CGImageDestinationAddImage(dest, image, [
            kCGImagePropertyDPIWidth: 144,
            kCGImagePropertyDPIHeight: 144,
        ] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        pruneScreenshotCache()
        return path
    }

    /// Keep newest PNGs under file + byte caps. Called on init and every write.
    private func pruneScreenshotCache() {
        let fm = FileManager.default
        let dir = URL(fileURLWithPath: cacheDir)
        guard let urls = try? fm.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey],
            options: [.skipsHiddenFiles]
        ) else { return }

        var items: [(url: URL, date: Date, size: Int64)] = []
        items.reserveCapacity(urls.count)
        for url in urls where url.pathExtension.lowercased() == "png" {
            let vals = try? url.resourceValues(forKeys: [
                .contentModificationDateKey, .fileSizeKey,
            ])
            items.append((
                url,
                vals?.contentModificationDate ?? .distantPast,
                Int64(vals?.fileSize ?? 0)
            ))
        }
        let totalBytes = items.reduce(Int64(0), { $0 + $1.size })
        guard items.count > Self.maxScreenshotFiles
            || totalBytes > Self.maxScreenshotBytes
        else { return }

        items.sort { $0.date > $1.date }
        var keptBytes: Int64 = 0
        var removed = 0
        for (i, item) in items.enumerated() {
            let overCount = i >= Self.maxScreenshotFiles
            let overBytes = keptBytes + item.size > Self.maxScreenshotBytes && i > 0
            if overCount || overBytes {
                try? fm.removeItem(at: item.url)
                removed += 1
            } else {
                keptBytes += item.size
            }
        }
        if removed > 0 {
            logger.info("pruned \(removed) screenshot(s); keptBytes=\(keptBytes)")
        }
    }
}
