import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import Logging

final class InputActions: @unchecked Sendable {
    private let logger: Logger
    var axTree: AXTreeWalker?

    init(logger: Logger) { self.logger = logger }

    // MARK: - Click

    func performPress(
        _ resolved: ResolvedApp,
        element: AXElement,
        snapshot: AXSnapshot
    ) -> [String: Any] {
        let axApp = resolved.axApp
        // Resolve the AXUIElement at the target index via BFS re-walk
        guard let target = axTree?.resolveAXUIElement(
            axApp: axApp,
            windowID: resolved.windowID,
            index: element.index
        ) else {
            return coordinateClick(
                resolved: resolved,
                point: CGPoint(
                    x: element.frame.midX,
                    y: element.frame.midY
                )
            )
        }

        // Only advertised actions. AXPress can return success as a no-op
        // when the control does not list AXPress — that is not an outcome.
        if element.actions.contains("AXPress") {
            let pressErr = AXUIElementPerformAction(target, kAXPressAction as CFString)
            if pressErr == .success {
                return [
                    "ok": true,
                    "method": "ax-press",
                    "element_index": element.index,
                ]
            }
        }

        if element.actions.contains("AXClick") {
            let clickErr = AXUIElementPerformAction(target, "AXClick" as CFString)
            if clickErr == .success {
                return [
                    "ok": true,
                    "method": "ax-click",
                    "element_index": element.index,
                ]
            }
        }

        logger.info("AX press/click not advertised or failed, falling back to CGEvent")
        return coordinateClick(
            resolved: resolved,
            point: CGPoint(x: element.frame.midX, y: element.frame.midY)
        )
    }

    func coordinateClick(resolved: ResolvedApp, point: CGPoint) -> [String: Any] {
        guard point.x.isFinite, point.y.isFinite else {
            return [
                "ok": false,
                "method": "cgevent-click",
                "error": "nonfinite click point",
            ]
        }
        let mouseDown = CGEvent(
            mouseEventSource: nil,
            mouseType: .leftMouseDown,
            mouseCursorPosition: point,
            mouseButton: .left
        )
        let mouseUp = CGEvent(
            mouseEventSource: nil,
            mouseType: .leftMouseUp,
            mouseCursorPosition: point,
            mouseButton: .left
        )
        postHid(mouseDown)
        postHid(mouseUp)

        return [
            "ok": true,
            "method": "cgevent-click",
            "point": ["x": Double(point.x), "y": Double(point.y)],
        ]
    }

    // MARK: - Key Press

