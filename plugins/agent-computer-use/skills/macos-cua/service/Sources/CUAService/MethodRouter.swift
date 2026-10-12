import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import Logging

final class MethodRouter: @unchecked Sendable {
    private let appResolver: AppResolver
    private let axTree: AXTreeWalker
    private let screenshotCapture: ScreenshotCapture
    private let settleEngine: SettleEngine
    private let cursorOverlay: CursorOverlay
    private let inputActions: InputActions
    private let logger: Logger
    private var requestRunning = false

    init(
        appResolver: AppResolver,
        axTree: AXTreeWalker,
        screenshotCapture: ScreenshotCapture,
        settleEngine: SettleEngine,
        cursorOverlay: CursorOverlay,
        inputActions: InputActions,
        logger: Logger
    ) {
        self.appResolver = appResolver
        self.axTree = axTree
        self.screenshotCapture = screenshotCapture
        self.settleEngine = settleEngine
        self.cursorOverlay = cursorOverlay
        self.inputActions = inputActions
        self.logger = logger
    }

    /// Read/config RPCs for the overlay. They bypass the request lock so
    /// `cursor status` can observe visibility while an action is in flight.
    static let cursorControlMethods: Set<String> = ["cursor_status", "configure_cursor"]
    /// Methods that never move the tip: no action bracket, so observers
    /// polling them neither show the tip nor extend its idle countdown.
    static let nonActionMethods: Set<String> = ["list_apps", "hide_agent_cursor", "wait"]

    static func movesTip(_ req: JSONRPCRequest) -> Bool {
        if nonActionMethods.contains(req.method) { return false }
        if req.method == "get_app_state" { return (req.param("raiseForInput") as Bool?) == true }
        return true
    }

