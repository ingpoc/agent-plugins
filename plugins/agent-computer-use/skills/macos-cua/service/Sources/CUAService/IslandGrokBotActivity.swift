import AppKit
import ApplicationServices
import Foundation

/// Grok Bot's own "Working" status, read from its sidebar. Documented attention states
/// (https://docs.x.ai/grok-bot/settings-and-notifications): the sidebar shows "Working or typing
/// status" per Bot. Its accessibility tree labels a busy Bot's row "<name>, Working". There is no
/// local file, URL scheme or public API for this (desktop-status.json holds only the app's pid and
/// version), so this reads the app's AX tree, event-driven: an AXObserver wakes it, never a timer.
enum IslandGrokBotActivity {
    static let bundleID = "com.anysphere.sand"
    static let workingSuffix = ", Working"

    static let statusSuffix = " is working"

    /// Bot names the app reports as working, in sidebar order, without duplicates. Two native
    /// signals: a sidebar row labelled "<name>, Working" (shown while a reply streams), and the
    /// app's screen-reader status "<name> is working" for the open conversation (kept for the
    /// whole turn, tool calls included). A status name counts only if it is a Bot in the sidebar.
    static func workingBots(labels: [String], statuses: [String] = []) -> [String] {
        let names = labels.map { label -> String in
            for suffix in [workingSuffix] where label.hasSuffix(suffix) { return String(label.dropLast(suffix.count)) }
            return label.components(separatedBy: ", ").first ?? label
        }.map { $0.trimmingCharacters(in: .whitespaces) }
        let fromStatus = Set(statuses.compactMap { text -> String? in
            guard text.hasSuffix(statusSuffix) else { return nil }
            return String(text.dropLast(statusSuffix.count)).trimmingCharacters(in: .whitespaces)
        })
        var seen = Set<String>()
        return zip(labels, names).compactMap { label, name in
            guard !name.isEmpty, label.hasSuffix(workingSuffix) || fromStatus.contains(name),
                  seen.insert(name).inserted else { return nil }
            return name
        }
    }

    /// "Agent Computer Use · Macos computer use" → title "Agent Computer Use" (the Bot's name;
    /// the part after " · " is its label).
    static func shortName(_ name: String) -> String {
        name.components(separatedBy: " · ").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? name
    }

    static func rows(_ working: [(name: String, since: Date)], now: Date) -> [IslandAgent] {
        working.map { bot in
            let age = max(0, Int(now.timeIntervalSince(bot.since)))
            var row = IslandAgent(id: "local:grok-bot:\(bot.name)", name: "Grok Bot", app: "Grok Bot",
                                  title: shortName(bot.name), status: "running",
                                  elapsed_s: Double(age), last_seen_at: bot.since.timeIntervalSince1970)
            row.local_detail = "Grok Bot · \(IslandCodexThreads.duration(age))"
            row.tooltip = bot.name
            return row
        }
    }
}

/// Watches the Grok Bot app with NSWorkspace launch/quit notifications and one AXObserver.
/// Idle cost is zero: no timers; each AX notification only schedules one coalesced re-read of the
/// sidebar's "Bot list" (cached element, a few dozen attribute reads).
@MainActor
final class IslandGrokBotWatcher {
    var onChange: ([String]) -> Void
    private var observer: AXObserver?
    private var appElement: AXUIElement?
    private var pid: pid_t = 0
    private var botList: AXUIElement?
    private var webArea: AXUIElement?
    private var readScheduled = false
    private var last: [String] = []
    private var tokens: [NSObjectProtocol] = []

    /// Row re-renders (created/destroyed), label changes, and the "<bot> is working" live region.
    /// Not AXValueChanged: it fires on every streamed character and carries no status.
    static let notifications = [kAXTitleChangedNotification, kAXCreatedNotification,
                                kAXUIElementDestroyedNotification, "AXLiveRegionChanged",
                                "AXLiveRegionCreated", kAXWindowCreatedNotification]

    init(onChange: @escaping ([String]) -> Void) { self.onChange = onChange }

