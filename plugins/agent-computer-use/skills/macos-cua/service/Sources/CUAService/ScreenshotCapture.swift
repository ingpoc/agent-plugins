import AppKit
import CoreGraphics
import Foundation
import Logging
import ScreenCaptureKit

final class ScreenshotCapture: @unchecked Sendable {
    private let logger: Logger
    private let cacheDir: String
    /// Unbounded SCShareableContent wedged get_app_state (start/end shots) and every batched act.
    private static let sckDeadlineNs: UInt64 = 700_000_000

    init(logger: Logger) {
        self.logger = logger
        self.cacheDir = NSString(
            string: "~/.cache/macos-cua/screenshots"
        ).expandingTildeInPath
        try? FileManager.default.createDirectory(
            atPath: cacheDir, withIntermediateDirectories: true
        )
    }

    private let lock = NSLock()
    private var cachedID: CGWindowID = 0
    private var cachedPath: String?
    private var cachedAt: TimeInterval = 0

    func invalidate() {
        lock.lock()
        cachedPath = nil
        cachedAt = 0
        lock.unlock()
    }

    func capture(windowID: CGWindowID, axBounds: CGRect? = nil) async -> (path: String?, cached: Bool) {
        _ = axBounds
        lock.lock()
        let hit = cachedPath.flatMap { path -> String? in
            guard cachedID == windowID,
                  ProcessInfo.processInfo.systemUptime - cachedAt < 0.2,
                  FileManager.default.fileExists(atPath: path) else { return nil }
            return path
        }
        lock.unlock()
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
        lock.lock()
        cachedID = windowID
        cachedPath = path
        cachedAt = ProcessInfo.processInfo.systemUptime
        lock.unlock()
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

    /// Codex uses ScreenCaptureKit. Capture the window at backing scale.
    private func captureScreenCaptureKit(windowID: CGWindowID) async -> String? {
        guard #available(macOS 14.4, *) else { return nil }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: true
            )
            guard let window = content.windows.first(where: { $0.windowID == windowID }) else {
                return nil
            }
            let filter = SCContentFilter(desktopIndependentWindow: window)
            let frame = window.frame
            let scale = NSScreen.screens.map(\.backingScaleFactor).max() ?? 2
            let config = SCStreamConfiguration()
            config.width = max(1, Int((frame.width * scale).rounded()))
            config.height = max(1, Int((frame.height * scale).rounded()))
            config.showsCursor = false
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
        return path
    }
}
