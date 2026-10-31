import AppKit
import SwiftUI

/// Fixed 640×320 transparent, non-activating panel pinned to the top centre of
/// the menu-bar display (spec §1). Only the black shape morphs inside it.
///
/// The panel ignores the mouse except while the pointer is over a real hit
/// rect (shape, app circles, agent circle) — so the large
/// transparent window never swallows menu-bar or desktop clicks, and it can
/// never become key or main (clicks never take focus).
@MainActor
final class SamanthaMenuBarIslandPanel {
    /// Pointer source for hover and hit testing; tests park it off-screen so a real pointer resting
    /// over the island can't open or pin cards mid-test.
    nonisolated(unsafe) static var pointerLocation: () -> NSPoint = { NSEvent.mouseLocation }

    private var panel: IslandPanel?
    private weak var model: IslandModel?
    private var screenObserver: NSObjectProtocol?
    private var trackingWindows: [IslandTrackingPanel] = []
    private var trackedRects: [CGRect] = []
    private var insideShape = false
    private var leaveTask: Task<Void, Never>?
    private var hoverTask: Task<Void, Never>?
    private var envTimer: Timer?
    /// The controller wants the island up; environment rules (fullscreen, Mission Control) may still hide it.
    private var wantsVisible = false
    private(set) var envHidden = false
    private var fullscreenStreak = 0

    // Settings (General), pushed by applySettings.
    private(set) var expandOnHover = false
    private(set) var hoverDelay: TimeInterval = 0
    private var hideInFullscreen = true
    private var hideInMissionControl = true
    private var hideFromCapture = false

    /// Card closes this long after the pointer leaves it: enough grace to cross a seam between the
    /// card and an app circle without collapsing, still well under 0.5 s.
    static let leaveDelay: TimeInterval = IslandModel.leaveGrace

    /// Leave watchdog period. Runs only while a card or peek is up (zero cost when compact).
    static let watchInterval: TimeInterval = 0.1
    /// Tracking areas report crossings only, and AppKit drops an exit when the areas or tracking
    /// windows are rebuilt under the pointer (every card resize). While expanded, the watchdog polls
    /// `NSEvent.mouseLocation` (screen coordinates, same space as `NSWindow.frame`) and reconciles.
    /// https://developer.apple.com/documentation/appkit/nsevent/mouselocation
    private var watchTimer: Timer?
    private var leaveWatch = IslandLeaveWatch()

    /// App that was frontmost when text entry began (key goes back to it after submit / Escape).
    private var keyReturnApp: NSRunningApplication?
    /// Keys 1–6 / Return / Shift-Return / Esc while the choice card is key (our own window only).
    private var keyMonitor: Any?
    private var resignObserver: Any?
    /// A peek is up: keep the island out of screen captures / shares (public NSWindow.sharingType).
    private var peekPrivate = false

