import AppKit
import Foundation
import SwiftUI

/// Menu-bar-center island, hosted by CUAService from launch (independent of Samantha's voice session).
/// Every source — Samantha's voice helper included (source=samantha) — publishes to the island hub
/// on 127.0.0.1:8777; the hub composes Samantha's state with other agents' rows and approvals.
@MainActor
final class IslandController {
    private var gateway: URL
    /// Voice helper control token (set by ServiceDelegate; empty = helper not launched by us).
    var controlToken: () -> String = { "" }
    var onStopListening: (() -> Void)?
    private var last: IslandSnapshot?
    private let model = IslandModel()
    private let menuBarPanel = SamanthaMenuBarIslandPanel()
    private var pendingConfirmId: String = ""
    private var voiceSessionActive = false
    private var loggedListeningReady = false
    private weak var cursorOverlay: CursorOverlay?

    private var sawReady = false
    private var shownKind = "idle"
    private var shownSince = Date.distantPast
    private var deferred: IslandSnapshot?
    private var deferTask: Task<Void, Never>?

    let store: IslandSettingsStore
    let hub: IslandHub
    private let hubServer: IslandHubServer
    private let socketServer: IslandHubSocketServer
    private let socketPath: String?
    /// System now playing (phase A1). Production starts the adapter; tests inject a fake source.
    let nowPlaying: NowPlayingController
    private let startsNowPlaying: Bool
    private var nowPlayingStarted = false
    private var activationObserver: NSObjectProtocol?
    private var promptGateObserver: NSObjectProtocol?
    /// A2 HUD (media-key tap + CoreAudio), A4 calendar, A9 tray store — production only (`startsSystem`).
    private(set) lazy var hud = IslandHUDController(model: model)
    let calendar = IslandCalendarController()
    let hotKey = IslandHotKey()
    private var announcementThrottle = IslandAnnouncementThrottle()
    private(set) lazy var focus = IslandFocusStatus()
    private var reduceMotionObserver: Any?
    private var motionFlush: Timer?
    private var motionLink: CVDisplayLink?
    private let startsSystem: Bool
    private var trayURL: URL?
    private let hubPort: UInt16?
    private(set) var boundHubPort: UInt16?
    private var started = false

    /// `hubPort` nil = no HTTP listener (unit tests drive `hub` directly); 0 = ephemeral.
    init(gatewayPort: Int = 8765, store: IslandSettingsStore? = nil, hubPort: UInt16? = IslandHub.defaultPort,
         socketPath: String? = nil, startsNowPlaying: Bool = false, startsSystem: Bool? = nil) {
        self.socketPath = socketPath
        self.startsNowPlaying = startsNowPlaying
        self.startsSystem = startsSystem ?? startsNowPlaying
        let resolvedStore = store ?? .shared
        nowPlaying = NowPlayingController(store: resolvedStore)
        gateway = URL(string: "http://127.0.0.1:\(gatewayPort)")!
        let store = store ?? .shared
        self.store = store
        self.hubPort = hubPort
        let hub = IslandHub(store: store)
        self.hub = hub
        hubServer = IslandHubServer { req in
            await hub.handleAsync(req, port: req.localPort)
        }
        socketServer = IslandHubSocketServer { req in
            await hub.handleAsync(req, port: 0)
        }
        model.resolveConfirm = { [weak self] approved in
            Task { await self?.resolveConfirm(approved: approved) }
        }
        model.onAction = { [weak self] action in
            Task { await self?.handle(action) }
        }
        menuBarPanel.attach(model: model)
        SystemVolume.onChange = { [weak self] state in self?.model.showHUD(state) }
        CredentialEntryController.shared.onChange = { [weak self] request in
            self?.model.setCredentialRequest(request)
            self?.menuBarPanel.updateCapturePrivacy()
        }
        model.onOpenSettings = { IslandSettingsWindowController.shared.showSettings() }
        hub.onSnapshot = { [weak self] snap in self?.ingest(snap) }
        nowPlaying.onChange = { [weak self] in self?.pushNowPlaying() }
        model.onMedia = { [weak self] cmd in self?.nowPlaying.perform(cmd) }
        model.onMediaAction = { [weak self] a in self?.mediaAction(a) }
        // Quick Peek waits while Guru is typing in the card's source chat app.
        model.peekSuppressed = { [weak self] a in
            IslandInput.isSourceApp(self?.frontmostAppName(), approval: a)
                && IslandInput.secondsSinceKeyDown() < IslandInput.typingWindow
        }
        model.onArrival = { [weak self] a in self?.cardArrived(a) }
        wireActivities()
        model.onAckExpired = { [weak self] ids in self?.hub.ackExpired(ids: ids) }
        applySettings(store)
        store.addObserver { [weak self] store in self?.applySettings(store) }
    }