    func pressKey(resolved: ResolvedApp, key: String) -> [String: Any] {
        let parsed = parseKeySpec(key)
        guard let keyCode = parsed.keyCode else {
            return ["ok": false, "error": "Unknown key: \(key)"]
        }
        if let refuse = hidFrontOrRefuse(resolved) { return refuse }

        let keyDown = CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: true)
        let keyUp = CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: false)
        keyDown?.flags = parsed.flags
        keyUp?.flags = parsed.flags
        postHid(keyDown)
        postHid(keyUp)

        return ["ok": true, "method": "cgevent-key", "key": key]
    }

    // MARK: - Type Text

    func typeText(
        resolved: ResolvedApp,
        text: String,
        afterNewDocument: Bool = false
    ) -> [String: Any] {
        if afterNewDocument, axTree?.longestTextValue() ?? 0 >= 24 {
            return [
                "ok": false,
                "error": "type_refused_stale_document",
                "method": "refused",
            ]
        }
        let areas = axTree?.liveTextAreaTargets() ?? []
        let live = axTree?.liveTextElements() ?? []
        let focused = focusedElement(resolved.axApp).flatMap {
            isTextRole($0) ? $0 : nil
        }
        let focusedIsArea = focused.map { axRole($0) == "AXTextArea" } ?? false

        if focusedIsArea, let focused, axInsertText(focused, text) {
            return ["ok": true, "method": "ax-set-value", "length": text.count]
        }
        for (el, _) in areas {
            if axInsertText(el, text) {
                return ["ok": true, "method": "ax-set-value", "length": text.count]
            }
        }
        if let focused, !focusedIsArea, axInsertText(focused, text) {
            return ["ok": true, "method": "ax-set-value", "length": text.count]
        }
        for el in live {
            if axInsertText(el, text) {
                return ["ok": true, "method": "ax-set-value", "length": text.count]
            }
        }

        if let (_, frame) = areas.first,
           frame.width > 1, frame.height > 1, frame.origin.x.isFinite,
           frame.origin.y.isFinite {
            _ = coordinateClick(
                resolved: resolved,
                point: CGPoint(x: frame.midX, y: frame.midY)
            )
            if let refuse = hidFrontOrRefuse(resolved) { return refuse }
            hidTypeUnicode(text)
            return ["ok": true, "method": "cgevent-type", "length": text.count]
        }
        if focused != nil || !live.isEmpty {
            if let refuse = hidFrontOrRefuse(resolved) { return refuse }
            hidTypeUnicode(text)
            return ["ok": true, "method": "cgevent-type", "length": text.count]
        }
        return [
            "ok": false,
            "error": "type_no_text_target",
            "method": "refused",
        ]
    }

    private func focusedElement(_ axApp: AXUIElement) -> AXUIElement? {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(
            axApp, kAXFocusedUIElementAttribute as CFString, &ref
        )
        return ref.map { $0 as! AXUIElement }
    }

    private func axRole(_ el: AXUIElement) -> String {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(el, kAXRoleAttribute as CFString, &ref)
        return (ref as? String) ?? ""
    }

    private func isTextRole(_ el: AXUIElement) -> Bool {
        ["AXTextArea", "AXTextField", "AXComboBox", "AXSearchField"]
            .contains(axRole(el))
    }

    /// Caret insert. SelectedText first — splicing AXValue fails when the
    /// control stores an attributed string. Dispatch success is not landing:
    /// the string (or attributed) AXValue must contain the insert.
    private func axInsertText(_ focused: AXUIElement, _ text: String) -> Bool {
        let before = axStringValue(focused)
        let selected = AXUIElementSetAttributeValue(
            focused,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        )
        if selected == .success {
            return axInsertLanded(focused, text, before: before)
        }

        guard let current = before else { return false }
        let ns = current as NSString
        var loc = ns.length
        var len = 0
        var rangeRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(
            focused, kAXSelectedTextRangeAttribute as CFString, &rangeRef
        ) == .success,
           let rangeRef,
           CFGetTypeID(rangeRef) == AXValueGetTypeID() {
            var r = CFRange()
            if AXValueGetValue(rangeRef as! AXValue, .cfRange, &r) {
                loc = max(0, min(r.location, ns.length))
                len = max(0, min(r.length, ns.length - loc))
            }
        }
        let next = ns.replacingCharacters(
            in: NSRange(location: loc, length: len),
            with: text
        )
        guard AXUIElementSetAttributeValue(
            focused,
            kAXValueAttribute as CFString,
            next as CFTypeRef
        ) == .success else { return false }
        return axInsertLanded(focused, text, before: before)
    }

    private func axStringValue(_ el: AXUIElement) -> String? {
        var valueRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            el, kAXValueAttribute as CFString, &valueRef
        ) == .success, let valueRef else { return nil }
        if let s = valueRef as? String { return s }
        if let a = valueRef as? NSAttributedString { return a.string }
        return nil
    }

    private func axInsertLanded(
        _ el: AXUIElement, _ text: String, before: String?
    ) -> Bool {
        guard let after = axStringValue(el) else { return false }
        if after.contains(text) { return true }
        if let before, after.count >= before.count + text.count { return true }
        return false
    }

    /// CGEvent unicode payload is capped (~20 UTF-16 units). No per-char 10ms wait.
    private func hidTypeUnicode(_ text: String) {
        let units = Array(text.utf16)
        var i = 0
        let cap = 20
        while i < units.count {
            let end = min(i + cap, units.count)
            let slice = Array(units[i..<end])
            let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
            down?.keyboardSetUnicodeString(
                stringLength: slice.count, unicodeString: slice
            )
            postHid(down)
            let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
            up?.keyboardSetUnicodeString(
                stringLength: slice.count, unicodeString: slice
            )
            postHid(up)
            i = end
        }
    }

    // MARK: - Scroll

    func scroll(
        resolved: ResolvedApp,
        element: AXElement?,
        direction: String,
        pages: Int
    ) -> [String: Any] {
        let dy: Int32
        switch direction.lowercased() {
        case "up", "u": dy = Int32(pages * 5)
        case "down", "d": dy = Int32(-pages * 5)
        default: return ["ok": false, "error": "Invalid direction: \(direction)"]
        }

        let point = element.map {
            CGPoint(x: $0.frame.midX, y: $0.frame.midY)
        } ?? CGPoint(x: 400, y: 400)

        // Move mouse to scroll position first
        let move = CGEvent(
            mouseEventSource: nil,
            mouseType: .mouseMoved,
            mouseCursorPosition: point,
            mouseButton: .left
        )
        postHid(move)

        let scroll = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: dy, wheel2: 0, wheel3: 0)
        postHid(scroll)

        return ["ok": true, "method": "cgevent-scroll", "direction": direction, "pages": pages]
    }

    // MARK: - Set Value

    func setValue(
        resolved: ResolvedApp,
        element: AXElement,
        value: String
    ) -> [String: Any] {
        guard let axEl = axTree?.resolveAXUIElement(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            index: element.index
        ) else {
            return ["ok": false, "error": "Element not found"]
        }

        let err = AXUIElementSetAttributeValue(
            axEl,
            kAXValueAttribute as CFString,
            value as CFTypeRef
        )
        if err == .success {
            return ["ok": true, "method": "ax-set-value"]
        }
        return ["ok": false, "error": "AX set value failed: \(err.rawValue)"]
    }

    // MARK: - Select Text

    func selectText(
        resolved: ResolvedApp,
        element: AXElement,
        text: String,
        prefix: String?,
        suffix: String?,
        selectionType: String?
    ) -> [String: Any] {
        guard let axEl = axTree?.resolveAXUIElement(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            index: element.index
        ) else {
            return ["ok": false, "error": "Element not found"]
        }

        var valueRef: CFTypeRef?
        AXUIElementCopyAttributeValue(axEl, kAXValueAttribute as CFString, &valueRef)
        guard let fullText = valueRef as? String else {
            return ["ok": false, "error": "Element has no text value"]
        }

        guard let range = fullText.range(of: text) else {
            return ["ok": false, "error": "Text not found in element"]
        }

        let start = fullText.distance(from: fullText.startIndex, to: range.lowerBound)
        let length = text.count

        var cfRange = CFRangeMake(start, length)
        let axRange = AXValueCreate(.cfRange, &cfRange)!
        let err = AXUIElementSetAttributeValue(
            axEl,
            kAXSelectedTextRangeAttribute as CFString,
            axRange
        )

        return [
            "ok": err == .success,
            "method": "ax-select-text",
            "start": start,
            "length": length,
        ]
    }

    // MARK: - Drag

    func drag(
        resolved: ResolvedApp,
        fromX: Double, fromY: Double,
        toX: Double, toY: Double
    ) -> [String: Any] {
        let from = CGPoint(x: fromX, y: fromY)
        let to = CGPoint(x: toX, y: toY)

        let down = CGEvent(
            mouseEventSource: nil,
            mouseType: .leftMouseDown,
            mouseCursorPosition: from,
            mouseButton: .left
        )
        postHid(down)

        // Interpolate drag path
        let steps = 10
        for i in 1...steps {
            let t = Double(i) / Double(steps)
            let pt = CGPoint(
                x: from.x + (to.x - from.x) * t,
                y: from.y + (to.y - from.y) * t
            )
            let move = CGEvent(
                mouseEventSource: nil,
                mouseType: .leftMouseDragged,
                mouseCursorPosition: pt,
                mouseButton: .left
            )
            postHid(move)
            Thread.sleep(forTimeInterval: 0.02)
        }

        let up = CGEvent(
            mouseEventSource: nil,
            mouseType: .leftMouseUp,
            mouseCursorPosition: to,
            mouseButton: .left
        )
        postHid(up)

        return ["ok": true, "method": "cgevent-drag"]
    }

    // MARK: - Secondary Action

    func performSecondaryAction(
        resolved: ResolvedApp,
        element: AXElement,
        action: String
    ) -> [String: Any] {
        guard let axEl = axTree?.resolveAXUIElement(
            axApp: resolved.axApp,
            windowID: resolved.windowID,
            index: element.index
        ) else {
            return ["ok": false, "error": "Element not found"]
        }

        let err = AXUIElementPerformAction(axEl, action as CFString)
        return [
            "ok": err == .success,
            "method": "ax-secondary-action",
            "action": action,
            "error_code": err.rawValue,
        ]
    }

    // MARK: - Helpers

    /// HID at the AX/CG point. PID-only misses some compositors; PID+HID
    /// duplicates every key (doubled glyphs). Not a desktop-global hunt.
    private func postHid(_ event: CGEvent?) {
        event?.post(tap: .cghidEventTap)
    }

    private func hidFrontOrRefuse(_ resolved: ResolvedApp) -> [String: Any]? {
        if AppResolver.hidFrontPid() == resolved.pid { return nil }
        return [
            "ok": false,
            "error": "key_target_not_front",
            "method": "refused",
        ]
    }

    // MARK: - Key Parsing

    struct ParsedKey {
        var keyCode: CGKeyCode?
        var flags: CGEventFlags
    }

    private func parseKeySpec(_ spec: String) -> ParsedKey {
        let parts = spec.split(separator: "+").map { String($0).trimmingCharacters(in: .whitespaces) }
        var flags = CGEventFlags()
        var keyName = spec

        if parts.count > 1 {
            for part in parts.dropLast() {
                switch part.lowercased() {
                case "super", "command", "cmd": flags.insert(.maskCommand)
                case "shift": flags.insert(.maskShift)
                case "alt", "option": flags.insert(.maskAlternate)
                case "ctrl", "control": flags.insert(.maskControl)
                default: break
                }
            }
            keyName = parts.last!
        }

        let code = keyCodeMap[keyName.lowercased()] ?? keyCodeMap[keyName]
        return ParsedKey(keyCode: code, flags: flags)
    }

    private let keyCodeMap: [String: CGKeyCode] = [
        "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31,
        "delete": 0x33, "backspace": 0x33, "escape": 0x35, "esc": 0x35,
        "up": 0x7E, "down": 0x7D, "left": 0x7B, "right": 0x7C,
        "home": 0x73, "end": 0x77, "pageup": 0x74, "page_up": 0x74,
        "pagedown": 0x79, "page_down": 0x79,
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
        "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
        "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
        "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02,
        "e": 0x0E, "f": 0x03, "g": 0x05, "h": 0x04,
        "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25,
        "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23,
        "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11,
        "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
        "y": 0x10, "z": 0x06,
        "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14,
        "4": 0x15, "5": 0x17, "6": 0x16, "7": 0x1A,
        "8": 0x1C, "9": 0x19,
        "kp_0": 0x52, "kp_1": 0x53, "kp_2": 0x54, "kp_3": 0x55,
        "kp_4": 0x56, "kp_5": 0x57, "kp_6": 0x58, "kp_7": 0x59,
        "kp_8": 0x5B, "kp_9": 0x5C,
        "minus": 0x1B, "equal": 0x18, "bracketleft": 0x21,
        "bracketright": 0x1E, "backslash": 0x2A, "semicolon": 0x29,
        "apostrophe": 0x27, "grave": 0x32, "comma": 0x2B,
        "period": 0x2F, "slash": 0x2C,
    ]
}