    func attach(model: IslandModel) {
        self.model = model
        model.onBeginTextEntry = { [weak self] in _ = self?.beginTextEntry() }
        model.onEndTextEntry = { [weak self] in self?.endTextEntry() }
        // ⌃⌘I keyboard focus reuses the same key-window path (non-activating; focus handed back on end).
        model.onBeginKeyboard = { [weak self] in self?.beginTextEntry() ?? false }
        model.onEndKeyboard = { [weak self] in
            guard let self, self.model?.textEntryActive != true else { return }
            self.endTextEntry()
        }
        model.onGeometryChange = { [weak self] in self?.updateTrackingGeometry(); self?.syncLeaveWatch() }
        model.onPeekVisible = { [weak self] on in
            self?.peekPrivate = on; self?.applyCaptureSharing(); self?.syncLeaveWatch()
        }
        resignObserver = NotificationCenter.default.addObserver(forName: NSWindow.didResignKeyNotification, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated {
                guard let self, let panel = self.panel, (n.object as AnyObject?) === panel else { return }
                // Focus left the island (click elsewhere): tear down BOTH key paths — never strand a monitor.
                self.model?.endKeyboard(close: false)
                self.model?.endTextEntry()
                self.endTextEntry()
            }
        }
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.panel?.isVisible == true else { return }
                self.reposition(on: Self.menuBarScreen())
            }
        }
    }

    deinit {
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
    }

    func show(on screen: NSScreen? = nil) {
        guard let model else { return }
        let target = screen ?? Self.menuBarScreen()
        if panel == nil { panel = makePanel(model: model) }
        wantsVisible = true
        reposition(on: target)
        applyCaptureSharing()
        startEnvTimer()
        evaluateEnvironment()
        if !envHidden {
            panel?.orderFrontRegardless()
            startPointerTracking()
        }
    }

    // MARK: Key window (choice text field only)

    /// Only reached from the text field's click handler via `IslandModel.beginTextEntry`.
    @discardableResult
    private func beginTextEntry() -> Bool {
        guard let panel, panel.isVisible else { return false }
        let front = NSWorkspace.shared.frontmostApplication
        keyReturnApp = front?.processIdentifier == ProcessInfo.processInfo.processIdentifier ? nil : front
        panel.allowKey = true
        panel.ignoresMouseEvents = false
        panel.makeKey()   // non-activating panel: the frontmost app stays active
        if keyMonitor == nil {
            keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
                let code = e.keyCode, chars = e.charactersIgnoringModifiers ?? "", f = e.modifierFlags
                let win = e.window.map(ObjectIdentifier.init)
                let used: Bool = MainActor.assumeIsolated {
                    guard let self, let model = self.model, let panel = self.panel, panel.isKeyWindow,
                          win == ObjectIdentifier(panel) else { return false }
                    return model.handleKey(keyCode: code, characters: chars, shift: f.contains(.shift),
                                           command: f.contains(.command), control: f.contains(.control))
                }
                return used ? nil : e
            }
        }
        return true
    }

    private func endTextEntry() {
        guard let panel else { return }
        if let m = keyMonitor { NSEvent.removeMonitor(m); keyMonitor = nil }
        let wasKey = panel.isKeyWindow
        panel.allowKey = false
        if wasKey { panel.resignKey() }
        if wasKey, let app = keyReturnApp, !app.isTerminated { app.activate() }  // give key back
        keyReturnApp = nil
    }

    /// File tray Share… — system picker anchored under the island (AirDrop, Mail, Messages…).
    func share(_ urls: [URL]) {
        guard !urls.isEmpty, let view = panel?.contentView, let model else { return }
        let r = model.shapeRect
        let anchor = NSRect(x: r.midX - 1, y: view.bounds.height - r.maxY, width: 2, height: 2)
        NSSharingServicePicker(items: urls).show(relativeTo: anchor, of: view, preferredEdge: .minY)
    }

    /// Test hooks.
    var debugCanBecomeKey: Bool { panel?.canBecomeKey ?? false }
    var debugIsKey: Bool { panel?.isKeyWindow ?? false }
    func debugEnsurePanel() { if panel == nil, let model { panel = makePanel(model: model) } }

    /// Test hooks: the one panel instance, and whether the island is meant to be up.
    var panelID: ObjectIdentifier? { panel.map(ObjectIdentifier.init) }
    var isShown: Bool { wantsVisible }

    func hide() {
        CredentialEntryController.shared.cancelActive()
        model?.endTextEntry()
        model?.endKeyboard(close: false)
        if let m = keyMonitor { NSEvent.removeMonitor(m); keyMonitor = nil }
        wantsVisible = false
        panel?.orderOut(nil)
        stopPointerTracking()
        envTimer?.invalidate()
        envTimer = nil
    }

    // MARK: Settings

    func applySettings(_ s: IslandSettingsStore) {
        typealias K = IslandSettingsSchema
        expandOnHover = s.string(K.openOn) == "hover"
        hoverDelay = s.double(K.hoverDuration)
        hideInFullscreen = s.bool(K.hideInFullscreen)
        hideInMissionControl = s.bool(K.hideInMissionControl)
        hideFromCapture = s.bool(K.hideFromScreenCapture)
        applyCaptureSharing()
        if wantsVisible {
            reposition(on: Self.menuBarScreen())
            evaluateEnvironment()
        }
    }

    func updateCapturePrivacy() { applyCaptureSharing() }

    private func applyCaptureSharing() {
        panel?.sharingType = hideFromCapture || peekPrivate || model?.credentialRequest != nil ? .none : .readOnly
    }

    private func startEnvTimer() {
        guard envTimer == nil else { return }
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.evaluateEnvironment() }
        }
        RunLoop.main.add(t, forMode: .common)
        envTimer = t
    }

    /// 1 s tick: hide in fullscreen spaces / Mission Control when those settings are on.
    /// Fullscreen needs two consecutive hits so a menubar redraw never blinks the island.
    private func evaluateEnvironment() {
        guard wantsVisible, let panel else { return }
        let screen = Self.menuBarScreen()
        let fs = hideInFullscreen && IslandEnvironment.fullscreenActive(on: screen)
        fullscreenStreak = fs ? fullscreenStreak + 1 : 0
        let mc = hideInMissionControl && IslandEnvironment.missionControlActive()
        let hidden = fullscreenStreak >= 2 || mc
        if hidden == envHidden && panel.isVisible == !hidden { return }
        if hidden != envHidden {
            SamanthaActivityLog.startup(phase: hidden ? "island_env_hidden" : "island_env_shown", status: "ok",
                                        detail: hidden ? (mc ? "mission_control" : "fullscreen") : "")
        }
        envHidden = hidden
        if hidden {
            panel.orderOut(nil)
            stopPointerTracking()
        } else {
            reposition(on: screen)
            panel.orderFrontRegardless()
            startPointerTracking()
        }
    }

    /// Kept for callers; hit-testing is now pointer-driven.
    func setInteractive(_ on: Bool) {}

    func reposition(on screen: NSScreen? = nil) {
        guard let panel else { return }
        let target = screen ?? Self.menuBarScreen()
        let bar = MenuBarWindowAnchor.strip(on: target)?.appKit
            ?? CGRect(x: target.frame.minX, y: target.frame.maxY - 30, width: target.frame.width, height: 30)
        let frame = IslandBarLayout.windowFrame(bar: bar, screen: target.frame)
        if panel.frame != frame { panel.setFrame(frame, display: true); updateTrackingGeometry(force: true) }
        panel.level = Self.menuBarPanelLevel
    }

    /// Island screen per Settings → Display on (main/menu-bar display by default).
    static func menuBarScreen() -> NSScreen {
        let main = MenuBarWindowAnchor.menuBarScreen()
        let s = IslandSettingsStore.shared
        return IslandDisplayChoice.pick(choice: s.string(IslandSettingsSchema.displayOn), screens: NSScreen.screens,
                                        selectedUUID: s.defaults.string(forKey: IslandSettingsSchema.selectedDisplay), main: main)
    }

    static func menuBarContentHeight(on screen: NSScreen) -> CGFloat {
        if let strip = MenuBarWindowAnchor.strip(on: screen) { return strip.appKit.height }
        let measured = screen.frame.maxY - screen.visibleFrame.maxY
        return measured > 4 ? measured : NSStatusBar.system.thickness
    }

    // MARK: Pointer

    private func startPointerTracking() {
        updateTrackingGeometry(force: true)
    }

    private func stopPointerTracking() {
        for child in trackingWindows { panel?.removeChildWindow(child); child.orderOut(nil) }
        trackingWindows.removeAll()
        trackedRects = []
        hoverTask?.cancel()
        hoverTask = nil
        leaveTask?.cancel()
        leaveTask = nil
        panel?.ignoresMouseEvents = true
        model?.hovering = false
        insideShape = false
        syncLeaveWatch()
    }

    private func updateTrackingGeometry(force: Bool = false) {
        guard let panel, let model, panel.isVisible else { return }
        let rects = model.hitRects
        guard force || rects != trackedRects else { return }
        trackedRects = rects
        for child in trackingWindows { panel.removeChildWindow(child); child.orderOut(nil) }
        trackingWindows.removeAll()
        if let host = panel.contentView as? IslandHostingView<SamanthaIslandBar> {
            host.islandTrackingRects = rects
            host.onPointerChange = { [weak self] in self?.pointerChanged() }
            host.updateTrackingAreas()
        }
        for rect in rects where !rect.isEmpty {
            let frame = CGRect(x: panel.frame.minX + rect.minX, y: panel.frame.maxY - rect.maxY,
                               width: rect.width, height: rect.height)
            let child = IslandTrackingPanel(contentRect: frame, styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
            child.isOpaque = false; child.backgroundColor = .clear; child.hasShadow = false
            child.level = panel.level
            child.hidesOnDeactivate = false
            child.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
            let view = IslandTrackingView(frame: CGRect(origin: .zero, size: frame.size))
            view.onPointerChange = { [weak self] in self?.pointerChanged() }
            view.registerForDraggedTypes([.fileURL])
            view.onDragEnter = { [weak self, weak model] in
                guard let self, let model, model.trayEnabled else { return false }
                self.holdTask?.cancel()
                self.holdTask = Task { @MainActor [weak model] in
                    try? await Task.sleep(nanoseconds: UInt64(Self.dropHold * 1_000_000_000))
                    guard !Task.isCancelled else { return }
                    model?.setTrayTargeted(true)
                }
                return true
            }
            view.onDragExit = { [weak self, weak model] in
                self?.holdTask?.cancel()
                self?.holdTask = nil
                model?.setTrayTargeted(false)
            }
            view.onDrop = { [weak self, weak model] urls in
                self?.holdTask?.cancel()
                guard let model, model.trayEnabled else { return false }
                model.addToTray(urls)
                model.setTrayTargeted(false)
                return true
            }
            view.onMouseEvent = { [weak panel] event in
                guard let panel, let forwarded = NSEvent.mouseEvent(with: event.type,
                    location: panel.convertPoint(fromScreen: NSEvent.mouseLocation), modifierFlags: event.modifierFlags,
                    timestamp: event.timestamp, windowNumber: panel.windowNumber, context: nil,
                    eventNumber: event.eventNumber, clickCount: event.clickCount, pressure: event.pressure) else { return }
                panel.sendEvent(forwarded)
            }
            child.contentView = view
            panel.addChildWindow(child, ordered: .below)
            child.order(.below, relativeTo: panel.windowNumber)
            trackingWindows.append(child)
        }
        // Tracking areas only fire on crossings: a card that closes under a stationary pointer must
        // still drop click capture and hover, so reconcile against the pointer now.
        pointerChanged()
    }

    /// Screen point → window coords (top-left origin), the space IslandBarLayout uses.
    nonisolated static func windowPoint(screen p: CGPoint, frame: CGRect) -> CGPoint {
        CGPoint(x: p.x - frame.minX, y: frame.maxY - p.y)
    }

    // MARK: File-tray drag (A9): drag near the pill + 200 ms hold → drop target

    private var dragPBCount = NSPasteboard(name: .drag).changeCount
    private var mouseWasDown = false
    private var fileDragActive = false
    private var holdTask: Task<Void, Never>?
    static let dropHold: TimeInterval = 0.2

    private func trackFileDrag(_ p: CGPoint, model: IslandModel) -> Bool {
        let down = NSEvent.pressedMouseButtons & 1 != 0
        let pb = NSPasteboard(name: .drag)
        if down && !mouseWasDown { dragPBCount = pb.changeCount }
        if down, model.trayEnabled, pb.changeCount != dragPBCount, !IslandHostingView<SamanthaIslandBar>.fileURLs(pb).isEmpty {
            fileDragActive = true
        }
        if !down {
            fileDragActive = false
            holdTask?.cancel()
            holdTask = nil
            if model.trayTargeted { model.setTrayTargeted(false) }
        }
        mouseWasDown = down
        guard fileDragActive else { return false }
        let near = IslandTrayRules.nearPill(p, shape: model.shapeRect)
        if near && !model.trayTargeted && holdTask == nil {
            holdTask = Task { @MainActor [weak self, weak model] in
                try? await Task.sleep(nanoseconds: UInt64(Self.dropHold * 1_000_000_000))
                guard !Task.isCancelled else { return }
                model?.setTrayTargeted(true)
                self?.holdTask = nil
            }
        } else if !near {
            holdTask?.cancel()
            holdTask = nil
            if model.trayTargeted { model.setTrayTargeted(false) }
        }
        return near
    }

    private func dragOverIsland(_ on: Bool) {
        guard let model else { return }
        if !on, !fileDragActive { model.setTrayTargeted(false) }
    }

    private func pointerChanged() {
        guard let panel, panel.isVisible else { return }
        applyPointer(Self.windowPoint(screen: Self.pointerLocation(), frame: panel.frame))
    }

    /// Pointer at `p` (window coords): hover, click capture and the leave timer. `via` tags the log.
    func applyPointer(_ p: CGPoint, via: String = "exit") {
        guard let model else { return }
        let dragNear = trackFileDrag(p, model: model)
        let overAny = dragNear || model.hitRects.contains { $0.contains(p) }
        if let panel, panel.ignoresMouseEvents == overAny { panel.ignoresMouseEvents = !overAny }
        // Hover follows the same whole-pill target as clicks (incl. the band above it).
        let overShape = !model.idleHidden && IslandBarLayout.pillTarget(shape: model.shapeRect).contains(p)
        if overShape != model.hovering { model.hovering = overShape }
        // Settings → Expand on hover (+ Hover duration): open the card after the delay.
        if expandOnHover, overShape, !model.cardOpen {
            if hoverTask == nil {
                let delay = hoverDelay
                hoverTask = Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: UInt64(max(0, delay) * 1_000_000_000))
                    guard let self, !Task.isCancelled else { return }
                    if self.model?.hovering == true, self.model?.cardOpen == false { self.model?.hoverOpen() }
                }
            }
        } else if !overShape {
            hoverTask?.cancel()
            hoverTask = nil
        } else if model.cardOpen {
            hoverTask = nil
        }
        // Leave timer keys off every hit rect, so moving onto a circle keeps the card open.
        if overAny {
            leaveTask?.cancel()
            leaveTask = nil
        } else if insideShape {
            leaveTask?.cancel()
            // The watchdog sees a lost exit up to one period late: take that off the grace.
            let grace = via == "watchdog" ? Self.leaveDelay - Self.watchInterval : Self.leaveDelay
            leaveTask = Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(grace * 1_000_000_000))
                guard let self, !Task.isCancelled else { return }
                // Tracking areas only report crossings: re-check the real pointer so a missed
                // re-enter (geometry rebuilt under the pointer) never collapses a hovered card.
                self.leaveTask = nil
                if self.pointerOverHitRects() { self.insideShape = true; return }
                if self.model?.cardOpen == true { IslandEventLog.log("pointer-left", ["via": via]) }
                self.model?.pointerLeft()
            }
        }
        insideShape = overAny
    }

    // MARK: Leave watchdog

    /// Timer on while a card or peek is up on screen; off (and disarmed) when compact or hidden.
    private func syncLeaveWatch() {
        let need = panel?.isVisible == true && !envHidden && (model?.cardOpen == true || model?.peek != nil)
        if need {
            guard watchTimer == nil else { return }
            leaveWatch.start(armed: insideShape)
            let t = Timer(timeInterval: Self.watchInterval, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.watchTick() }
            }
            RunLoop.main.add(t, forMode: .common)
            watchTimer = t
        } else if let t = watchTimer {
            t.invalidate()
            watchTimer = nil
            leaveWatch.reset()
        }
    }

    private func watchTick() {
        guard let panel, panel.isVisible else { syncLeaveWatch(); return }
        watchStep(Self.windowPoint(screen: Self.pointerLocation(), frame: panel.frame), now: Date())
    }

    /// One watchdog step against the real pointer `p` (window coords); tests drive it directly.
    /// 1. Reconcile: the pointer is not where the last tracking event left us (a lost exit or
    ///    enter) → run the normal pointer path, which starts the usual `leaveDelay` grace.
    /// 2. Backstop: the pointer has been off the island past the grace with no leave pending
    ///    (timer lost to a rebuild) → leave now.
    func watchStep(_ p: CGPoint, now: Date) {
        guard let model else { return }
        let over = model.hitRects.contains { $0.contains(p) }
        let overShape = !model.idleHidden && IslandBarLayout.pillTarget(shape: model.shapeRect).contains(p)
        if over != insideShape || overShape != model.hovering {
            if !over && insideShape { IslandEventLog.log("exit-lost", ["via": "watchdog"]) }
            applyPointer(p, via: "watchdog")
        }
        let active = model.cardOpen || model.peek != nil
        guard let out = leaveWatch.observe(over: over, active: active, now: now),
              out >= Self.leaveDelay + Self.watchInterval, leaveTask == nil else { return }
        leaveWatch.reset()
        if model.hovering { model.hovering = false }   // peek: starts its short dismiss
        if model.cardOpen {
            IslandEventLog.log("pointer-left", ["via": "backstop"])
            model.pointerLeft()
        }
    }

    /// Test hooks.
    var debugInsideShape: Bool { insideShape }
    var debugLeavePending: Bool { leaveTask != nil }
    var debugWatchRunning: Bool { watchTimer != nil }

    private func pointerOverHitRects() -> Bool {
        guard let panel, let model, panel.isVisible else { return false }
        let p = Self.windowPoint(screen: Self.pointerLocation(), frame: panel.frame)
        return model.hitRects.contains { $0.contains(p) }
    }

    /// Same band as Window Server Menubar (24) + 1 so the island paints over the strip.
    private static var menuBarPanelLevel: NSWindow.Level {
        NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.mainMenuWindow)) + 1)
    }

    private func makePanel(model: IslandModel) -> IslandPanel {
        let size = IslandBarLayout.windowSize
        let host = IslandHostingView(rootView: SamanthaIslandBar(model: model))
        host.frame = NSRect(origin: .zero, size: size)
        host.registerForDraggedTypes([.fileURL])
        host.onScroll = { [weak model] e in
            guard let model else { return }
            // Momentum events never trigger gestures.
            guard e.momentumPhase.isEmpty else { return }
            model.handleScroll(dx: e.scrollingDeltaX, dy: e.scrollingDeltaY, precise: e.hasPreciseScrollingDeltas,
                               began: e.phase.contains(.began), ended: e.phase.contains(.ended) || e.phase.contains(.cancelled))
        }
        host.onDragState = { [weak self] on in self?.dragOverIsland(on) }
        host.onDrop = { [weak model] urls in
            guard let model, model.trayEnabled else { return false }
            model.addToTray(urls)
            return true
        }
        host.autoresizingMask = [.width, .height]
        let panel = IslandPanel(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        panel.onMouseDown = { [weak model] in
            guard let model, model.credentialRequest != nil else { return }
            model.beginTextEntry(focusField: false)
        }
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.isFloatingPanel = false
        panel.level = Self.menuBarPanelLevel
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.ignoresMouseEvents = true
        panel.contentView = host
        return panel
    }
}

