import AppKit
import ApplicationServices
import CryptoKit
import Foundation
import Logging

enum IslandClaudeDesktopActivity {
    static let bundleID = "com.anthropic.claudefordesktop"

    enum Status: String { case working = "running", needsYou = "needs_you", done, error, idle }
    struct ReadLogState {
        private var rowCount: Int?
        private var hadWindows = false
        mutating func shouldLog(rowCount: Int, hasWindows: Bool) -> Bool {
            let changed = self.rowCount.map { $0 != rowCount } ?? (rowCount > 0)
            let emptyWindow = rowCount == 0 && hasWindows && !hadWindows
            self.rowCount = rowCount
            hadWindows = hasWindows
            return changed || emptyWindow
        }
    }
    struct Row: Equatable {
        var status: Status
        var title: String
        var selected = false
        var completionHeld = false
        var id: String {
            "claude-desktop:" + SHA256.hash(data: Data(title.utf8)).map { String(format: "%02x", $0) }.joined()
        }
    }
    struct Snapshot: Equatable {
        var rows: [Row] = []
        var modelLabel: String?
        var chatTabActive = false

        var agents: [IslandAgent] {
            let working = rows.filter { $0.status == .working }
            return rows.filter { $0.status == .working || $0.status == .needsYou || (($0.status == .done || $0.status == .error) && $0.completionHeld) }.map { row in
                var agent = IslandAgent(id: row.id, name: "Claude", app: "Claude", title: row.title,
                                        status: row.status.rawValue, needs_you: row.status == .needsYou)
                // Selection is required: a single busy sidebar chat need not be the open chat.
                if row.status == .working, row.selected, working.count == 1,
                   let modelLabel, let model = parseModel(modelLabel) {
                    agent.local_detail = [model.model, model.effort].compactMap { $0 }.joined(separator: " · ")
                }
                return agent
            }
        }
    }

    protocol AXElement {
        var role: String { get }
        var subrole: String? { get }
        var roleDescription: String? { get }
        func labels() -> [String]
        func isSelected() -> Bool
        func childElements(limit: Int) -> [any AXElement]
    }

    struct Element: AXElement {
        var role: String
        var subrole: String?
        var roleDescription: String?
        var title: String?
        var description: String?
        var selected = false
        var children: [any AXElement] = []
        func labels() -> [String] { [title, description].compactMap { $0 } }
        func isSelected() -> Bool { selected }
        func childElements(limit: Int) -> [any AXElement] { Array(children.prefix(limit)) }
    }

    static func read(_ root: any AXElement) -> Snapshot {
        var snapshot = Snapshot()
        var queue: [(any AXElement, Int, Bool, [any AXElement])] = [(root, 0, false, [])]
        var seen = Set<String>()
        var index = 0
        while index < queue.count, index < 4000 {
            let (element, depth, parentSidebar, siblings) = queue[index]
            index += 1
            if ["AXHeading", "AXStaticText", "AXTextArea", "AXTextField", "AXArticle"].contains(element.role) { continue }
            let sidebarContainer = ["AXGroup", "AXList", "AXScrollArea"].contains(element.role)
            let sideLabel = sidebarContainer ? ((element.subrole ?? "") + " " + (element.roleDescription ?? "")).lowercased() : ""
            let sidebar = parentSidebar || element.role == "AXOutline" || element.subrole == "AXLandmarkNavigation"
                || (sidebarContainer && (sideLabel.contains("sidebar") || sideLabel.contains("side panel") || sideLabel.contains("sidepanel")))
            let labels = ["AXButton", "AXPopUpButton"].contains(element.role) ? element.labels() : []
            if element.role == "AXButton", labels.contains(where: { ["Stop", "Stop response"].contains($0) }) {
                snapshot.chatTabActive = true
            }
            let rowLabel = labels.first { parseClaudeRow($0) != nil }
            if element.role == "AXButton", let rowLabel, sidebar || hasMoreOptionsSibling(rowLabel, siblings: siblings) {
                if let row = parseClaudeRow(rowLabel), seen.insert(row.title).inserted {
                    snapshot.rows.append(.init(status: row.status, title: row.title, selected: element.isSelected()))
                }
            }
            if element.role == "AXPopUpButton", let label = labels.first(where: { $0.hasPrefix("Model: ") }) {
                snapshot.modelLabel = label
            }
            if depth < 30, queue.count < 4000 {
                let children = element.childElements(limit: 4000 - queue.count)
                queue += children.map { ($0, depth + 1, sidebar, children) }
            }
        }
        if snapshot.chatTabActive, !snapshot.rows.contains(where: { $0.status == .working }) {
            snapshot.rows.append(.init(status: .working, title: "Current response", selected: true))
        }
        return snapshot
    }