    @MainActor
    func handle(_ request: JSONRPCRequest) async -> JSONRPCResponse {
        if Self.cursorControlMethods.contains(request.method) {
            do {
                return .success(id: request.id, try handleCursorControl(request))
            } catch let e as RPCMethodError {
                return .error(id: request.id, code: e.code, message: e.message)
            } catch {
                return .error(id: request.id, code: -32603, message: error.localizedDescription)
            }
        }
        // ponytail: desktop input is one shared resource; split lanes only if
        // independent displays become a measured concurrent use case.
        while requestRunning {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        requestRunning = true
        defer { requestRunning = false }
        // Action bracket: the idle-hide countdown starts only after the whole
        // request (glide, landing gate, press) returned — never mid-press.
        let isAction = Self.movesTip(request)
        if isAction { cursorOverlay.beginAction(request.method) }
        defer { if isAction { cursorOverlay.endAction(request.method) } }
        do {
            let result = try await dispatch(request)
            return .success(id: request.id, result)
        } catch let e as RPCMethodError {
            return .error(id: request.id, code: e.code, message: e.message)
        } catch {
            return .error(id: request.id, code: -32603, message: error.localizedDescription)
        }
    }

    @MainActor
    private func dispatch(_ req: JSONRPCRequest) async throws -> Any {
        // Per-request landing contract (requests are serialized above).
        lastLanding = nil
        allowUnlanded = (req.param("allow_unlanded") as Bool?) == true
        let value = try await route(req)
        // Every input result reports how the tip landed before its press.
        if var dict = value as? [String: Any], dict["cursor_landing"] == nil,
           let landing = lastLanding {
            dict["cursor_landing"] = landing.dict
            return dict
        }
        return value
    }

    @MainActor
    private func route(_ req: JSONRPCRequest) async throws -> Any {
        switch req.method {
        case "list_apps":
            return appResolver.listApps()

        case "get_app_state":
            return try await handleGetAppState(req)

        case "click":
            return try await handleClick(req)

        case "press_key":
            return try await handlePressKey(req)

        case "hold_key":
            return try await handleHoldKey(req)

        case "type_text":
            return try await handleTypeText(req)

        case "scroll":
            return try await handleScroll(req)

        case "set_value":
            return try await handleSetValue(req)

        case "select_text":
            return try await handleSelectText(req)

        case "perform_secondary_action":
            return try await handleSecondaryAction(req)

        case "drag":
            return try await handleDrag(req)

        case "open_item":
            return try await handleOpenItem(req)

        case "execute_plan":
            return try await handleExecutePlan(req)

        case "wait":
            return try await handleWait(req)

        case "hide_agent_cursor":
            cursorOverlay.hide()
            return ["ok": true] as [String: Any]

        default:
            throw RPCMethodError(
                code: -32601,
                message: "Method not found: \(req.method)"
            )
        }
    }

    @MainActor
    private func handleCursorControl(_ req: JSONRPCRequest) throws -> Any {
        if req.method == "configure_cursor" {
            guard let ms = req.paramInt("idle_hide_ms") else {
                throw RPCMethodError(code: -32602, message: "configure_cursor needs integer idle_hide_ms")
            }
            try cursorOverlay.configure(idleHideMs: ms)
        }
        var d = cursorOverlay.status(includeEvents: (req.param("events") as Bool?) == true)
        d["ok"] = true
        return d
    }

    @MainActor
    private func handleExecutePlan(_ req: JSONRPCRequest) async throws -> Any {
        // A plan is a whole task: the cursor stays visible across its steps
        // and hides when the plan ends, success or failure.
        defer { cursorOverlay.hide(reason: "plan_end") }
        guard let app: String = req.param("app"),
              let steps: [Any] = req.param("steps"),
              !steps.isEmpty, steps.count <= 50
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or 1...50 plan steps")
        }
        let allowed = Set([
            "click", "press_key", "type_text", "scroll", "set_value",
            "select_text", "perform_secondary_action", "drag", "open_item",
            "get_app_state", "wait",
        ])
        let pin = try req.paramCGWindowID("windowId", "window_id")
        let before = await planState(app: app, windowID: pin)
        var results: [[String: Any]] = []
        for raw in steps {
            guard let step = raw as? [String: Any],
                  let method = step["method"] as? String,
                  allowed.contains(method)
            else {
                throw RPCMethodError(code: -32602, message: "Invalid plan step")
            }
            var params = step["params"] as? [String: Any] ?? [:]
            params["app"] = params["app"] ?? app
            params = Self.pinPlanParams(params, windowID: pin)
            if (req.param("allow_unlanded") as Bool?) == true {
                params["allow_unlanded"] = params["allow_unlanded"] ?? true
            }
            let child = JSONRPCRequest(
                jsonrpc: "2.0",
                method: method,
                params: params.mapValues(AnyCodable.init),
                id: nil
            )
            let value: Any
            do {
                value = try await dispatch(child)
            } catch let e as RPCMethodError where e.code == Self.cursorNotLandedCode {
                // Refused press: stop the plan, keep before/after evidence.
                results.append([
                    "ok": false, "error": e.message,
                    "error_code": "CURSOR_NOT_LANDED", "method": method,
                ])
                break
            }
            var result = value as? [String: Any] ?? ["ok": false, "error": "invalid step result"]
            if method == "get_app_state" && result["error"] == nil {
                result["ok"] = true
                result["method"] = "focus"
            }
            results.append(result)
            if result["ok"] as? Bool == false || result["error"] != nil {
                break
            }
        }
        let after = await planState(app: app, windowID: pin)
        return [
            "ok": results.count == steps.count && results.allSatisfy { $0["ok"] as? Bool == true },
            "before": before,
            "after": after,
            "results": results,
        ] as [String: Any]
    }