/// Pointer-leave bookkeeping for the watchdog (pure; unit-tested).
/// Armed once the pointer has been over the island while expanded; disarmed when compact.
struct IslandLeaveWatch: Equatable {
    private(set) var armed = false
    private(set) var outsideSince: Date?

    mutating func start(armed: Bool) { self.armed = armed; outsideSince = nil }
    mutating func reset() { armed = false; outsideSince = nil }

    /// Seconds the pointer has been off the island since it was last seen on it; nil when inside,
    /// unarmed (never hovered: keyboard- or alert-opened cards keep their own rules) or inactive.
    mutating func observe(over: Bool, active: Bool, now: Date) -> TimeInterval? {
        guard active else { reset(); return nil }
        if over { armed = true; outsideSince = nil; return nil }
        guard armed else { return nil }
        let since = outsideSince ?? now
        outsideSince = since
        return now.timeIntervalSince(since)
    }
}

/// Small tracking windows receive the first enter while the main panel is click-through.
final class IslandTrackingPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class IslandTrackingView: NSView {
    var onPointerChange: (() -> Void)?
    var onMouseEvent: ((NSEvent) -> Void)?
    var onDragEnter: (() -> Bool)?
    var onDragExit: (() -> Void)?
    var onDrop: (([URL]) -> Bool)?
    private var area: NSTrackingArea?
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let area { removeTrackingArea(area) }
        // https://developer.apple.com/documentation/appkit/nstrackingarea
        // .activeAlways: fires while ACU is not frontmost. .inVisibleRect: AppKit keeps the area equal
        // to the view's visible rect, so a resized tracking window can't keep a stale rect.
        // https://developer.apple.com/documentation/appkit/nstrackingarea/options-swift.struct/invisiblerect
        let next = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(next)
        area = next
    }
    override func mouseEntered(with event: NSEvent) { onPointerChange?() }
    override func mouseExited(with event: NSEvent) { onPointerChange?() }
    override func mouseDown(with event: NSEvent) { onPointerChange?(); onMouseEvent?(event) }
    override func mouseUp(with event: NSEvent) { onMouseEvent?(event) }
    override func mouseDragged(with event: NSEvent) { onMouseEvent?(event) }
    override func rightMouseDown(with event: NSEvent) { onPointerChange?(); onMouseEvent?(event) }
    override func rightMouseUp(with event: NSEvent) { onMouseEvent?(event) }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard !IslandHostingView<SamanthaIslandBar>.fileURLs(sender.draggingPasteboard).isEmpty,
              onDragEnter?() == true else { return [] }
        return .link
    }
    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { .link }
    override func draggingExited(_ sender: NSDraggingInfo?) { onDragExit?() }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        onDrop?(IslandHostingView<SamanthaIslandBar>.fileURLs(sender.draggingPasteboard)) ?? false
    }
}