    static func retainFinishedRows(_ rows: [Row], previous: [Row], finishedAt: inout [String: Date], now: Date) -> [Row] {
        var current: [String: Row] = [:]
        for row in rows { current[row.title] = row }
        for row in rows where row.status == .working || row.status == .needsYou { finishedAt.removeValue(forKey: row.title) }
        for old in previous where old.status == .working && current[old.title] == nil {
            finishedAt[old.title] = finishedAt[old.title] ?? now
            current[old.title] = Row(status: .done, title: old.title, completionHeld: true)
        }
        for old in previous where old.status == .working {
            if let next = current[old.title], next.status != .working, next.status != .needsYou, finishedAt[old.title] == nil {
                finishedAt[old.title] = now
            }
        }
        for (title, date) in Array(finishedAt) {
            if now.timeIntervalSince(date) >= 10 {
                current[title]?.completionHeld = false
                finishedAt.removeValue(forKey: title)
            } else {
                let status = current[title]?.status ?? previous.first(where: { $0.title == title })?.status
                current[title] = Row(status: status == .error ? .error : .done, title: title, completionHeld: true)
            }
        }
        return current.values.sorted { $0.title < $1.title }
    }

    private static func hasMoreOptionsSibling(_ label: String, siblings: [any AXElement]) -> Bool {
        guard let row = parseClaudeRow(label) else { return false }
        return siblings.contains { candidate in
            guard ["AXPopUpButton", "AXButton"].contains(candidate.role) else { return false }
            return candidate.labels().contains("More options for \(row.title)")
        }
    }

    static func parseClaudeRow(_ label: String) -> (status: Status, title: String)? {
        let prefix = "Mark as unread "
        let text = label.hasPrefix(prefix) ? String(label.dropFirst(prefix.count)) : label
        let statuses: [(String, Status)] = [("Something went wrong", .error), ("Needs input", .needsYou),
            ("Working", .working), ("Ready", .done), ("Completed", .done), ("Done", .done), ("Idle", .idle)]
        for (word, status) in statuses where text.hasPrefix(word + " ") {
            let title = String(text.dropFirst(word.count + 1)).trimmingCharacters(in: .whitespacesAndNewlines)
            if !title.isEmpty { return (status, title) }
        }
        return nil
    }

    static func parseModel(_ label: String) -> (model: String, effort: String?)? {
        guard label.hasPrefix("Model: ") else { return nil }
        var model = String(label.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        var effort: String?
        for word in ["Extra high", "Low", "Medium", "High", "Max"] where model.hasSuffix(" " + word) {
            model = String(model.dropLast(word.count + 1))
            effort = word.lowercased()
            break
        }
        guard !model.isEmpty else { return nil }
        return (model, effort)
    }
}

/// Reads only sidebar status buttons and the model popup. No conversation labels or values.
@MainActor
final class IslandClaudeDesktopWatcher {
    typealias Activity = IslandClaudeDesktopActivity
    private let onChange: (Activity.Snapshot) -> Void
    private var observer: AXObserver?
    private var appElement: AXUIElement?
    private var pid: pid_t = 0
    private var pendingRead: DispatchWorkItem?
    private var tokens: [NSObjectProtocol] = []
    private var last = Activity.Snapshot()
    private var finishDates: [String: Date] = [:]
    private var lastReadAt: Date?
    private var manualAttempts = 0
    private var manualLastError: String?
    private var retryWork: [DispatchWorkItem] = []
    private var rowLogState = Activity.ReadLogState()

    static let notifications = [kAXValueChangedNotification, kAXTitleChangedNotification,
        kAXUIElementDestroyedNotification, kAXCreatedNotification, "AXLayoutChanged", kAXFocusedUIElementChangedNotification]

    init(onChange: @escaping (Activity.Snapshot) -> Void) { self.onChange = onChange }

