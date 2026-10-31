import AppKit
import ApplicationServices
import CryptoKit
import Foundation
import Logging

enum IslandClaudeDesktopActivity {
    static let bundleID = "com.anthropic.claudefordesktop"

    enum Status: String { case working = "running", needsYou = "needs_you", done, error, idle }
    struct Row: Equatable {
        var status: Status
        var title: String
        var selected = false
        var id: String {
            "claude-desktop:" + SHA256.hash(data: Data(title.utf8)).map { String(format: "%02x", $0) }.joined()
        }
    }
    struct Snapshot: Equatable {
        var rows: [Row] = []
        var modelLabel: String?

        var agents: [IslandAgent] {
            let working = rows.filter { $0.status == .working }
            return rows.filter { $0.status == .working || $0.status == .needsYou }.map { row in
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
    private let enabler = ChromiumAXEnabler(logger: Logger(label: "island.claude"))
    private var observer: AXObserver?
    private var appElement: AXUIElement?
    private var pid: pid_t = 0
    private var pendingRead: DispatchWorkItem?
    private var tokens: [NSObjectProtocol] = []
    private var last = Activity.Snapshot()

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
        let element = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(element, 0.1)
        enabler.enableManualAccessibility(axApp: element)
        appElement = element
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

    private func detach() {
        pendingRead?.cancel(); pendingRead = nil
        if let observer { CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode) }
        observer = nil; appElement = nil; pid = 0
        publish(Activity.Snapshot())
    }

    func scheduleRead() {
        pendingRead?.cancel()
        let work = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.read() } }
        pendingRead = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
    }

    private func publish(_ snapshot: Activity.Snapshot) {
        guard snapshot != last else { return }
        last = snapshot
        onChange(snapshot)
    }

    private func read() {
        guard let appElement else { return }
        var snapshot = Activity.Snapshot()
        var queue: [(AXUIElement, Int, Bool)] = [(appElement, 0, false)], index = 0
        var seen = Set<String>()
        while index < queue.count, index < 4000 {
            let (element, depth, parentSidebar) = queue[index]; index += 1
            let role = IslandGrokBotWatcher.string(element, kAXRoleAttribute) ?? ""
            // Never read conversation leaves; only navigation/outline descendants qualify as sidebar buttons.
            if ["AXHeading", "AXStaticText", "AXTextArea", "AXTextField", "AXArticle"].contains(role) { continue }
            let sidebar = parentSidebar || role == kAXOutlineRole as String
                || IslandGrokBotWatcher.string(element, kAXSubroleAttribute) == "AXLandmarkNavigation"
            if (sidebar && role == kAXButtonRole as String) || role == kAXPopUpButtonRole as String {
                for attribute in [kAXTitleAttribute, kAXDescriptionAttribute] {
                    guard let label = IslandGrokBotWatcher.string(element, attribute) else { continue }
                    if role == kAXButtonRole as String, let row = Activity.parseClaudeRow(label), seen.insert(row.title).inserted {
                        var selected: CFTypeRef?
                        AXUIElementCopyAttributeValue(element, kAXSelectedAttribute as CFString, &selected)
                        snapshot.rows.append(.init(status: row.status, title: row.title, selected: selected as? Bool == true))
                        break
                    } else if role == kAXPopUpButtonRole as String, label.hasPrefix("Model: ") {
                        snapshot.modelLabel = label
                        break
                    }
                }
                continue
            }
            if depth < 30, queue.count < 4000 {
                queue += IslandGrokBotWatcher.children(element).prefix(4000 - queue.count).map { ($0, depth + 1, sidebar) }
            }
        }
        publish(snapshot)
    }
}