    @MainActor
    private func planState(app: String, windowID: CGWindowID? = nil) async -> [String: Any] {
        // includeElements so compact expect.value can verify fields after mutations
        // (Codex r8 major 2). Screenshots stay off — planState must skip capture.
        // The enclosing plan windowId is part of the observation, not only the steps.
        var params: [String: AnyCodable] = [
            "app": AnyCodable(app),
            "disableDiff": AnyCodable(true),
            "includeScreenshot": AnyCodable(false),
            "includeElements": AnyCodable(true),
        ]
        if let windowID, windowID != 0 {
            params["windowId"] = AnyCodable(Int(windowID))
        }
        let request = JSONRPCRequest(
            jsonrpc: "2.0",
            method: "get_app_state",
            params: params,
            id: nil
        )
        do {
            return try await handleGetAppState(request) as? [String: Any] ?? [:]
        } catch {
            return ["text": "", "error": error.localizedDescription]
        }
    }

    /// Optional caller-pinned CGWindowID (windowId / window_id). When set,
    /// action RPCs must not re-resolve to a different focused window.
    /// Invalid integers are an RPC error, never a trapping UInt32 conversion.
    private func pinnedWindowID(_ req: JSONRPCRequest) throws -> CGWindowID? {
        try req.paramCGWindowID("windowId", "window_id")
    }

    /// Copy the enclosing plan pin onto a child that did not set its own.
    static func pinPlanParams(_ params: [String: Any], windowID: CGWindowID?) -> [String: Any] {
        var params = params
        guard let windowID, windowID != 0 else { return params }
        if params["windowId"] == nil && params["window_id"] == nil {
            params["windowId"] = Int(windowID)
        }
        return params
    }

    /// Index actions must target the pinned window — never another window's tree.
    private func requirePinnedAXWindow(_ resolved: ResolvedApp) throws {
        guard resolved.windowID != 0 else { return }
        guard axTree.hasPinnedAXWindow(
            axApp: resolved.axApp, windowID: resolved.windowID
        ) else {
            throw RPCMethodError(
                code: -32002,
                message: "Pinned AX window \(resolved.windowID) not found; refusing fallback to focused/first window"
            )
        }
    }


    @MainActor
    private func handleWait(_ req: JSONRPCRequest) async throws -> Any {
        let seconds = min(max(req.paramDouble("seconds") ?? 0, 0), 45)
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        return ["ok": true, "method": "wait", "wait": seconds] as [String: Any]
    }

    @MainActor
    private func handleOpenItem(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app") else {
            throw RPCMethodError(code: -32602, message: "Missing app")
        }
        let path: String? = req.param("path")
        let rawURL: String? = req.param("url")
        guard (path == nil) != (rawURL == nil) else {
            throw RPCMethodError(code: -32602, message: "Pass exactly one of path or url")
        }

        let target: URL
        if let path {
            guard path.hasPrefix("/") else {
                throw RPCMethodError(code: -32602, message: "Path must be absolute")
            }
            let standardized = URL(fileURLWithPath: path).standardizedFileURL
            guard FileManager.default.fileExists(atPath: standardized.path) else {
                throw RPCMethodError(code: -32602, message: "Path does not exist: \(standardized.path)")
            }
            target = standardized
        } else if let rawURL, let parsed = URL(string: rawURL), parsed.scheme != nil {
            target = parsed
        } else {
            throw RPCMethodError(code: -32602, message: "URL must include a scheme")
        }

        if (req.param("reveal") as Bool?) == true, target.isFileURL {
            NSWorkspace.shared.activateFileViewerSelecting([target])
            return ["ok": true, "method": "workspace-reveal", "path": target.path]
        }

