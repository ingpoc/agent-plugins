import AppKit
import ApplicationServices
import Foundation
import Logging

/// Force Chromium/Electron shells to build a real AX tree (VoiceOver-style).
/// Prefer `AXManualAccessibility` (Electron); fall back to `AXEnhancedUserInterface`.
/// Not a browser stack — Comet pages stay on comet-control; this unlocks Slack/VS Code/Cursor shells.
final class ChromiumAXEnabler: @unchecked Sendable {
    private let logger: Logger
    private let lock = NSLock()
    /// PIDs we already attempted (success or fail) — avoid per-state hammering.
    private var attempted: Set<pid_t> = []

    init(logger: Logger) { self.logger = logger }

    /// Sparse chrome-only tree: traffic lights + empty group, no useful labels.
    static func isSparse(_ snapshot: AXSnapshot) -> Bool {
        let labeled = snapshot.elements.filter {
            let lab = ($0.label ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return !lab.isEmpty
        }.count
        return snapshot.elements.count <= 16 && labeled <= 3
    }

    static func isChromiumShell(pid: pid_t, bundleID: String?) -> Bool {
        if let bundleID {
            let id = bundleID.lowercased()
            if id.hasPrefix("com.todesktop.") { return true }
            if id.contains("electron") { return true }
            // Cursor, VS Code forks, Slack desktop, Discord, etc.
            if id.hasPrefix("com.microsoft.vscode") { return true }
            if id.hasPrefix("com.tinyspeck.slackmacgap") { return true }
            if id.hasPrefix("com.hnc.discord") { return true }
            if id.hasPrefix("ai.perplexity.comet") { return true }
        }
        guard let app = NSRunningApplication(processIdentifier: pid),
              let url = app.bundleURL else { return false }
        let frameworks = url.appendingPathComponent("Contents/Frameworks")
        guard let names = try? FileManager.default.contentsOfDirectory(
            atPath: frameworks.path
        ) else { return false }
        return names.contains {
            $0.contains("Electron") || $0.contains("Chromium")
                || $0.contains("Comet Framework")
        }
    }

    /// If shell looks Chromium and tree is sparse, enable AX once and signal rewalk.
    func enrichIfNeeded(
        axApp: AXUIElement,
        pid: pid_t,
        bundleID: String?,
        snapshot: AXSnapshot
    ) -> String? {
        guard Self.isChromiumShell(pid: pid, bundleID: bundleID) else { return nil }
        guard Self.isSparse(snapshot) else { return nil }

        lock.lock()
        let already = attempted.contains(pid)
        lock.unlock()
        if already { return nil }

        let manual = AXUIElementSetAttributeValue(
            axApp,
            "AXManualAccessibility" as CFString,
            kCFBooleanTrue
        )
        if manual == .success {
            lock.lock()
            attempted.insert(pid)
            lock.unlock()
            logger.info("AXManualAccessibility enabled pid=\(pid)")
            return "AXManualAccessibility"
        }

        let enhanced = AXUIElementSetAttributeValue(
            axApp,
            "AXEnhancedUserInterface" as CFString,
            kCFBooleanTrue
        )
        if enhanced == .success {
            lock.lock()
            attempted.insert(pid)
            lock.unlock()
            logger.info("AXEnhancedUserInterface enabled pid=\(pid)")
            return "AXEnhancedUserInterface"
        }

        lock.lock()
        attempted.insert(pid)
        lock.unlock()
        logger.info(
            "chromium AX enrich failed pid=\(pid) manual=\(manual.rawValue) enhanced=\(enhanced.rawValue)"
        )
        return nil
    }

    func clearAttempt(_ pid: pid_t) {
        lock.lock()
        attempted.remove(pid)
        lock.unlock()
    }
}