    func start() {
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            tokens.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
                guard app?.bundleIdentifier == IslandGrokBotActivity.bundleID else { return }
                MainActor.assumeIsolated { self?.attachRunning() }
            })
        }
        attachRunning()
    }

    private func attachRunning() {
        let app = NSRunningApplication.runningApplications(withBundleIdentifier: IslandGrokBotActivity.bundleID)
            .first { !$0.isTerminated }
        guard let app else { detach(); return }
        guard app.processIdentifier != pid else { return }
        detach()
        pid = app.processIdentifier
        let element = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(element, 0.5)
        // Electron builds its AX tree only for assistive clients (same switch VoiceOver flips);
        // this changes nothing on screen.
        AXUIElementSetAttributeValue(element, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        appElement = element
        var created: AXObserver?
        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon else { return }
            let watcher = Unmanaged<IslandGrokBotWatcher>.fromOpaque(refcon).takeUnretainedValue()
            MainActor.assumeIsolated { watcher.scheduleRead() }
        }
        guard AXObserverCreate(pid, callback, &created) == .success, let created else { return }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        for n in Self.notifications { AXObserverAddNotification(created, element, n as CFString, refcon) }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(created), .defaultMode)
        observer = created
        scheduleRead()
    }

    private func detach() {
        if let observer { CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode) }
        observer = nil; appElement = nil; botList = nil; webArea = nil; pid = 0
        publish([])
    }

    func scheduleRead() {
        guard !readScheduled else { return }
        readScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.readScheduled = false
                self.read()
            }
        }
    }

    private func read() {
        guard let appElement else { return }
        if botList == nil || Self.string(botList!, kAXRoleAttribute) == nil {
            botList = Self.find(appElement) { Self.label($0) == "Bot list" }
        }
        guard let botList else { publish([]); return }
        var labels: [String] = []
        Self.walk(botList, limit: 600) { e in
            if Self.string(e, kAXRoleAttribute) == kAXButtonRole as String, let l = Self.label(e) { labels.append(l) }
        }
        // The screen-reader status sits two levels under the web area (a 1×1 pt live region).
        if webArea == nil || Self.string(webArea!, kAXRoleAttribute) == nil {
            webArea = Self.find(appElement) { Self.string($0, kAXRoleAttribute) == "AXWebArea" }
        }
        var statuses: [String] = []
        if let webArea {
            for group in Self.children(webArea) {
                for e in Self.children(group) where Self.string(e, kAXRoleAttribute) == kAXStaticTextRole as String {
                    if let v = Self.string(e, kAXValueAttribute), v.hasSuffix(IslandGrokBotActivity.statusSuffix) { statuses.append(v) }
                }
            }
        }
        publish(IslandGrokBotActivity.workingBots(labels: labels, statuses: statuses))
    }

    private func publish(_ names: [String]) {
        guard names != last else { return }
        last = names
        onChange(names)
    }

    static func string(_ e: AXUIElement, _ attribute: String) -> String? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, attribute as CFString, &v) == .success else { return nil }
        return v as? String
    }

    static func label(_ e: AXUIElement) -> String? {
        [kAXDescriptionAttribute, kAXTitleAttribute].lazy.compactMap { string(e, $0) }.first { !$0.isEmpty }
    }

    static func children(_ e: AXUIElement) -> [AXUIElement] {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, kAXChildrenAttribute as CFString, &v) == .success else { return [] }
        return v as? [AXUIElement] ?? []
    }

    static func walk(_ root: AXUIElement, limit: Int, _ visit: (AXUIElement) -> Void) {
        var queue = [root], seen = 0
        while !queue.isEmpty, seen < limit {
            let e = queue.removeFirst(); seen += 1
            visit(e)
            queue += children(e)
        }
    }

    static func find(_ root: AXUIElement, limit: Int = 4000, _ match: (AXUIElement) -> Bool) -> AXUIElement? {
        var queue = [root], seen = 0
        while !queue.isEmpty, seen < limit {
            let e = queue.removeFirst(); seen += 1
            if match(e) { return e }
            queue += children(e)
        }
        return nil
    }
}