        guard let applicationURL = appResolver.applicationURL(named: app)
            ?? NSWorkspace.shared.urlForApplication(withBundleIdentifier: app)
        else {
            throw RPCMethodError(code: -32602, message: "Application not found: \(app)")
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        let running = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<NSRunningApplication?, Error>) in
            NSWorkspace.shared.open(
                [target],
                withApplicationAt: applicationURL,
                configuration: configuration
            ) { applications, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: applications)
                }
            }
        }
        return [
            "ok": true,
            "method": "workspace-open",
            "app": app,
            "path": target.isFileURL ? target.path : target.absoluteString,
            "pid": running.map { Int($0.processIdentifier) } as Any,
        ] as [String: Any]
    }

    // MARK: - get_app_state

    @MainActor
    private func handleGetAppState(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app") else {
            throw RPCMethodError(code: -32602, message: "Missing 'app' parameter")
        }
        let disableDiff: Bool = req.param("disableDiff") ?? false
        let raiseForInput: Bool = req.param("raiseForInput") ?? false
        let includeScreenshot: Bool = req.param("includeScreenshot") ?? true
        let t0 = ProcessInfo.processInfo.systemUptime
        let resolved0 = try appResolver.resolve(app, raiseForInput: raiseForInput, windowID: pinnedWindowID(req))
        var resolved = resolved0
        let t1 = ProcessInfo.processInfo.systemUptime
        var snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            maxElements: req.paramInt("maxElements") ?? 80,
            disableDiff: disableDiff,
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        let enrich = axTree.lastEnrichMethod
        let t2 = ProcessInfo.processInfo.systemUptime
        var screenshotPath: String?
        var captureCached = false
        var captureRetry = false
        if includeScreenshot {
            var shot = await screenshotCapture.capture(
                windowID: resolved.windowID,
                axBounds: snapshot.windowFrame
            )
            screenshotPath = shot.path
            captureCached = shot.cached
            if screenshotPath == nil {
                captureRetry = true
                try await Task.sleep(nanoseconds: 120_000_000)
                resolved = try appResolver.resolve(app, raiseForInput: raiseForInput, windowID: pinnedWindowID(req))
                snapshot = axTree.walk(
                    axApp: resolved.axApp,
                    windowID: resolved.windowID,
                    maxElements: req.paramInt("maxElements") ?? 80,
                    disableDiff: disableDiff,
                    pid: resolved.pid,
                    bundleID: resolved.bundleID
                )
                shot = await screenshotCapture.capture(
                    windowID: resolved.windowID,
                    axBounds: snapshot.windowFrame
                )
                screenshotPath = shot.path
                captureCached = shot.cached
            }
        }
        let t3 = ProcessInfo.processInfo.systemUptime

        var result: [String: Any] = [
            "app": resolved.name,
            "pid": Int(resolved.pid),
            "windowId": Int(resolved.windowID),
            "windowTitle": resolved.windowTitle as Any,
            "text": snapshot.markdown,
            "elementCount": snapshot.elements.count,
            "axTrusted": AXIsProcessTrusted(),
            "screenCapture": CGPreflightScreenCaptureAccess(),
            "timings_ms": [
                "resolve": Int(((t1 - t0) * 1000).rounded()),
                "walk": Int(((t2 - t1) * 1000).rounded()),
                "capture": Int(((t3 - t2) * 1000).rounded()),
                "capture_retry": captureRetry,
                "capture_cached": captureCached,
            ] as [String: Any],
        ]
        if let frame = snapshot.windowFrame {
            result["windowWidth"] = frame.width
            result["windowHeight"] = frame.height
        }
        if (req.param("includeElements") as Bool?) == true {
            // Structured view of the SAME walk the input methods index into.
            result["elements"] = snapshot.elements.map(Self.elementDict)
            if let frame = snapshot.windowFrame {
                result["windowFrame"] = Self.frameDict(frame)
            }
        }
        if let enrich {
            result["ax_enrich"] = enrich
        }
        if let screenshotPath {
            result["screenshot"] = ["url": "file://\(screenshotPath)"]
        }
        if raiseForInput {
            try await syncCursorToWindow(resolved: resolved, snapshot: snapshot, gate: false)
            result["cursor_landing"] = lastLanding?.dict as Any
        }
        return result
    }

    static func frameDict(_ r: CGRect) -> [String: Any] {
        ["x": Double(r.origin.x), "y": Double(r.origin.y),
         "w": Double(r.width), "h": Double(r.height)]
    }

    static func elementDict(_ e: AXElement) -> [String: Any] {
        [
            "element_index": e.index,
            "role": e.role,
            "label": e.label as Any,
            "value": e.value as Any,
            "frame": frameDict(e.frame),
            "actions": e.actions,
            "parent_index": e.parentIndex as Any,
            "depth": e.depth,
        ]
    }

    /// Walk bound for index-addressed input: always include the index so a
    /// snapshot taken with a larger maxElements stays addressable.
    static func walkBound(for index: Int?) -> Int {
        max(80, (index ?? 0) + 1)
    }

    // MARK: - Cursor sync (agent pointer follows every input surface)

    /// Last tip landing (reported on click results as `cursor_landing`).
    private var lastLanding: CursorOverlay.Landing?
    /// Caller opted in (`allow_unlanded: true`) to press blind when the tip
    /// cannot be confirmed; the result then carries cursor_landing.landed=false.
    private var allowUnlanded = false

    /// Error code for a press refused because the tip did not land.
    static let cursorNotLandedCode = -32010

    @MainActor
    private func syncCursorToPoint(
        _ point: CGPoint,
        windowID: CGWindowID,
        axBounds: CGRect?,
        gate: Bool = true
    ) async throws -> CGPoint {
        // Tip lands (confirmed: final frame + measured origin + drawn frame)
        // before any press. Unconfirmed after hide + one retry → the press is
        // REFUSED (fail loud) unless the caller opted in with allow_unlanded.
        let landing = await cursorOverlay.glideAndLand(
            screenPoint: point,
            windowID: windowID,
            axBounds: axBounds
        )
        lastLanding = landing
        if gate { try requireLanded(landing) }
        return landing.point
    }

    @MainActor
    private func requireLanded(_ landing: CursorOverlay.Landing) throws {
        if landing.landed || allowUnlanded { return }
        let render = landing.presented.map { "\($0.dict)" } ?? "none"
        throw RPCMethodError(
            code: Self.cursorNotLandedCode,
            message: "CURSOR_NOT_LANDED: agent cursor did not land after hide + one retry "
                + "(method=\(landing.method) error_pt=\(landing.errorPt) wait_ms=\(landing.waitMs) render=\(render)); "
                + "press NOT sent. Hint: wake/unlock the display or check Screen Recording/overlay, then retry; "
                + "only if a blind press is acceptable pass allow_unlanded:true (result reports cursor_landing.landed=false)."
        )
    }

    /// Input with no window frame cannot show a tip: same contract as a miss.
    @MainActor
    private func requireWindowFrame(_ snapshot: AXSnapshot) throws {
        if let frame = snapshot.windowFrame, frame.width > 0, frame.height > 0 { return }
        if allowUnlanded { return }
        throw RPCMethodError(
            code: Self.cursorNotLandedCode,
            message: "CURSOR_NOT_LANDED: target window has no frame, the agent cursor cannot land; press NOT sent. "
                + "Hint: bring the window on screen, or pass allow_unlanded:true for a blind press."
        )
    }

    @MainActor
    private func syncCursorToElement(
        _ element: AXElement,
        resolved: ResolvedApp,
        snapshot: AXSnapshot
    ) async throws {
        _ = try await syncCursorToPoint(
            CGPoint(x: element.frame.midX, y: element.frame.midY),
            windowID: resolved.windowID,
            axBounds: snapshot.windowFrame
        )
    }

    @MainActor
    private func syncCursorToWindow(
        resolved: ResolvedApp,
        snapshot: AXSnapshot,
        gate: Bool = true
    ) async throws {
        if gate { try requireWindowFrame(snapshot) }
        guard let frame = snapshot.windowFrame,
              frame.width > 0, frame.height > 0 else {
            // Only reachable for a gated press with allow_unlanded:true (or an
            // ungated observe): the press must still report landed:false.
            if gate {
                lastLanding = CursorOverlay.Landing(
                    point: .zero, landed: false, method: "no_window_frame",
                    errorPt: 0, waitMs: 0, presented: nil
                )
            }
            return
        }
        _ = try await syncCursorToPoint(
            CGPoint(x: frame.midX, y: frame.midY),
            windowID: resolved.windowID,
            axBounds: frame,
            gate: gate
        )
    }

    @MainActor
    private func syncCursorToFocusedInput(
        resolved: ResolvedApp,
        snapshot: AXSnapshot
    ) async throws {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(
            resolved.axApp, kAXFocusedUIElementAttribute as CFString, &ref
        )
        if let ref,
           let frame = Self.axQuartzFrame(ref as! AXUIElement),
           frame.width > 0, frame.height > 0 {
            _ = try await syncCursorToPoint(
                CGPoint(x: frame.midX, y: frame.midY),
                windowID: resolved.windowID,
                axBounds: snapshot.windowFrame
            )
            return
        }
        let textRoles: Set<String> = [
            "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField",
        ]
        if let field = snapshot.elements.first(where: { textRoles.contains($0.role) }) {
            try await syncCursorToElement(field, resolved: resolved, snapshot: snapshot)
            return
        }
        try await syncCursorToWindow(resolved: resolved, snapshot: snapshot)
    }

    private static func axQuartzFrame(_ el: AXUIElement) -> CGRect? {
        var posRef: CFTypeRef?
        var sizeRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            el, kAXPositionAttribute as CFString, &posRef
        ) == .success,
              AXUIElementCopyAttributeValue(
                  el, kAXSizeAttribute as CFString, &sizeRef
              ) == .success,
              let posVal = posRef,
              let sizeVal = sizeRef
        else { return nil }
        var pos = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(posVal as! AXValue, .cgPoint, &pos)
        AXValueGetValue(sizeVal as! AXValue, .cgSize, &size)
        guard size.width > 0, size.height > 0 else { return nil }
        return CGRect(origin: pos, size: size)
    }

    // MARK: - click

    @MainActor
    private func handleClick(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app") else {
            throw RPCMethodError(code: -32602, message: "Missing 'app' parameter")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        try requirePinnedAXWindow(resolved)
        screenshotCapture.invalidate()
        let elementIndex: Int? = req.paramInt("element_index")
        let cached = axTree.cachedSnapshot(windowID: resolved.windowID)
            .flatMap { snap in snap.elements.count > (elementIndex ?? -1) ? snap : nil }
        let snapshot = cached
            ?? axTree.walk(
                axApp: resolved.axApp,
                windowID: resolved.windowID,
                maxElements: Self.walkBound(for: elementIndex),
                pid: resolved.pid,
                bundleID: resolved.bundleID
            )

        let label: String? = req.param("label")
        let x: Double? = req.paramDouble("x")
        let y: Double? = req.paramDouble("y")

        var target: AXElement?

        if let elementIndex {
            target = axTree.findElementByIndex(snapshot, index: elementIndex)
        } else if let label {
            target = axTree.findElementByLabel(snapshot, label: label)
        }

        let clickPoint: CGPoint
        if let x, let y {
            clickPoint = CGPoint(x: x, y: y)
        } else if let target {
            clickPoint = CGPoint(x: target.frame.midX, y: target.frame.midY)
        } else {
            if let label {
                throw RPCMethodError(
                    code: -32602,
                    message: "Label not found: \(label). elementCount=\(snapshot.elements.count) axTrusted=\(AXIsProcessTrusted()). If the tree is AXUnknown, re-enable Accessibility for CUAService and relaunch."
                )
            }
            throw RPCMethodError(
                code: -32602,
                message: "No target: provide label, element_index, or x/y"
            )
        }

        // Tip lands first, then press — concurrent fire-and-forget left the
        // badge mid-glide while AX/HID already fired.
        let aimPoint = try await syncCursorToPoint(
            clickPoint,
            windowID: resolved.windowID,
            axBounds: snapshot.windowFrame
        )

        let clickCount = max(1, min(req.paramInt("click_count") ?? 1, 3))
        let pressResult: [String: Any]
        if let target, clickCount == 1 {
            pressResult = inputActions.performPress(
                resolved, element: target, snapshot: snapshot
            )
        } else {
            pressResult = inputActions.coordinateClick(
                resolved: resolved, point: aimPoint, clickCount: clickCount
            )
        }

        let method = pressResult["method"] as? String
        if method == "ax-press" || method == "ax-click"
            || method == "ax-set-value" || method == "cgevent-click"
            || method == "cgevent-click-pid" {
            var result = pressResult
            result["cursor_landing"] = lastLanding?.dict as Any
            result["settled"] = [
                "settled": true,
                "elapsed_ms": 0,
                "notifications": 0,
                "reason": "ax-action",
            ]
            result["label"] = label as Any
            result["element_index"] = (target?.index ?? elementIndex) as Any
            return result
        }

        // Settle
        let settled = await settleEngine.waitForQuiescence(
            app: resolved.axApp,
            pid: resolved.pid,
            timeout: 0.6,
            minQuiet: 0.08
        )
        if settled.notifications > 0 {
            axTree.invalidateCaches()
        }

        var result = pressResult
        result["cursor_landing"] = lastLanding?.dict as Any
        result["settled"] = settled.dict
        result["label"] = label as Any
        result["element_index"] = (target?.index ?? elementIndex) as Any
        return result
    }

    // MARK: - Input methods

    @MainActor
    private func handlePressKey(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let key: String = req.param("key")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or key")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        try await syncCursorToWindow(resolved: resolved, snapshot: snapshot)
        try recheckPinnedFocus(resolved)
        let pressed = inputActions.pressKey(resolved: resolved, key: key)
        let keyNorm = key.lowercased().replacingOccurrences(of: " ", with: "")
        if ["cmd+n", "command+n", "cmd+t", "command+t"].contains(keyNorm) {
            appResolver.invalidateWindowCache()
        }
        return pressed
    }

    /// After the cursor glide, the key must still be aimed at the pinned window.
    /// Focus can move to another window while the tip is in flight.
    @MainActor
    private func recheckPinnedFocus(_ resolved: ResolvedApp) throws {
        guard resolved.windowID != 0 else { return }
        let focused = appResolver.focusedCGWindowID(axApp: resolved.axApp)
        guard focused == resolved.windowID else {
            throw RPCMethodError(
                code: -32002,
                message: "pinned window \(resolved.windowID) is not focused after cursor landing"
                    + " (focused \(focused.map { String($0) } ?? "none")); key NOT sent"
            )
        }
    }

    @MainActor
    private func handleHoldKey(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let key: String = req.param("key")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or key")
        }
        let seconds = min(max(req.paramDouble("seconds") ?? 0, 0.05), 10)
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        try requirePinnedAXWindow(resolved)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        try await syncCursorToWindow(resolved: resolved, snapshot: snapshot)
        try recheckPinnedFocus(resolved)
        return inputActions.holdKey(resolved: resolved, key: key, seconds: seconds)
    }

    @MainActor
    private func handleTypeText(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let text: String = req.param("text")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or text")
        }
        let afterNew: Bool = req.param("after_new_document") ?? false
        let resolved = try appResolver.resolve(
            app, raiseForInput: true, preferFocusedWindow: afterNew,
            windowID: pinnedWindowID(req)
        )
        try requirePinnedAXWindow(resolved)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        let elementIndex: Int? = req.paramInt("element_index")
        let target = elementIndex.flatMap { axTree.findElementByIndex(snapshot, index: $0) }
        if elementIndex != nil && target == nil {
            throw RPCMethodError(
                code: -32602,
                message: "Element not found at index \(elementIndex ?? -1)"
            )
        }
        try await syncCursorToFocusedInput(resolved: resolved, snapshot: snapshot)
        return inputActions.typeText(
            resolved: resolved, text: text, afterNewDocument: afterNew, target: target
        )
    }

    @MainActor
    private func handleScroll(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let direction: String = req.param("direction")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or direction")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            maxElements: Self.walkBound(for: req.paramInt("element_index")),
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        let element: AXElement?
        if let idx: Int = req.paramInt("element_index") {
            element = axTree.findElementByIndex(snapshot, index: idx)
        } else {
            element = nil
        }
        if let element {
            try await syncCursorToElement(element, resolved: resolved, snapshot: snapshot)
        } else {
            try await syncCursorToWindow(resolved: resolved, snapshot: snapshot)
        }
        let pages: Int = req.paramInt("pages") ?? 1
        return inputActions.scroll(
            resolved: resolved,
            element: element,
            direction: direction,
            pages: pages,
            windowFrame: snapshot.windowFrame
        )
    }

    @MainActor
    private func handleSetValue(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let idx = req.paramInt("element_index"),
              let value: String = req.param("value")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app, element_index, or value")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        try requirePinnedAXWindow(resolved)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            maxElements: Self.walkBound(for: idx),
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        guard let element = axTree.findElementByIndex(snapshot, index: idx) else {
            throw RPCMethodError(code: -32602, message: "Element not found at index \(idx)")
        }
        try await syncCursorToElement(element, resolved: resolved, snapshot: snapshot)
        return inputActions.setValue(
            resolved: resolved, element: element, value: value
        )
    }

    @MainActor
    private func handleSelectText(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let idx = req.paramInt("element_index"),
              let text: String = req.param("text")
        else {
            throw RPCMethodError(code: -32602, message: "Missing required params")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        try requirePinnedAXWindow(resolved)
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            maxElements: Self.walkBound(for: idx),
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        guard let element = axTree.findElementByIndex(snapshot, index: idx) else {
            throw RPCMethodError(code: -32602, message: "Element not found at index \(idx)")
        }
        try await syncCursorToElement(element, resolved: resolved, snapshot: snapshot)
        return inputActions.selectText(
            resolved: resolved,
            element: element,
            text: text,
            prefix: req.param("prefix"),
            suffix: req.param("suffix"),
            selectionType: req.param("selection_type")
        )
    }

    @MainActor
    private func handleSecondaryAction(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let idx = req.paramInt("element_index"),
              let action: String = req.param("action")
        else {
            throw RPCMethodError(code: -32602, message: "Missing required params")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        try requirePinnedAXWindow(resolved)
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            maxElements: Self.walkBound(for: idx),
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        guard let element = axTree.findElementByIndex(snapshot, index: idx) else {
            throw RPCMethodError(code: -32602, message: "Element not found at index \(idx)")
        }
        try await syncCursorToElement(element, resolved: resolved, snapshot: snapshot)
        return inputActions.performSecondaryAction(
            resolved: resolved, element: element, action: action
        )
    }

    @MainActor
    private func handleDrag(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let fromX: Double = req.paramDouble("from_x"),
              let fromY: Double = req.paramDouble("from_y"),
              let toX: Double = req.paramDouble("to_x"),
              let toY: Double = req.paramDouble("to_y")
        else {
            throw RPCMethodError(code: -32602, message: "Missing required drag params")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true, windowID: pinnedWindowID(req))
        let snapshot = axTree.cachedSnapshot(windowID: resolved.windowID)
            ?? axTree.walk(
                axApp: resolved.axApp,
                windowID: resolved.windowID,
                pid: resolved.pid,
                bundleID: resolved.bundleID
            )
        let bounds = snapshot.windowFrame
        _ = try await syncCursorToPoint(
            CGPoint(x: fromX, y: fromY),
            windowID: resolved.windowID,
            axBounds: bounds
        )
        let result = inputActions.drag(
            resolved: resolved,
            fromX: fromX, fromY: fromY,
            toX: toX, toY: toY
        )
        let pressLanding = lastLanding
        _ = try await syncCursorToPoint(
            CGPoint(x: toX, y: toY),
            windowID: resolved.windowID,
            axBounds: bounds,
            gate: false
        )
        var out = result
        out["cursor_landing"] = pressLanding?.dict as Any
        return out
    }
}