    /// App launch: listener up, island shown in its idle state (per Island Settings).
    func start() {
        guard !started else { return }
        started = true
        if let hubPort {
            boundHubPort = hubServer.start(port: hubPort)
            IslandHub.boundPort = boundHubPort
            SamanthaActivityLog.startup(phase: "island_hub", status: boundHubPort == nil ? "fail" : "ok",
                                        detail: "127.0.0.1:\(boundHubPort.map(String.init) ?? "bind failed \(hubPort)")")
        }
        if let socketPath {
            hub.persistURL = URL(fileURLWithPath: (socketPath as NSString).deletingLastPathComponent)
                .appendingPathComponent("island-pending.json")
            IslandHub.boundSocketPath = socketServer.start(path: socketPath)
            SamanthaActivityLog.startup(phase: "island_socket", status: IslandHub.boundSocketPath == nil ? "fail" : "ok",
                                        detail: IslandHub.boundSocketPath == nil ? "bind failed" : "unix socket 0600")
        }
        startClock()
        if startsSystem { startSystemActivities() }
        if startsNowPlaying {
            nowPlayingStarted = true
            nowPlaying.startAutomatic()
            activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
            ) { [weak self] _ in Task { @MainActor in self?.nowPlaying.recompute() } }  // hide while source active
        }
        // Agent prompts: held off the island while their source app is frontmost (inline there).
        promptGateObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] _ in Task { @MainActor in self?.hub.frontmostAppChanged() } }
        ingest(hub.compose())
        updatePanelVisibility()
    }

    func shutdown() {
        CredentialEntryController.shared.cancelActive()
        hubServer.stop()
        socketServer.stop()
        IslandHub.boundSocketPath = nil
        nowPlaying.stop()
        if startsSystem {
            hud.stop(); calendar.stop()
            hotKey.unregister()
            IslandMotionRecorder.shared.enabled = false
            motionFlush?.invalidate(); motionFlush = nil
            if let l = motionLink { CVDisplayLinkStop(l); motionLink = nil }
        }
        if let o = reduceMotionObserver { NSWorkspace.shared.notificationCenter.removeObserver(o); reduceMotionObserver = nil }
        if let o = activationObserver { NSWorkspace.shared.notificationCenter.removeObserver(o); activationObserver = nil }
        if let o = promptGateObserver { NSWorkspace.shared.notificationCenter.removeObserver(o); promptGateObserver = nil }
        stopClock()
        menuBarPanel.hide()
    }

    private func updatePanelVisibility() {
        guard started else { return }
        if store.bool(IslandSettingsSchema.showIsland) {
            menuBarPanel.show(on: SamanthaMenuBarIslandPanel.menuBarScreen())
        } else {
            menuBarPanel.hide()
        }
    }

    /// Hub snapshot → model (dwell / connecting rules as before; meters-only updates are cheap).
    func ingest(_ snap: IslandSnapshot) {
        if last == nil || !snap.sameVisual(as: last!) {
            last = snap
            pendingConfirmId = snap.confirm_id
            apply(snap)  // synchronous on the main actor: snapshots render in order
        } else {
            last = snap
            model.applyMeter(snap)
        }
    }

    /// Push Settings → General / AI Agents into the model and panel. Re-run on every change.
    func applySettings(_ s: IslandSettingsStore) {
        typealias K = IslandSettingsSchema
        let shortcut = IslandShortcut.read(s)
        model.shortcutSpoken = shortcut.spoken
        if hotKey.shortcut != shortcut {
            hotKey.unregister()
            hotKey.shortcut = shortcut
        }
        if startsSystem {
            if s.bool(K.hotKey) {
                if !hotKey.register() {
                    SamanthaActivityLog.startup(phase: "island_hotkey", status: "fail", detail: "\(shortcut.display) is taken by another app; island keyboard shortcut off")
                }
            } else { hotKey.unregister() }
            applyMotionProbe()
        }
        IslandModel.peekDone = s.double(K.resultPeek)
        IslandModel.peekError = s.double(K.errorPeek)
        IslandModel.doneSettle = s.double(K.doneSettle)
        model.contrastOutline = s.bool(K.contrastOutline)
        updateAccessibilityDisplay()
        typealias N = NowPlayingKeys
        let style = NowPlayingStyle(color: s.string(N.color), compactWaveform: !s.bool(N.disableCompactWaveform),
                                    artworkFlip: !s.bool(N.disableArtworkFlip),
                                    action: s.string(N.action) == "shuffle" ? "favorite" : s.string(N.action))
        if style != model.mediaStyle { model.mediaStyle = style }
        if nowPlayingStarted { nowPlaying.applySettings() } else { nowPlaying.recompute() }
        model.samanthaEnabled = s.sourceEnabled(.samantha)
        let enabled = Dictionary(uniqueKeysWithValues: AgentSource.allCases.map { ($0, s.sourceEnabled($0)) })
        let mayRaise = Dictionary(uniqueKeysWithValues: AgentSource.allCases.map { ($0, s.mayRaiseApproval($0)) })
        model.agentAllowed = { a in
            let source: AgentSource? = a.id.hasPrefix("agent:") || a.id.hasPrefix("local:")
                ? .agentRoster : AgentSource.match(a.name) ?? AgentSource.match(a.app)
            guard let src = source else { return true }
            return enabled[src] ?? true
        }
        model.approvalAllowed = { a in
            guard let src = AgentSource.match(a.agent) ?? AgentSource.match(a.app) else { return true }
            return mayRaise[src] ?? true
        }
        model.idleActivity = s.string(K.idleActivity)
        // A2–A9
        IslandModel.hudDwell = max(1, min(2, s.double(K.soundDuration)))
        switch s.string(K.soundSpeed) {
        case "smooth": model.hudBarAnimation = .smooth(duration: 0.35)
        case "instant": model.hudBarAnimation = nil
        default: model.hudBarAnimation = .smooth(duration: 0.15)
        }
        if startsSystem {
            hud.volumeEnabled = s.bool(K.soundEnabled)
            hud.brightnessEnabled = s.bool(K.displayEnabled)
        }
        IslandModel.peekDwell = max(2.5, min(6, s.double(K.peekDuration)))
        model.calendarEnabled = s.bool(K.calendarEnabled)
        model.calendarLead = max(5, min(30, s.double(K.calendarLead))) * 60
        model.useCalendarColor = s.bool(K.calendarColor)
        calendar.enabled = s.bool(K.calendarEnabled)
        model.trayEnabled = s.bool(K.fileTrayEnabled)
        model.naturalMovement = s.bool(K.naturalMovement)
        model.gestures = (toggle: s.bool(K.swipeToToggle), cycle: s.bool(K.swipeToCycle))
        menuBarPanel.applySettings(s)
        model.reapplyFilters()
        hub.applyGating()
        updatePanelVisibility()
    }

    /// Card controls. Allow/Deny go to the confirm API; agent approvals to
    /// /api/agent/approval; the rest are local or posted to /api/island/action
    /// (gateway records them; stop/cancel/replay/retry are stubs until the
    /// voice session exposes those controls).
    private func handle(_ action: IslandCardAction) async {
        switch action {
        case .allow, .deny:
            return // resolveConfirm already called by the view
        case .show(let app), .open(let app):
            if !app.isEmpty { AppIconResolver.activate(app) }
        case .copy(let text):
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        case .dismiss:
            model.dismissCard()
        case .agentApprove(let id), .agentDeny(let id):
            let approved: Bool = { if case .agentApprove = action { return true } else { return false } }()
            hub.resolveApproval(id: id, approved: approved)  // the posting agent long-polls GET /api/agent/approval
        case .choiceSubmit(let id, let values, let text, let via):
            hub.answerChoice(id: id, values: values, text: text, via: "island-" + via)
        case .choiceDismiss(let id):
            hub.dismissChoice(id: id)
        case .stopListening:
            onStopListening?()
        case .cancel, .stopSpeaking, .replay, .stopTask, .retry:
            // Samantha's own card controls: the voice gateway runs them on her live session.
            let ok = await postAction(String(describing: action))
            if !ok { NSSound.beep() }
            if case .retry = action { model.dismissCard() }
        }
    }

    /// POST /api/island/action → {"handled": true} when the session ran it.
    private func postAction(_ action: String) async -> Bool {
        guard let url = URL(string: "/api/island/action", relativeTo: gateway) else { return false }
        var req = URLRequest(url: url, timeoutInterval: 3)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let token = controlToken()
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Voice-CUA-Control") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["action": action])
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return j["handled"] as? Bool ?? false
    }

    // MARK: Activities (A2–A9, agent rows)

    private func wireActivities() {
        hub.onPeek = { [weak self] p in
            guard let self, self.store.bool(IslandSettingsSchema.agentPeeks) else { return }
            self.model.showPeek(p)
        }
        model.onPeekOpen = { p in
            if !p.app.isEmpty { AppIconResolver.activate(p.app) }
        }
        model.onRowControl = { [weak self] a, action in
            guard let self else { return }
            switch action {
            case .review:
                let id = String(a.id.dropFirst("pending:".count))
                guard self.hub.selectApproval(id: id) else {
                    self.model.onAnnounce?("That request is no longer available to review.")
                    return
                }
                self.model.openApprovalFromRoster()
            case .dismiss:
                if !self.hub.dismissStale(id: a.id) { self.model.failRowControl(a.id) }
            case .stop, .retry:
                if !self.hub.requestControl(id: a.id, action: action.rawValue) { self.model.failRowControl(a.id) }
            }
        }
        model.onOpenEvent = { e, join in
            if join, let u = e.meetingURL { NSWorkspace.shared.open(u) } else { IslandCalendarController.open(e) }
        }
        model.onHaptic = { [weak self] in
            guard self?.store.bool(IslandSettingsSchema.haptics) ?? false else { return }
            NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        }
        model.onShare = { [weak self] urls in self?.menuBarPanel.share(urls) }
        model.onTrayChange = { [weak self] items in self?.saveTray(items) }
        calendar.onChange = { [weak self] events in self?.model.setCalendar(events) }
        IslandSettingsSchema.calendarAccessAction = { [weak self] in
            _ = self; PermissionBroker.requestCalendars()
        }
        IslandSettingsSchema.focusAccessAction = { [weak self] in
            _ = self; PermissionBroker.requestFocus()
        }
        hotKey.onPress = { [weak self] in self?.model.hotKeyPressed() }
        model.onAnnounce = { [weak self] text in
            guard let self, self.announcementThrottle.allows() else { return }
            let priority: NSAccessibilityPriorityLevel = self.model.announcementPriority == .high ? .high : .medium
            // https://developer.apple.com/documentation/appkit/nsaccessibility-swift.struct/notification/announcementrequested
            NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                                 userInfo: [.announcement: text, .priority: priority.rawValue])
        }
        model.peekGate = { [weak self] in
            guard let self else { return true }
            typealias K = IslandSettingsSchema
            let hideShare = self.store.bool(K.hideWhileSharing), respect = self.store.bool(K.respectFocus)
            return IslandPrivacy.peeksAllowed(sharing: hideShare && IslandPrivacy.screenSharingActive(),
                                              focused: respect && self.focus.isFocused,
                                              hideWhileSharing: hideShare, respectFocus: respect)
        }
        reduceMotionObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.model.systemReduceMotion = IslandMotion.systemReduceMotion
                self?.updateAccessibilityDisplay()
            }
        }
    }

    private func updateAccessibilityDisplay() {
        // https://developer.apple.com/documentation/appkit/nsworkspace/accessibilitydisplayshouldincreasecontrast
        // https://developer.apple.com/documentation/appkit/nsworkspace/accessibilitydisplayoptionsdidchangenotification
        let workspace = NSWorkspace.shared
        model.systemIncreaseContrast = workspace.accessibilityDisplayShouldIncreaseContrast
        model.systemDifferentiate = workspace.accessibilityDisplayShouldDifferentiateWithoutColor
        IslandInk.increased = model.contrastOutline || model.systemIncreaseContrast
    }

    private func startSystemActivities() {
        // Warm every activity offscreen once so the first real open drops no frames.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { MainActor.assumeIsolated { IslandPrewarm.run() } }
        hud.onOutputChanged = { [weak self] name, symbol in
            guard let self, self.store.bool(IslandSettingsSchema.connectivityEnabled), !name.isEmpty else { return }
            self.model.showPeek(IslandPeekItem(id: "output", source: "Connectivity", symbol: symbol, title: name, body: "Audio output"))
        }
        hud.start()
        calendar.start()
        if let dir = socketPath.map({ ($0 as NSString).deletingLastPathComponent }) {
            trayURL = URL(fileURLWithPath: dir).appendingPathComponent("island-tray.json")
            loadTray()
        }
    }

    private func loadTray() {
        guard let url = trayURL, let data = try? Data(contentsOf: url),
              let items = try? JSONDecoder().decode([IslandTrayItem].self, from: data) else { return }
        // References only: resolve bookmarks (files may have moved); drop missing.
        let live = items.compactMap { item -> IslandTrayItem? in
            var stale = false
            if let bm = item.bookmark, let u = try? URL(resolvingBookmarkData: bm, options: [.withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale) {
                var i = item
                i.path = u.path
                return FileManager.default.fileExists(atPath: u.path) ? i : nil
            }
            return FileManager.default.fileExists(atPath: item.path) ? item : nil
        }
        model.setTray(live)
    }

    private func saveTray(_ items: [IslandTrayItem]) {
        guard let url = trayURL, let data = try? JSONEncoder().encode(items) else { return }
        try? data.write(to: url, options: [.atomic])
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private func post(_ path: String, _ body: [String: Any]) async {
        guard let url = URL(string: path, relativeTo: gateway) else { return }
        var req = URLRequest(url: url, timeoutInterval: 3)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: req)
    }

    /// Frontmost app name (tests inject a fixed one).
    var frontmostAppName: () -> String? = { NSWorkspace.shared.frontmostApplication?.localizedName }

    /// Soft Tink on a new card — only when its source app isn't frontmost, and not when muted.
    private func cardArrived(_ a: IslandAgentApproval) {
        guard !store.bool(IslandSettingsSchema.mutePromptSound) else { return }
        guard !IslandInput.isSourceApp(frontmostAppName(), approval: a) else { return }
        IslandSound.tink()
    }

    // MARK: Now Playing

    private func pushNowPlaying() {
        model.mediaDevice = nowPlaying.device
        model.setNowPlaying(nowPlaying.info, visible: nowPlaying.visible)
    }

    /// Card action slot (user click only): Favorite (Music) / Copy.
    private func mediaAction(_ a: String) {
        guard let info = nowPlaying.info else { return }
        switch a {
        case "copy":
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString([info.title, info.artist].filter { !$0.isEmpty }.joined(separator: " — "), forType: .string)
        case "favorite":
            guard info.bundleID == NowPlayingRules.musicBundle,
                  !NSRunningApplication.runningApplications(withBundleIdentifier: NowPlayingRules.musicBundle).isEmpty,
                  AutomationGate.allowed(NowPlayingRules.musicBundle) else { return }
            let src = "tell application \"Music\"\ntry\nset favorited of current track to not (favorited of current track)\non error\nset loved of current track to not (loved of current track)\nend try\nend tell"
            DispatchQueue.global(qos: .userInitiated).async {
                var err: NSDictionary?
                NSAppleScript(source: src)?.executeAndReturnError(&err)
            }
        default: break
        }
    }

    /// Capture harness / test hooks.
    func openKeyboard() { model.hotKeyPressed() }
    var debugModel: IslandModel { model }
    var debugPanel: SamanthaMenuBarIslandPanel { menuBarPanel }

    func updateGateway(port: Int) {
        gateway = URL(string: "http://127.0.0.1:\(port)")!
    }

    /// Island badge for a missing required consent; tap opens onboarding at the permission rows.
    private var warningPermission: ACUPermission?
    func setPermissionWarning(_ p: ACUPermission?) {
        warningPermission = p
        let label = p.map { "Needs access: " + $0.label }
        if model.permissionWarning != label { model.permissionWarning = label }
        if model.onPermissionWarningTap == nil {
            model.onPermissionWarningTap = { [weak self] in
                OnboardingWindowController.shared.show(row: self?.warningPermission)
            }
        }
    }

    func setCursorOverlay(_ overlay: CursorOverlay) {
        cursorOverlay = overlay
    }

    /// Samantha's voice session began: her states arrive via the hub (source=samantha). Panel untouched.
    func startStreaming() {
        voiceSessionActive = true
        hub.samanthaActive = true
        sawReady = false
        deferred = nil
        deferTask?.cancel()
        deferTask = nil
        last = nil
        ingest(hub.compose())
        SamanthaActivityLog.startup(phase: "island_voice_begin", status: "ok", detail: "Connecting pill")
    }

    /// Samantha stopped: drop her state; the island stays up for other sources.
    func stopStreaming() {
        CredentialEntryController.shared.cancelActive()
        voiceSessionActive = false
        hub.samanthaActive = false
        loggedListeningReady = false
        sawReady = false
        deferred = nil
        deferTask?.cancel()
        deferTask = nil
        hideAgentCursor()
        pendingConfirmId = ""
        last = nil
        hub.clearSamantha()
    }


    func resolveConfirm(approved: Bool) async {
        guard !pendingConfirmId.isEmpty else { return }
        guard let url = URL(string: "/api/confirm", relativeTo: gateway) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Any] = ["confirm_id": pendingConfirmId, "approved": approved]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: req)
    }

    func postListening() async {
        guard let url = URL(string: "/api/session/set-listening", relativeTo: gateway) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: req)
    }

    private func apply(_ snap: IslandSnapshot) {
        var effective = snap
        if voiceSessionActive && snap.kind == "idle" {
            effective = sawReady
                ? IslandSnapshot(kind: "reconnecting", title: "Reconnecting")
                : IslandSnapshot(kind: "listening", title: "Connecting")
        } else if snap.kind != "idle" && snap.title != "Connecting" {
            sawReady = true
        }
        if IslandDwell.shouldHold(
            current: shownKind,
            since: shownSince,
            incoming: effective.kind,
            now: Date()
        ) {
            deferred = effective
            if deferTask == nil {
                deferTask = Task { [weak self] in
                    try? await Task.sleep(nanoseconds: UInt64(IslandDwell.holdSeconds * 1_000_000_000))
                    self?.flushDeferred()
                }
            }
            return
        }
        deferTask?.cancel()
        deferTask = nil
        deferred = nil
        render(effective)
    }

    private func flushDeferred() {
        deferTask = nil
        guard let d = deferred else { return }
        deferred = nil
        render(d)
    }

    private func render(_ s: IslandSnapshot) {
        guard model.apply(s) else { return } // benign error: never shown
        if s.kind != shownKind {
            shownKind = s.kind
            shownSince = Date()
        }
        if s.kind == "listening", s.title == "Listening", !loggedListeningReady {
            loggedListeningReady = true
            SamanthaActivityLog.startup(phase: "island_listening", status: "ok")
        }
        if s.kind == "idle" { hideAgentCursor() }
    }

    /// 1 s clock for tint expiry (4 s) and circle dim (10 s) / removal (60 s).
    private var clock: Timer?

    private func startClock() {
        guard clock == nil else { return }
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.model.tick()
                self.hub.expire()
                if self.startsSystem { self.hub.refreshLocalHarnesses() }
                self.nowPlaying.recompute()   // idle-duration collapse after a pause
            }
        }
        RunLoop.main.add(timer, forMode: .common)  // keep ticking during menu tracking
        clock = timer
    }

    private func stopClock() {
        clock?.invalidate()
        clock = nil
    }

    /// Session end / disconnect — not between tasks while Listening.
    private func hideAgentCursor() {
        cursorOverlay?.hide()
    }

    // MARK: Motion probe (debug: `defaults write com.ingpoc.cua-service island.debug.motionProbe -bool true`)

    /// Per-vsync shape samples → ~/.cache/macos-cua/island-motion.jsonl (in-process; no screen capture).
    /// A CVDisplayLink reads the probe's latest value once per real display refresh.
    private func applyMotionProbe() {
        let on = UserDefaults.standard.bool(forKey: "island.debug.motionProbe")
        IslandMotionRecorder.shared.enabled = on
        motionFlush?.invalidate()
        motionFlush = nil
        if let l = motionLink { CVDisplayLinkStop(l); motionLink = nil }
        guard on else { return }
        let sampler = IslandVsyncSampler.shared
        var link: CVDisplayLink?
        CVDisplayLinkCreateWithActiveCGDisplays(&link)
        if let link {
            CVDisplayLinkSetOutputCallback(link, { _, _, _, _, _, _ in
                IslandVsyncSampler.shared.tick()
                return kCVReturnSuccess
            }, nil)
            CVDisplayLinkStart(link)
            motionLink = link
        }
        let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/macos-cua/island-motion.jsonl")
        // island-motion.jsonl = one row per vsync; island-motion-frames.jsonl = one row per rendered
        // frame (each distinct interpolated value SwiftUI produced, with its timestamp).
        let framesURL = url.deletingLastPathComponent().appendingPathComponent("island-motion-frames.jsonl")
        func append(_ s: [IslandMotionRecorder.Sample], to u: URL) {
            guard !s.isEmpty else { return }
            let lines = s.map { String(format: "{\"t\":%.6f,\"w\":%.3f,\"h\":%.3f}\n", $0.t, $0.w, $0.h) }.joined()
            if let h = try? FileHandle(forWritingTo: u) { h.seekToEndOfFile(); h.write(Data(lines.utf8)); try? h.close() }
            else { try? Data(lines.utf8).write(to: u) }
        }
        motionFlush = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            append(IslandMotionRecorder.shared.drain(), to: framesURL)
            append(sampler.drain(), to: url)
        }
    }
}

/// One sample per display refresh (CVDisplayLink thread): the probe's latest size at that vsync.
final class IslandVsyncSampler: @unchecked Sendable {
    static let shared = IslandVsyncSampler()
    private let lock = NSLock()
    private var buf: [IslandMotionRecorder.Sample] = []
    func tick() {
        guard let l = IslandMotionRecorder.shared.latest else { return }
        lock.lock(); defer { lock.unlock() }
        // Only record while something moves (or just stopped): keeps the file small.
        if let p = buf.last, p.w == l.w, p.h == l.h, CACurrentMediaTime() - p.t > 0.5 { return }
        buf.append(.init(t: CACurrentMediaTime(), w: l.w, h: l.h))
        if buf.count > 20000 { buf.removeFirst(5000) }
    }
    func drain() -> [IslandMotionRecorder.Sample] { lock.lock(); defer { lock.unlock() }; let b = buf; buf.removeAll(); return b }
}