/// Never main, and key ONLY while the user is typing into a choice card's text field (after their click);
/// clicking an app circle activates that app, not us.
final class IslandPanel: NSPanel {
    /// Set by `SamanthaMenuBarIslandPanel.beginTextEntry` (a click into the field) and cleared on
    /// submit / Escape / card change. Nothing else may set it.
    var allowKey = false
    var onMouseDown: (() -> Void)?
    override func sendEvent(_ event: NSEvent) {
        if event.type == .leftMouseDown { onMouseDown?() }
        super.sendEvent(event)
    }
    override var canBecomeKey: Bool { allowKey }
    override var canBecomeMain: Bool { false }
    // https://developer.apple.com/documentation/appkit/nsaccessibility-swift.struct/subrole/floatingwindow
    override func accessibilitySubrole() -> NSAccessibility.Subrole? { .floatingWindow }
}

/// First click lands even though the panel is never key. Also the File Tray drop destination (A9)
/// and the two-finger swipe source (A6) — the panel only takes the mouse over the island, so both
/// only ever fire there.
final class IslandHostingView<Content: View>: NSHostingView<Content> {
    var islandTrackingRects: [CGRect] = []
    var onPointerChange: (() -> Void)?
    private var islandTrackingAreas: [NSTrackingArea] = []
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        islandTrackingAreas.forEach { removeTrackingArea($0) }
        // https://developer.apple.com/documentation/appkit/nsview/updatetrackingareas()
        islandTrackingAreas = islandTrackingRects.map { rect in
            let r = CGRect(x: rect.minX, y: bounds.height - rect.maxY, width: rect.width, height: rect.height)
            let area = NSTrackingArea(rect: r, options: [.mouseEnteredAndExited, .activeAlways], owner: self)
            addTrackingArea(area)
            return area
        }
    }
    override func mouseEntered(with event: NSEvent) { onPointerChange?() }
    override func mouseExited(with event: NSEvent) { onPointerChange?() }
    var onScroll: ((NSEvent) -> Void)?
    var onDragState: ((Bool) -> Void)?
    var onDrop: (([URL]) -> Bool)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func scrollWheel(with event: NSEvent) {
        if let onScroll { onScroll(event) } else { super.scrollWheel(with: event) }
    }

    static func fileURLs(_ pb: NSPasteboard) -> [URL] {
        (pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard onDrop != nil, !Self.fileURLs(sender.draggingPasteboard).isEmpty else { return [] }
        onDragState?(true)
        return .link   // the tray stores references, never copies
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        onDrop != nil && !Self.fileURLs(sender.draggingPasteboard).isEmpty ? .link : []
    }

    override func draggingExited(_ sender: NSDraggingInfo?) { onDragState?(false) }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = Self.fileURLs(sender.draggingPasteboard)
        defer { onDragState?(false) }
        return !urls.isEmpty && (onDrop?(urls) ?? false)
    }
}
