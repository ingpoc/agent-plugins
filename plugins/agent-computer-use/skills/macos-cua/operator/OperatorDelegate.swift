import AppKit
import Darwin
import Foundation

@MainActor
final class OperatorDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let stateURL: URL
    private var lastData: Data?
    private var currentState = OperatorState()
    private var statusItem: NSStatusItem!
    private var appItem: NSMenuItem!
    private var windowItem: NSMenuItem!
    private var harnessItem: NSMenuItem!
    private var stateItem: NSMenuItem!
    private var toggleItem: NSMenuItem!
    private var panel: NSPanel!
    private var previewView: PreviewView!
    private var cursorOverlayPanel: NSPanel!
    private var cursorOverlayView: CursorOverlayView!
    private var cursorAnimationTimer: Timer?
    private var cursorAnimationStart = NSPoint.zero
    private var cursorAnimationTarget = NSPoint.zero
    private var cursorAnimationStartedAt: TimeInterval = 0
    private var cursorAnimationTargetX = 0.5
    private var cursorAnimationTargetY = 0.5
    private var cursorAnimationTargetUpdateID = ""
    private var cursorAnimationDuration: TimeInterval = 0.32
    private var detailLabel: NSTextField!
    private var hideButton: NSButton!
    private var timer: Timer?
    private var pipVisible = true

    init(stateURL: URL) {
        self.stateURL = stateURL
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        pipVisible = UserDefaults.standard.object(forKey: "pipVisible") as? Bool ?? true
        configureStatusItem()
        configurePanel()
        configureCursorOverlay()
        reloadState(force: true)
        timer = Timer.scheduledTimer(
            timeInterval: 0.05,
            target: self,
            selector: #selector(pollState),
            userInfo: nil,
            repeats: true
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
    }

    private func configureStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.imagePosition = .imageLeading
            button.toolTip = "macos-cua operator — idle"
        }

        let menu = NSMenu(title: "macos-cua")
        let header = NSMenuItem(title: "macos-cua operator", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        appItem = NSMenuItem(title: "Controlled app: None", action: nil, keyEquivalent: "")
        windowItem = NSMenuItem(title: "Window: None", action: nil, keyEquivalent: "")
        harnessItem = NSMenuItem(title: "Harness: Unknown", action: nil, keyEquivalent: "")
        stateItem = NSMenuItem(title: "Status: Idle", action: nil, keyEquivalent: "")
        for item in [appItem, windowItem, harnessItem, stateItem] {
            item?.isEnabled = false
            if let item { menu.addItem(item) }
        }

        menu.addItem(.separator())
        toggleItem = NSMenuItem(
            title: "Hide Picture in Picture",
            action: #selector(togglePictureInPicture),
            keyEquivalent: ""
        )
        toggleItem.target = self
        menu.addItem(toggleItem)

        let refresh = NSMenuItem(title: "Refresh", action: #selector(refresh), keyEquivalent: "r")
        refresh.target = self
        menu.addItem(refresh)

        menu.addItem(.separator())
        let end = NSMenuItem(
            title: "End Controlled Session",
            action: #selector(endSession),
            keyEquivalent: "e"
        )
        end.target = self
        menu.addItem(end)
        statusItem.menu = menu
    }

    private func configurePanel() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 340),
            styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.title = "macos-cua — No controlled app"
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovableByWindowBackground = true
        panel.contentMinSize = NSSize(width: 280, height: 220)
        panel.delegate = self

        let root = NSVisualEffectView()
        root.material = .sidebar
        root.blendingMode = .behindWindow
        root.state = .active
        root.translatesAutoresizingMaskIntoConstraints = false

        previewView = PreviewView()
        previewView.translatesAutoresizingMaskIntoConstraints = false

        detailLabel = NSTextField(labelWithString: "Idle — no controlled app")
        detailLabel.font = .systemFont(ofSize: 12, weight: .medium)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byTruncatingMiddle
        detailLabel.translatesAutoresizingMaskIntoConstraints = false

        hideButton = NSButton(title: "Hide", target: self, action: #selector(togglePictureInPicture))
        hideButton.bezelStyle = .rounded
        hideButton.toolTip = "Hide Picture in Picture"
        let refreshButton = NSButton(title: "Refresh", target: self, action: #selector(refresh))
        refreshButton.bezelStyle = .rounded
        refreshButton.toolTip = "Reload the current macos-cua state"
        let endButton = NSButton(title: "End", target: self, action: #selector(endSession))
        endButton.bezelStyle = .rounded
        endButton.toolTip = "End the controlled session and keep the service ready"
        let controls = NSStackView(views: [detailLabel, hideButton, refreshButton, endButton])
        controls.orientation = .horizontal
        controls.alignment = .centerY
        controls.spacing = 8
        controls.translatesAutoresizingMaskIntoConstraints = false
        detailLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        root.addSubview(previewView)
        root.addSubview(controls)
        panel.contentView = root
        NSLayoutConstraint.activate([
            previewView.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 12),
            previewView.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -12),
            previewView.topAnchor.constraint(equalTo: root.topAnchor, constant: 12),
            previewView.bottomAnchor.constraint(equalTo: controls.topAnchor, constant: -10),
            controls.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            controls.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -14),
            controls.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -10),
            controls.heightAnchor.constraint(equalToConstant: 28),
        ])
        placePanel()
    }

    private func configureCursorOverlay() {
        cursorOverlayPanel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 210, height: 90),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        cursorOverlayPanel.level = .screenSaver
        cursorOverlayPanel.backgroundColor = .clear
        cursorOverlayPanel.isOpaque = false
        cursorOverlayPanel.hasShadow = false
        cursorOverlayPanel.ignoresMouseEvents = true
        cursorOverlayPanel.hidesOnDeactivate = false
        cursorOverlayPanel.isReleasedWhenClosed = false
        cursorOverlayPanel.collectionBehavior = [
            .canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle
        ]
        cursorOverlayView = CursorOverlayView(frame: cursorOverlayPanel.contentView?.bounds ?? .zero)
        cursorOverlayView.autoresizingMask = [.width, .height]
        cursorOverlayPanel.contentView = cursorOverlayView
    }

    private func quartzWindowBounds(windowID: Int) -> CGRect? {
        guard let rows = CGWindowListCopyWindowInfo(
            [.optionIncludingWindow], CGWindowID(windowID)
        ) as? [[String: Any]],
        let row = rows.first,
        let rawBounds = row[kCGWindowBounds as String] as? [String: Any],
        let x = (rawBounds["X"] as? NSNumber)?.doubleValue,
        let y = (rawBounds["Y"] as? NSNumber)?.doubleValue,
        let width = (rawBounds["Width"] as? NSNumber)?.doubleValue,
        let height = (rawBounds["Height"] as? NSNumber)?.doubleValue else {
            return nil
        }
        return CGRect(x: x, y: y, width: width, height: height)
    }

    private func appKitPoint(fromQuartz point: CGPoint) -> NSPoint? {
        let screenNumberKey = NSDeviceDescriptionKey("NSScreenNumber")
        for screen in NSScreen.screens {
            guard let number = screen.deviceDescription[screenNumberKey] as? NSNumber else {
                continue
            }
            let quartzFrame = CGDisplayBounds(CGDirectDisplayID(number.uint32Value))
            guard quartzFrame.contains(point) else { continue }
            return NSPoint(
                x: screen.frame.minX + point.x - quartzFrame.minX,
                y: screen.frame.maxY - (point.y - quartzFrame.minY)
            )
        }
        return nil
    }

    private func renderCursorOverlay(_ state: OperatorState, active: Bool) {
        guard active,
              state.cursorVisible ?? false,
              let cursorX = state.cursorX,
              let cursorY = state.cursorY,
              let cursorUpdateID = state.cursorUpdateID else {
            cursorAnimationTimer?.invalidate()
            cursorAnimationTimer = nil
            cursorOverlayPanel.orderOut(nil)
            return
        }
        let quartzTarget: CGPoint
        if let screenX = state.cursorScreenX, let screenY = state.cursorScreenY {
            quartzTarget = CGPoint(x: screenX, y: screenY)
        } else {
            guard let windowID = state.windowID,
                  let bounds = quartzWindowBounds(windowID: windowID) else {
                cursorOverlayPanel.orderOut(nil)
                return
            }
            quartzTarget = CGPoint(
                x: bounds.minX + min(1, max(0, cursorX)) * bounds.width,
                y: bounds.minY + min(1, max(0, cursorY)) * bounds.height
            )
        }
        guard let target = appKitPoint(fromQuartz: quartzTarget) else {
            cursorOverlayPanel.orderOut(nil)
            return
        }

        cursorOverlayView.render(
            cursorImagePath: state.cursorImagePath,
            harness: state.harness ?? "Agent"
        )
        let origin = NSPoint(x: target.x - 15, y: target.y - 76)
        let wasVisible = cursorOverlayPanel.isVisible
        if !wasVisible {
            cursorOverlayPanel.setFrameOrigin(origin)
            persistRenderedCursorPosition(x: cursorX, y: cursorY, updateID: cursorUpdateID)
        }
        cursorOverlayPanel.orderFrontRegardless()
        if wasVisible {
            moveCursorOverlay(
                to: origin,
                cursorX: cursorX,
                cursorY: cursorY,
                updateID: cursorUpdateID,
                durationMS: state.cursorDurationMS
            )
        }
    }

    private func moveCursorOverlay(
        to origin: NSPoint,
        cursorX: Double,
        cursorY: Double,
        updateID: String,
        durationMS: Double?
    ) {
        cursorAnimationTimer?.invalidate()
        let current = cursorOverlayPanel.frame.origin
        if abs(current.x - origin.x) < 0.5 && abs(current.y - origin.y) < 0.5 {
            cursorOverlayPanel.setFrameOrigin(origin)
            persistRenderedCursorPosition(x: cursorX, y: cursorY, updateID: updateID)
            return
        }
        cursorAnimationStart = current
        cursorAnimationTarget = origin
        cursorAnimationTargetX = cursorX
        cursorAnimationTargetY = cursorY
        cursorAnimationTargetUpdateID = updateID
        cursorAnimationDuration = min(5, max(0.08, (durationMS ?? 120) / 1000))
        cursorAnimationStartedAt = ProcessInfo.processInfo.systemUptime
        let timer = Timer(
            timeInterval: 1.0 / 60.0,
            target: self,
            selector: #selector(cursorAnimationTick(_:)),
            userInfo: nil,
            repeats: true
        )
        cursorAnimationTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    @objc private func cursorAnimationTick(_ timer: Timer) {
        let elapsed = ProcessInfo.processInfo.systemUptime - cursorAnimationStartedAt
        let progress = min(1, max(0, elapsed / cursorAnimationDuration))
        let eased = 1 - pow(1 - progress, 3)
        let origin = NSPoint(
            x: cursorAnimationStart.x
                + (cursorAnimationTarget.x - cursorAnimationStart.x) * eased,
            y: cursorAnimationStart.y
                + (cursorAnimationTarget.y - cursorAnimationStart.y) * eased
        )
        cursorOverlayPanel.setFrameOrigin(origin)
        if progress >= 1 {
            timer.invalidate()
            cursorAnimationTimer = nil
            cursorOverlayPanel.setFrameOrigin(cursorAnimationTarget)
            persistRenderedCursorPosition(
                x: cursorAnimationTargetX,
                y: cursorAnimationTargetY,
                updateID: cursorAnimationTargetUpdateID
            )
        }
    }

    private func persistRenderedCursorPosition(x: Double, y: Double, updateID: String) {
        let lockPath = stateURL.path + ".lock"
        let lockDescriptor = open(lockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard lockDescriptor >= 0 else { return }
        guard flock(lockDescriptor, LOCK_EX) == 0 else {
            close(lockDescriptor)
            return
        }
        defer {
            flock(lockDescriptor, LOCK_UN)
            close(lockDescriptor)
        }
        guard let data = try? Data(contentsOf: stateURL),
              var object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["cursor_update_id"] as? String == updateID
        else { return }
        let renderedX = object["cursor_rendered_x"] as? Double
        let renderedY = object["cursor_rendered_y"] as? Double
        let renderedUpdateID = object["cursor_rendered_update_id"] as? String
        if let renderedX, let renderedY,
           abs(renderedX - x) < 0.0001,
           abs(renderedY - y) < 0.0001,
           renderedUpdateID == updateID {
            return
        }
        object["cursor_rendered_x"] = x
        object["cursor_rendered_y"] = y
        object["cursor_rendered_update_id"] = updateID
        if let updated = try? JSONSerialization.data(
            withJSONObject: object, options: [.prettyPrinted, .sortedKeys]
        ) {
            try? updated.write(to: stateURL, options: .atomic)
        }
    }

    private func placePanel() {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let visible = screen.visibleFrame
        let frame = panel.frame
        panel.setFrameOrigin(NSPoint(
            x: visible.maxX - frame.width - 24,
            y: visible.maxY - frame.height - 24
        ))
    }

    @objc private func pollState() {
        reloadState(force: false)
        followLiveWindowIfNeeded()
    }

    private func followLiveWindowIfNeeded() {
        let state = currentState
        guard state.cursorVisible == true,
              state.cursorScreenX == nil,
              state.cursorX != nil,
              cursorAnimationTimer == nil else { return }
        renderCursorOverlay(state, active: state.active ?? false)
    }

    @objc private func refresh() {
        reloadState(force: true)
    }

    private func reloadState(force: Bool) {
        guard let data = try? Data(contentsOf: stateURL) else {
            render(OperatorState(active: false, status: "idle", message: "Waiting for a macos-cua session"))
            return
        }
        if !force && data == lastData { return }
        lastData = data
        guard let state = try? JSONDecoder().decode(OperatorState.self, from: data) else {
            render(OperatorState(active: false, status: "error", message: "Invalid operator state"))
            return
        }
        currentState = state
        render(state)
    }

    private func render(_ state: OperatorState) {
        if let requestedVisibility = state.pipVisible {
            pipVisible = requestedVisibility
            UserDefaults.standard.set(pipVisible, forKey: "pipVisible")
        }
        let active = state.active ?? false
        let app = state.app?.trimmingCharacters(in: .whitespacesAndNewlines)
        let appName = (app?.isEmpty == false ? app! : "None")
        let status = (state.status ?? (active ? "active" : "idle")).capitalized
        let harness = state.harness ?? "Unknown"
        let window = state.windowTitle?.isEmpty == false
            ? state.windowTitle!
            : state.windowID.map(String.init) ?? "None"

        if let button = statusItem.button {
            let symbol = active ? "cursorarrow.motionlines" : "cursorarrow"
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "macos-cua \(status)")
            button.title = active ? " \(appName)" : ""
            button.contentTintColor = active ? .controlAccentColor : .secondaryLabelColor
            button.toolTip = "macos-cua — \(status) — \(appName) via \(harness)"
        }

        appItem.title = "Controlled app: \(appName)"
        windowItem.title = "Window: \(window)"
        harnessItem.title = "Harness: \(harness)"
        stateItem.title = "Status: \(status)"
        panel.title = "macos-cua — \(appName)"
        detailLabel.stringValue = "\(status) • \(appName) • \(harness)"

        if let path = state.screenshotPath,
           FileManager.default.fileExists(atPath: path),
           let image = NSImage(contentsOfFile: path) {
            previewView.render(
                image: image,
                cursorX: state.cursorX,
                cursorY: state.cursorY,
                cursorVisible: state.cursorVisible ?? false,
                cursorImagePath: state.cursorImagePath
            )
        } else {
            let placeholder = NSImage(
                systemSymbolName: "rectangle.inset.filled.and.cursorarrow",
                accessibilityDescription: "Waiting for app preview"
            )
            previewView.render(
                image: placeholder,
                cursorX: nil,
                cursorY: nil,
                cursorVisible: false,
                cursorImagePath: nil
            )
        }

        if active && pipVisible {
            panel.orderFrontRegardless()
        } else {
            panel.orderOut(nil)
        }
        renderCursorOverlay(state, active: active)
        toggleItem.title = pipVisible ? "Hide Picture in Picture" : "Show Picture in Picture"
        hideButton.title = pipVisible ? "Hide" : "Show"
    }

    @objc private func togglePictureInPicture() {
        pipVisible.toggle()
        UserDefaults.standard.set(pipVisible, forKey: "pipVisible")
        persistPictureInPictureVisibility()
        render(currentState)
    }

    private func persistPictureInPictureVisibility() {
        guard let data = try? Data(contentsOf: stateURL),
              var object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        object["pip_visible"] = pipVisible
        object["updated_at"] = ISO8601DateFormatter().string(from: Date())
        if let updated = try? JSONSerialization.data(
            withJSONObject: object, options: [.prettyPrinted, .sortedKeys]
        ) {
            try? updated.write(to: stateURL, options: .atomic)
            currentState.pipVisible = pipVisible
        }
    }

    @objc private func endSession() {
        guard let data = try? Data(contentsOf: stateURL),
              var object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        object["active"] = false
        object["status"] = "idle"
        object["message"] = "Ended from native operator"
        object["cursor_visible"] = false
        object["updated_at"] = ISO8601DateFormatter().string(from: Date())
        if let updated = try? JSONSerialization.data(
            withJSONObject: object, options: [.prettyPrinted, .sortedKeys]
        ) {
            try? updated.write(to: stateURL, options: .atomic)
            reloadState(force: true)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        pipVisible = false
        UserDefaults.standard.set(false, forKey: "pipVisible")
        persistPictureInPictureVisibility()
        render(currentState)
        return false
    }
}
