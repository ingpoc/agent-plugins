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
            return try handlePressKey(req)

        case "type_text":
            return try handleTypeText(req)

        case "scroll":
            return try handleScroll(req)

        case "set_value":
            return try handleSetValue(req)

        case "select_text":
            return try handleSelectText(req)

        case "perform_secondary_action":
            return try handleSecondaryAction(req)

        case "drag":
            return try handleDrag(req)

        default:
            throw RPCMethodError(
                code: -32601,
                message: "Method not found: \(req.method)"
            )
        }
    }

    // MARK: - get_app_state

    @MainActor
    private func handleGetAppState(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app") else {
            throw RPCMethodError(code: -32602, message: "Missing 'app' parameter")
        }
        let disableDiff: Bool = req.param("disableDiff") ?? false
        let t0 = ProcessInfo.processInfo.systemUptime
        let resolved0 = try appResolver.resolve(app)
        var resolved = resolved0
        let t1 = ProcessInfo.processInfo.systemUptime
        var snapshot = axTree.walk(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            maxElements: req.paramInt("maxElements") ?? 80,
            disableDiff: disableDiff
        )
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
            resolved = try appResolver.resolve(app)
            snapshot = axTree.walk(
                axApp: resolved.axApp,
                windowID: resolved.windowID,
                maxElements: req.paramInt("maxElements") ?? 80,
                disableDiff: disableDiff
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
        if let screenshotPath {
            result["screenshot"] = ["url": "file://\(screenshotPath)"]
        }
        return result
    }

    // MARK: - click

    @MainActor
    private func handleClick(_ req: JSONRPCRequest) async throws -> Any {
        guard let app: String = req.param("app") else {
            throw RPCMethodError(code: -32602, message: "Missing 'app' parameter")
        }
        let resolved = try appResolver.resolve(app)
        screenshotCapture.invalidate()
        let snapshot = axTree.cachedSnapshot(windowID: resolved.windowID)
            ?? axTree.walk(axApp: resolved.axApp, windowID: resolved.windowID)

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
        let tip = cursorOverlay.glideTo(
            screenPoint: clickPoint,
            windowID: resolved.windowID,
            axBounds: snapshot.windowFrame
        )
        if tip.wait > 0 {
            try await Task.sleep(
                nanoseconds: UInt64((tip.wait * 1_000_000_000).rounded())
            )
        }

        let pressResult: [String: Any]
        if let target {
            pressResult = inputActions.performPress(
                resolved, element: target, snapshot: snapshot
            )
        } else {
            pressResult = inputActions.coordinateClick(
                resolved: resolved, point: tip.point
            )
        }

        let method = pressResult["method"] as? String
        if method == "ax-press" || method == "ax-click"
            || method == "ax-set-value" || method == "cgevent-click" {
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

        var result = pressResult
        result["settled"] = settled.dict
        result["label"] = label as Any
        result["element_index"] = (target?.index ?? elementIndex) as Any
        return result
    }

    // MARK: - Input methods

    private func handlePressKey(_ req: JSONRPCRequest) throws -> Any {
        guard let app: String = req.param("app"),
              let key: String = req.param("key")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or key")
        }
        let resolved = try appResolver.resolve(app, raiseForInput: true)
        screenshotCapture.invalidate()
        let pressed = inputActions.pressKey(resolved: resolved, key: key)
        let keyNorm = key.lowercased().replacingOccurrences(of: " ", with: "")
        if ["cmd+n", "command+n", "cmd+t", "command+t"].contains(keyNorm) {
            appResolver.invalidateWindowCache()
        }
        return pressed
    }

    private func handleTypeText(_ req: JSONRPCRequest) throws -> Any {
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
        _ = axTree.walk(axApp: resolved.axApp, windowID: resolved.windowID)
        return inputActions.typeText(
            resolved: resolved, text: text, afterNewDocument: afterNew
        )
    }

    private func handleScroll(_ req: JSONRPCRequest) throws -> Any {
        guard let app: String = req.param("app"),
              let direction: String = req.param("direction")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app or direction")
        }
        let resolved = try appResolver.resolve(app)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp, windowID: resolved.windowID
        )
        let element: AXElement?
        if let idx: Int = req.paramInt("element_index") {
            element = axTree.findElementByIndex(snapshot, index: idx)
        } else {
            element = nil
        }
        let pages: Int = req.paramInt("pages") ?? 1
        return inputActions.scroll(
            resolved: resolved,
            element: element,
            direction: direction,
            pages: pages
        )
    }

    private func handleSetValue(_ req: JSONRPCRequest) throws -> Any {
        guard let app: String = req.param("app"),
              let idx = req.paramInt("element_index"),
              let value: String = req.param("value")
        else {
            throw RPCMethodError(code: -32602, message: "Missing app, element_index, or value")
        }
        let resolved = try appResolver.resolve(app)
        screenshotCapture.invalidate()
        let snapshot = axTree.walk(
            axApp: resolved.axApp, windowID: resolved.windowID
        )
        guard let element = axTree.findElementByIndex(snapshot, index: idx) else {
            throw RPCMethodError(code: -32602, message: "Element not found at index \(idx)")
        }
        return inputActions.setValue(
            resolved: resolved, element: element, value: value
        )
    }

    private func handleSelectText(_ req: JSONRPCRequest) throws -> Any {
        guard let app: String = req.param("app"),
              let idx = req.paramInt("element_index"),
              let text: String = req.param("text")
        else {
            throw RPCMethodError(code: -32602, message: "Missing required params")
        }
        let resolved = try appResolver.resolve(app)
        let snapshot = axTree.walk(
            axApp: resolved.axApp, windowID: resolved.windowID
        )
        guard let element = axTree.findElementByIndex(snapshot, index: idx) else {
            throw RPCMethodError(code: -32602, message: "Element not found at index \(idx)")
        }
        return inputActions.selectText(
            resolved: resolved,
            element: element,
            text: text,
            prefix: req.param("prefix"),
            suffix: req.param("suffix"),
            selectionType: req.param("selection_type")
        )
    }

    private func handleSecondaryAction(_ req: JSONRPCRequest) throws -> Any {
        guard let app: String = req.param("app"),
              let idx = req.paramInt("element_index"),
              let action: String = req.param("action")
        else {
            throw RPCMethodError(code: -32602, message: "Missing required params")
        }
        let resolved = try appResolver.resolve(app)
        let snapshot = axTree.walk(
            axApp: resolved.axApp, windowID: resolved.windowID
        )
        guard let element = axTree.findElementByIndex(snapshot, index: idx) else {
            throw RPCMethodError(code: -32602, message: "Element not found at index \(idx)")
        }
        return inputActions.performSecondaryAction(
            resolved: resolved, element: element, action: action
        )
    }

    private func handleDrag(_ req: JSONRPCRequest) throws -> Any {
        guard let app: String = req.param("app"),
              let fromX: Double = req.paramDouble("from_x"),
              let fromY: Double = req.paramDouble("from_y"),
              let toX: Double = req.paramDouble("to_x"),
              let toY: Double = req.paramDouble("to_y")
        else {
            throw RPCMethodError(code: -32602, message: "Missing required drag params")
        }
        let resolved = try appResolver.resolve(app)
        return inputActions.drag(
            resolved: resolved,
            fromX: fromX, fromY: fromY,
            toX: toX, toY: toY
        )
    }
}
