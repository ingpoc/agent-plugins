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

    @MainActor
    func handle(_ request: JSONRPCRequest) async -> JSONRPCResponse {
        // ponytail: desktop input is one shared resource; split lanes only if
        // independent displays become a measured concurrent use case.
        while requestRunning {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        requestRunning = true
        defer { requestRunning = false }
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
        switch req.method {
        case "list_apps":
            return appResolver.listApps()

        case "get_app_state":
            return try await handleGetAppState(req)

        case "click":
            return try await handleClick(req)

        case "press_key":
            return try await handlePressKey(req)

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
    private func handleExecutePlan(_ req: JSONRPCRequest) async throws -> Any {
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
        let before = await planState(app: app)
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
            let child = JSONRPCRequest(
                jsonrpc: "2.0",
                method: method,
                params: params.mapValues(AnyCodable.init),
                id: nil
            )
            let value = try await dispatch(child)
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
        let after = await planState(app: app)
        return [
            "ok": results.count == steps.count && results.allSatisfy { $0["ok"] as? Bool == true },
            "before": before,
            "after": after,
            "results": results,
        ] as [String: Any]
    }

    @MainActor
    private func planState(app: String) async -> [String: Any] {
        let request = JSONRPCRequest(
            jsonrpc: "2.0",
            method: "get_app_state",
            params: [
                "app": AnyCodable(app),
                "disableDiff": AnyCodable(true),
            ],
            id: nil
        )
        do {
            return try await handleGetAppState(request) as? [String: Any] ?? [:]
        } catch {
            return ["text": "", "error": error.localizedDescription]
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
        let t0 = ProcessInfo.processInfo.systemUptime
        let resolved0 = try appResolver.resolve(app, raiseForInput: raiseForInput)
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
        var shot = await screenshotCapture.capture(
            windowID: resolved.windowID,
            axBounds: snapshot.windowFrame
        )
        var screenshotPath = shot.path
        var captureCached = shot.cached
        var captureRetry = false
        if screenshotPath == nil {
            captureRetry = true
            try await Task.sleep(nanoseconds: 120_000_000)
            resolved = try appResolver.resolve(app, raiseForInput: raiseForInput)
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
        if let enrich {
            result["ax_enrich"] = enrich
        }
        if let screenshotPath {
            result["screenshot"] = ["url": "file://\(screenshotPath)"]
        }
        if raiseForInput {
            try await syncCursorToWindow(resolved: resolved, snapshot: snapshot)
        }
        return result
    }

    // MARK: - Cursor sync (agent pointer follows every input surface)

    @MainActor
    private func syncCursorToPoint(
        _ point: CGPoint,
        windowID: CGWindowID,
        axBounds: CGRect?
    ) async throws -> CGPoint {
        let tip = cursorOverlay.glideTo(
            screenPoint: point,
            windowID: windowID,
            axBounds: axBounds
        )
        if tip.wait > 0 {
            try await Task.sleep(
                nanoseconds: UInt64((tip.wait * 1_000_000_000).rounded())
            )
        }
        return tip.point
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
        snapshot: AXSnapshot
    ) async throws {
        guard let frame = snapshot.windowFrame,
              frame.width > 0, frame.height > 0 else { return }
        _ = try await syncCursorToPoint(
            CGPoint(x: frame.midX, y: frame.midY),
            windowID: resolved.windowID,
            axBounds: frame
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
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        screenshotCapture.invalidate()
        let snapshot = axTree.cachedSnapshot(windowID: resolved.windowID)
            ?? axTree.walk(
                axApp: resolved.axApp,
                windowID: resolved.windowID,
                pid: resolved.pid,
                bundleID: resolved.bundleID
            )

        let label: String? = req.param("label")
        let elementIndex: Int? = req.paramInt("element_index")
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

        let pressResult: [String: Any]
        if let target {
            pressResult = inputActions.performPress(
                resolved, element: target, snapshot: snapshot
            )
        } else {
            pressResult = inputActions.coordinateClick(
                resolved: resolved, point: aimPoint
            )
        }

        let method = pressResult["method"] as? String
        if method == "ax-press" || method == "ax-click"
            || method == "ax-set-value" || method == "cgevent-click"
            || method == "cgevent-click-pid" {
            var result = pressResult
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
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        try await syncCursorToWindow(resolved: resolved, snapshot: snapshot)
        let pressed = inputActions.pressKey(resolved: resolved, key: key)
        let keyNorm = key.lowercased().replacingOccurrences(of: " ", with: "")
        if ["cmd+n", "command+n", "cmd+t", "command+t"].contains(keyNorm) {
            appResolver.invalidateWindowCache()
        }
        return pressed
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
            app, raiseForInput: true, preferFocusedWindow: afterNew
        )
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            pid: resolved.pid,
            bundleID: resolved.bundleID
        )
        try await syncCursorToFocusedInput(resolved: resolved, snapshot: snapshot)
        return inputActions.typeText(
            resolved: resolved, text: text, afterNewDocument: afterNew
        )
    }

    @MainActor
    private func handleScroll(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app"),
              let direction: String = req.param("direction")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or direction")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
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
            pages: pages
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
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
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
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
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
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        let snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
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
        let resolved = try appResolver.resolve(app, raiseForInput: true)
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
        _ = try await syncCursorToPoint(
            CGPoint(x: toX, y: toY),
            windowID: resolved.windowID,
            axBounds: bounds
        )
        return result
    }
}