    func start() {
        guard tokens.isEmpty else { return }
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            tokens.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
                guard app?.bundleIdentifier == Activity.bundleID else { return }
                MainActor.assumeIsolated { self?.attachRunning() }
            })
        }
        attachRunning()
    }

    private func attachRunning() {
        let app = NSRunningApplication.runningApplications(withBundleIdentifier: Activity.bundleID).first { !$0.isTerminated }
        guard let app else { detach(); return }
        guard app.processIdentifier != pid else { return }
        detach()
        pid = app.processIdentifier
        manualAttempts = 0
        manualLastError = nil
        let element = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(element, 0.1)
        enableManualAccessibility(element)
        appElement = element
        scheduleManualAccessibilityRetries(element)
        var created: AXObserver?
        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon else { return }
            let watcher = Unmanaged<IslandClaudeDesktopWatcher>.fromOpaque(refcon).takeUnretainedValue()
            MainActor.assumeIsolated { watcher.scheduleRead() }
        }
        guard AXObserverCreate(pid, callback, &created) == .success, let created else { return }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        for name in Self.notifications { AXObserverAddNotification(created, element, name as CFString, refcon) }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(created), .defaultMode)
        observer = created
        scheduleRead()
    }

    private func enableManualAccessibility(_ element: AXUIElement) {
        manualAttempts += 1
        let result = AXUIElementSetAttributeValue(element, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        manualLastError = result == .success ? nil : "AXManualAccessibility: \(result.rawValue)"
        IslandClaudeDesktopStatus.update(pid: pid, snapshot: last, readAt: lastReadAt, attempts: manualAttempts, lastError: manualLastError)
        scheduleRead()
    }

    private func scheduleManualAccessibilityRetries(_ element: AXUIElement) {
        retryWork.forEach { $0.cancel() }; retryWork.removeAll()
        let targetPID = pid
        for delay in [0.5, 1.0, 2.0, 4.0] {
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.pid == targetPID, self.appElement != nil, self.manualAttempts < 5 else { return }
                var windows: CFTypeRef?
                let result = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &windows)
                let hasWindows = result == .success && (windows as? [AXUIElement])?.isEmpty == false
                if hasWindows { self.scheduleRead(); return }
                self.enableManualAccessibility(element)
            }
            retryWork.append(work)
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        }
    }

    private func detach() {
        pendingRead?.cancel(); pendingRead = nil
        retryWork.forEach { $0.cancel() }; retryWork.removeAll()
        if let observer { CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode) }
        observer = nil; appElement = nil; pid = 0
        lastReadAt = nil
        rowLogState = Activity.ReadLogState()
        finishDates.removeAll()
        manualAttempts = 0
        manualLastError = nil
        IslandClaudeDesktopStatus.update(pid: nil, snapshot: nil, readAt: nil, attempts: 0, lastError: nil)
        publish(Activity.Snapshot())
    }

    func scheduleRead(after delay: TimeInterval = 0.3) {
        pendingRead?.cancel()
        let work = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.read() } }
        pendingRead = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func publish(_ snapshot: Activity.Snapshot) {
        guard snapshot != last else { return }
        last = snapshot
        IslandClaudeDesktopStatus.update(pid: pid == 0 ? nil : pid, snapshot: snapshot, readAt: lastReadAt,
                                         attempts: manualAttempts, lastError: manualLastError)
        onChange(snapshot)
    }

    private func read() {
        guard let appElement else { return }
        var snapshot = Activity.read(IslandClaudeDesktopNativeAXElement(appElement))
        lastReadAt = Date()
        snapshot.rows = Activity.retainFinishedRows(snapshot.rows, previous: last.rows, finishedAt: &finishDates, now: lastReadAt!)
        publish(snapshot)
        IslandClaudeDesktopStatus.update(pid: pid == 0 ? nil : pid, snapshot: snapshot, readAt: lastReadAt,
                                         attempts: manualAttempts, lastError: manualLastError)
        let logger = Logger(label: "island.claude")
        let hasWindows = self.appHasWindows()
        if rowLogState.shouldLog(rowCount: snapshot.rows.count, hasWindows: hasWindows) {
            logger.info("Claude Desktop sidebar rows observed", metadata: ["row_count": .string(String(snapshot.rows.count))])
        }
        if let nextExpiry = finishDates.values.map({ $0.addingTimeInterval(10) }).min() {
            scheduleRead(after: max(0.05, nextExpiry.timeIntervalSinceNow + 0.05))
        }
    }

    private func appHasWindows() -> Bool {
        guard let appElement else { return false }
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &value) == .success
            && (value as? [AXUIElement])?.isEmpty == false
    }
}
