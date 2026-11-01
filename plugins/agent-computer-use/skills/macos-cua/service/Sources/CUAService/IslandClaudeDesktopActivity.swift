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
        /// Working because the open chat streams (composer "Stop response" / "Currently streaming message"),
        /// not because its sidebar row says so: plain chats keep "Idle <title>" while Claude replies.
        var openChat = false
        var id: String {
            "claude-desktop:" + SHA256.hash(data: Data(title.utf8)).map { String(format: "%02x", $0) }.joined()
        }
    }
    struct Snapshot: Equatable {
        var rows: [Row] = []
        var modelLabel: String?
        var chatTabActive = false
        var stopButtonVisible = false
        var streamingMessageVisible = false
        /// Open chat's title from the header's "<title>, rename chat" button.
        var openChatTitle: String?
        var responding: Bool { stopButtonVisible || streamingMessageVisible }

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

    /// Live 4.4212 tree: the composer's "Stop response" sits at depth 32 from the app element,
    /// below the old 30 cap; chat messages are pruned, so a deeper walk stays cheap.
    static let maxDepth = 48
    static let renameChatSuffix = ", rename chat"
    static let streamingMessageLabel = "Currently streaming message"

    static func read(_ root: any AXElement) -> Snapshot {
        var snapshot = Snapshot()
        var queue: [(any AXElement, Int, Bool, [any AXElement])] = [(root, 0, false, [])]
        var seen = Set<String>()
        var moreOptionsTitles = Set<String>()
        var candidates: [(label: String, selected: Bool, inSidebar: Bool)] = []
        var index = 0
        while index < queue.count, index < 4000 {
            let (element, depth, parentSidebar, siblings) = queue[index]
            index += 1
            // Headings are walked (the chat header's "<title>, rename chat" button sits in one) but never
            // read; message headings ("You said: …") live inside pruned message articles.
            if ["AXStaticText", "AXTextArea", "AXTextField", "AXArticle"].contains(element.role) { continue }
            // A chat message: read only its accessible name ("Message 2 of 3" / "Currently streaming
            // message"), never its content, and don't descend.
            if element.role == "AXGroup", element.subrole == "AXDocumentArticle" {
                if element.labels().contains(streamingMessageLabel) { snapshot.streamingMessageVisible = true }
                continue
            }
            let sidebarContainer = ["AXGroup", "AXList", "AXScrollArea"].contains(element.role)
            let sideLabel = sidebarContainer ? ((element.subrole ?? "") + " " + (element.roleDescription ?? "")).lowercased() : ""
            // Claude 4.4212+ titles the sidebar group "Sidebar" (no sidebar subrole / role description).
            let titledSidebar = sidebarContainer && element.labels().contains { $0.lowercased() == "sidebar" }
            let sidebar = parentSidebar || element.role == "AXOutline" || element.subrole == "AXLandmarkNavigation" || titledSidebar
                || (sidebarContainer && (sideLabel.contains("sidebar") || sideLabel.contains("side panel") || sideLabel.contains("sidepanel")))
            let labels = ["AXButton", "AXPopUpButton"].contains(element.role) ? element.labels() : []
            if element.role == "AXButton", labels.contains(where: { ["Stop", "Stop response"].contains($0) }) {
                snapshot.stopButtonVisible = true
            }
            if element.role == "AXButton", snapshot.openChatTitle == nil,
               let label = labels.first(where: { $0.hasSuffix(renameChatSuffix) && $0.count > renameChatSuffix.count }) {
                snapshot.openChatTitle = String(label.dropLast(renameChatSuffix.count))
            }
            if element.role == "AXRadioButton", element.labels().contains("Chat and Cowork"), element.isSelected() {
                snapshot.chatTabActive = true
            }
            if element.role == "AXPopUpButton" || element.role == "AXButton" {
                for label in labels where label.hasPrefix("More options for ") {
                    moreOptionsTitles.insert(String(label.dropFirst("More options for ".count)))
                }
            }
            if element.role == "AXButton", let rowLabel = labels.first(where: { parseClaudeRow($0) != nil }) {
                if sidebar || hasMoreOptionsSibling(rowLabel, siblings: siblings) {
                    if let row = parseClaudeRow(rowLabel), seen.insert(row.title).inserted {
                        snapshot.rows.append(.init(status: row.status, title: row.title, selected: element.isSelected()))
                    }
                } else {
                    candidates.append((rowLabel, element.isSelected(), false))
                }
            }
            if element.role == "AXPopUpButton", let label = labels.first(where: { $0.hasPrefix("Model: ") }) {
                snapshot.modelLabel = label
            }
            if depth < maxDepth, queue.count < 4000 {
                let children = element.childElements(limit: 4000 - queue.count)
                queue += children.map { ($0, depth + 1, sidebar, children) }
            }
        }
        // Rows whose "More options for <title>" popup sits in a different parent (live 4.4212 tree).
        for candidate in candidates {
            guard let row = parseClaudeRow(candidate.label), moreOptionsTitles.contains(row.title),
                  seen.insert(row.title).inserted else { continue }
            snapshot.rows.append(.init(status: row.status, title: row.title, selected: candidate.selected))
        }
        // A plain chat's sidebar row stays "Idle <title>" while Claude replies: the open chat is the
        // working one. Mark its row (matched by the header title) or add it.
        if snapshot.responding, !snapshot.rows.contains(where: { $0.status == .working }) {
            if let title = snapshot.openChatTitle, let i = snapshot.rows.firstIndex(where: { sameTitle($0.title, title) }) {
                snapshot.rows[i].status = .working
                snapshot.rows[i].selected = true
                snapshot.rows[i].openChat = true
            } else {
                snapshot.rows.append(.init(status: .working, title: snapshot.openChatTitle ?? "Current response",
                                           selected: true, openChat: true))
            }
        }
        return snapshot
    }

    static func retainFinishedRows(_ rows: [Row], previous: [Row], finishedAt: inout [String: Date], now: Date) -> [Row] {
        var current: [String: Row] = [:]
        for row in rows { current[row.title] = row }
        for row in rows where row.status == .working || row.status == .needsYou { finishedAt.removeValue(forKey: row.title) }
        // Claude retitles a new chat mid-reply ("ACU island test…" → "History of lighthouses essay"):
        // the open chat is still working, so the old title must not flash Done.
        let openChatStillWorking = rows.contains { $0.openChat && $0.status == .working }
        for old in previous where old.status == .working && current[old.title] == nil {
            if old.openChat, openChatStillWorking { continue }
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

    /// Header and sidebar truncate long titles differently ("Draft about clo..." vs the full title).
    static func sameTitle(_ a: String, _ b: String) -> Bool {
        if a == b { return true }
        func stem(_ s: String) -> String? {
            for tail in ["...", "…"] where s.hasSuffix(tail) { return String(s.dropLast(tail.count)) }
            return nil
        }
        if let sa = stem(a), sa.count >= 8, b.hasPrefix(sa) { return true }
        if let sb = stem(b), sb.count >= 8, a.hasPrefix(sb) { return true }
        return false
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
        // "Mark as unread <title>" with no status word: a finished chat the user hasn't opened yet.
        if label.hasPrefix(prefix) {
            let title = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !title.isEmpty { return (.done, title) }
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
        // Poll as well as observe: Electron does not always post AX notifications for sidebar status
        // changes, and a Working phase can last only a few seconds.
        let busy = snapshot.responding || snapshot.rows.contains { $0.status == .working || $0.status == .needsYou }
        var next: TimeInterval = busy ? 1.0 : 2.0
        if let nextExpiry = finishDates.values.map({ $0.addingTimeInterval(10) }).min() {
            next = min(next, max(0.05, nextExpiry.timeIntervalSinceNow + 0.05))
        }
        scheduleRead(after: next)
    }

    private func appHasWindows() -> Bool {
        guard let appElement else { return false }
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &value) == .success
            && (value as? [AXUIElement])?.isEmpty == false
    }
}
