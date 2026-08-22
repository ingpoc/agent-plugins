import AppKit
import ApplicationServices
import Foundation
import Logging

struct AXElement: Sendable {
    let index: Int
    let role: String
    let label: String?
    let value: String?
    let frame: CGRect
    let actions: [String]
    let children: [Int]
    let parentIndex: Int?
    let depth: Int
}

struct AXSnapshot: Sendable {
    let elements: [AXElement]
    let markdown: String
    let windowFrame: CGRect?
    let timestamp: TimeInterval
}

final class AXTreeWalker: @unchecked Sendable {
    private let logger: Logger
    private var previousSnapshot: AXSnapshot?
    private var liveElements: [AXUIElement] = []
    private var liveWindowID: CGWindowID = 0
    private var liveAt: TimeInterval = 0
    private let lock = NSLock()
    private let chromiumAX: ChromiumAXEnabler

    init(logger: Logger) {
        self.logger = logger
        self.chromiumAX = ChromiumAXEnabler(logger: logger)
    }

    /// Drop live AX refs + snapshot (window switch / input invalidate).
    func invalidateCaches() {
        lock.lock()
        liveElements.removeAll(keepingCapacity: false)
        previousSnapshot = nil
        liveWindowID = 0
        liveAt = 0
        lock.unlock()
    }

    func walk(
        axApp: AXUIElement,
        windowID: CGWindowID,
        maxElements: Int = 80,
        disableDiff: Bool = false,
        pid: pid_t? = nil,
        bundleID: String? = nil
    ) -> AXSnapshot {
        var snapshot = walkOnce(
            axApp: axApp,
            windowID: windowID,
            maxElements: maxElements,
            disableDiff: disableDiff
        )
        if let pid,
           let method = chromiumAX.enrichIfNeeded(
            axApp: axApp,
            pid: pid,
            bundleID: bundleID,
            snapshot: snapshot
           ) {
            // Cursor/Electron rebuild AX asynchronously; measured ~2s after
            // ManualAccessibility + activate before WebArea children appear.
            if let app = NSRunningApplication(processIdentifier: pid) {
                app.activate(options: [.activateAllWindows])
            }
            var best = snapshot
            let deadline = ProcessInfo.processInfo.systemUptime + 2.5
            while ProcessInfo.processInfo.systemUptime < deadline {
                Thread.sleep(forTimeInterval: 0.2)
                let again = walkOnce(
                    axApp: axApp,
                    windowID: windowID,
                    maxElements: maxElements,
                    disableDiff: disableDiff
                )
                if again.elements.count > best.elements.count {
                    best = again
                }
                if !ChromiumAXEnabler.isSparse(again) { break }
            }
            snapshot = best
            lock.lock()
            lastEnrichMethod = method
            lock.unlock()
            if ChromiumAXEnabler.isSparse(snapshot) {
                chromiumAX.clearAttempt(pid)
            }
        } else {
            lock.lock()
            lastEnrichMethod = nil
            lock.unlock()
        }
        return snapshot
    }

    /// Enrich method from the last `walk` (nil if not applied).
    private(set) var lastEnrichMethod: String?

    private func walkOnce(
        axApp: AXUIElement,
        windowID: CGWindowID,
        maxElements: Int,
        disableDiff: Bool
    ) -> AXSnapshot {
        var elements: [AXElement] = []
        var queue: [(AXUIElement, Int?, Int)] = []
        var live: [AXUIElement] = []

        lock.lock()
        if liveWindowID != 0, liveWindowID != windowID {
            liveElements.removeAll(keepingCapacity: false)
            previousSnapshot = nil
            liveAt = 0
        }
        lock.unlock()

        // Find the target window or walk from app root
        let roots = walkRoots(axApp: axApp, windowID: windowID)
        let axWinFrame = roots.first.map { axFrame($0) }.flatMap { rect in
            rect.width > 0 && rect.height > 0 ? rect : nil
        }
        let cgFrame = quartzWindowBounds(windowID: windowID)
        let windowFrame: CGRect?
        if let axWinFrame {
            let axArea = axWinFrame.width * axWinFrame.height
            let cgArea = (cgFrame?.width ?? 0) * (cgFrame?.height ?? 0)
            // Stage Manager / window-server proxies report a tiny CGWindow
            // while AX has the real 230×408 (etc.) frame. Prefer AX.
            windowFrame = cgArea >= axArea * 0.5 ? (cgFrame ?? axWinFrame) : axWinFrame
        } else {
            windowFrame = cgFrame
        }
        for root in roots {
            queue.append((root, nil, 0))
        }

        var visited = 0
        while !queue.isEmpty && elements.count < maxElements && visited < 400 {
            visited += 1
            let (el, parentIdx, depth) = queue.removeFirst()
            let idx = elements.count
            let packed = axPacked(el)
            let roleName = packed.role ?? "AXUnknown"
            let label = axNorm(packed.title ?? packed.desc)
            let value = axNorm(packed.value)
            let frame = packed.frame
            let childElements = packed.children
            let keep = axKeepNode(
                role: roleName, frame: frame, depth: depth, windowFrame: windowFrame
            )
            if !keep {
                continue
            }
            let skipChrome: Set<String> = [
                "AXRow", "AXCell", "AXGroup", "AXColumn",
                "AXOutline", "AXScrollArea", "AXSplitGroup",
            ]
            if skipChrome.contains(roleName), label == nil, value == nil {
                for child in childElements {
                    queue.append((child, parentIdx, depth + 1))
                }
                continue
            }
            let actions = axActions(el)
            if !axEmitNode(
                role: roleName, label: label, value: value, actions: actions
            ) {
                for child in childElements {
                    queue.append((child, parentIdx, depth + 1))
                }
                continue
            }

            live.append(el)
            elements.append(AXElement(
                index: idx,
                role: roleName,
                label: label,
                value: value,
                frame: frame,
                actions: actions,
                children: [],
                parentIndex: parentIdx,
                depth: depth
            ))

            for child in childElements {
                queue.append((child, idx, depth + 1))
            }
        }

        // Build markdown representation
        let md = buildMarkdown(elements)

        let snapshot = AXSnapshot(
            elements: elements,
            markdown: md,
            windowFrame: windowFrame,
            timestamp: ProcessInfo.processInfo.systemUptime
        )

        lock.lock()
        liveElements = live
        liveWindowID = windowID
        liveAt = snapshot.timestamp
        previousSnapshot = snapshot
        lock.unlock()

        return snapshot
    }

    func findElementByLabel(_ snapshot: AXSnapshot, label: String) -> AXElement? {
        let needle = (axNorm(label) ?? "").lowercased()
        guard !needle.isEmpty else { return nil }
        if let exact = snapshot.elements.first(where: {
            ($0.label).map { (axNorm($0) ?? "").lowercased() == needle } == true
        }) {
            return exact
        }
        if let partial = snapshot.elements.first(where: {
            ($0.label).map { (axNorm($0) ?? "").lowercased().contains(needle) } == true
        }) {
            return partial
        }
        if let val = snapshot.elements.first(where: {
            ($0.value).map { (axNorm($0) ?? "").lowercased().contains(needle) } == true
        }) {
            return val
        }
        return nil
    }

    func findElementByIndex(_ snapshot: AXSnapshot, index: Int) -> AXElement? {
        guard index >= 0 && index < snapshot.elements.count else { return nil }
        return snapshot.elements[index]
    }

    /// Reuse the last walk for the same window (batched click after state).
    func cachedSnapshot(windowID: CGWindowID, maxAge: TimeInterval = 0.4) -> AXSnapshot? {
        lock.lock()
        defer { lock.unlock() }
        guard liveWindowID == windowID, let snap = previousSnapshot else { return nil }
        if ProcessInfo.processInfo.systemUptime - liveAt > maxAge { return nil }
        return snap
    }

    /// Resolve the live AXUIElement at a given BFS index.
    /// Resolve a live AXUIElement by BFS index. Uses the same root selection
    /// as `walk()` (walkRoots → surfaces/popovers → focused window) so indices match.
    func resolveAXUIElement(
        axApp: AXUIElement,
        windowID: CGWindowID,
        index: Int
    ) -> AXUIElement? {
        lock.lock()
        let cached = (index >= 0 && index < liveElements.count) ? liveElements[index] : nil
        lock.unlock()
        if let cached { return cached }
        let roots = walkRoots(axApp: axApp, windowID: windowID)
        var queue: [AXUIElement] = roots
        var visited = 0
        while !queue.isEmpty {
            let el = queue.removeFirst()
            if visited == index { return el }
            visited += 1
            queue.append(contentsOf: axChildren(el))
        }
        return nil
    }

    func liveTextElements() -> [AXUIElement] {
        lock.lock()
        defer { lock.unlock() }
        guard let snap = previousSnapshot else { return [] }
        let roles: Set<String> = [
            "AXTextArea", "AXTextField", "AXComboBox", "AXSearchField",
        ]
        let n = min(liveElements.count, snap.elements.count)
        var areas: [AXUIElement] = []
        var others: [AXUIElement] = []
        for i in 0..<n {
            let role = snap.elements[i].role
            guard roles.contains(role) else { continue }
            if role == "AXTextArea" {
                areas.append(liveElements[i])
            } else {
                others.append(liveElements[i])
            }
        }
        return areas + others
    }

    /// AXTextArea refs + frames for type_text (editor over search field).
    func liveTextAreaTargets() -> [(AXUIElement, CGRect)] {
        lock.lock()
        defer { lock.unlock() }
        guard let snap = previousSnapshot else { return [] }
        let n = min(liveElements.count, snap.elements.count)
        var out: [(AXUIElement, CGRect)] = []
        for i in 0..<n where snap.elements[i].role == "AXTextArea" {
            out.append((liveElements[i], snap.elements[i].frame))
        }
        return out
    }

    /// Longest AXTextArea/AXTextField value in the last walk.
    func longestTextValue() -> Int {
        lock.lock()
        defer { lock.unlock() }
        guard let snap = previousSnapshot else { return 0 }
        return snap.elements
            .filter { $0.role == "AXTextArea" || $0.role == "AXTextField" }
            .compactMap { $0.value?.count }
            .max() ?? 0
    }

    // MARK: - AX Helpers

    /// Match the CG window. App-root fallback walks the menu bar (banned).
    private func walkRoots(axApp: AXUIElement, windowID: CGWindowID) -> [AXUIElement] {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &ref)
        let windows = (ref as? [AXUIElement]) ?? []
        for win in windows {
            var id: CGWindowID = 0
            if _CGSGetWindowID(win, &id) == .success, id == windowID {
                let surfaces = axSurfaces(win)
                return surfaces.isEmpty ? [win] : surfaces
            }
        }
        AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &ref)
        if let focusedWin = ref {
            let focused = focusedWin as! AXUIElement
            let surfaces = axSurfaces(focused)
            return surfaces.isEmpty ? [focused] : surfaces
        }
        if let first = windows.first {
            return [first]
        }
        return []
    }

    private func axSurfaces(_ window: AXUIElement) -> [AXUIElement] {
        let surfaceRoles: Set<String> = ["AXSheet", "AXPopover", "AXDialog"]
        var result: [AXUIElement] = []
        guard let children = axChildren(window) as [AXUIElement]? else { return result }
        for child in children {
            if let role = axStringAttr(child, kAXRoleAttribute),
               surfaceRoles.contains(role) {
                result.append(child)
            }
        }
        return result
    }

    /// Off-window carousel chips consume the 80-node budget. Keep chrome,
    /// zero-size groups, and popovers/menus (often just outside the window).
    private func axKeepNode(
        role: String, frame: CGRect, depth: Int, windowFrame: CGRect?
    ) -> Bool {
        if depth == 0 { return true }
        if ["AXPopover", "AXSheet", "AXDialog", "AXMenu", "AXMenuItem"].contains(role) {
            return true
        }
        if frame.width <= 1 || frame.height <= 1 { return true }
        guard let windowFrame else { return true }
        let pad = windowFrame.insetBy(dx: -120, dy: -120)
        return pad.intersects(frame)
    }

    /// Empty outline rows ate the 80-node budget; sidebar labels never appeared.
    private func axEmitNode(
        role: String, label: String?, value: String?, actions: [String]
    ) -> Bool {
        if label != nil || value != nil { return true }
        if actions.contains("AXPress") || actions.contains("AXConfirm") {
            return true
        }
        let skip: Set<String> = [
            "AXRow", "AXCell", "AXGroup", "AXColumn",
            "AXOutline", "AXScrollArea", "AXSplitGroup",
        ]
        return !skip.contains(role)
    }

    /// One AX IPC for role/title/description/value/frame/children.
    private func axPacked(_ el: AXUIElement) -> (
        role: String?,
        title: String?,
        desc: String?,
        value: String?,
        frame: CGRect,
        children: [AXUIElement]
    ) {
        let names: [CFString] = [
            kAXRoleAttribute as CFString,
            kAXTitleAttribute as CFString,
            kAXDescriptionAttribute as CFString,
            kAXValueAttribute as CFString,
            kAXPositionAttribute as CFString,
            kAXSizeAttribute as CFString,
            kAXChildrenAttribute as CFString,
        ]
        var values: CFArray?
        let err = AXUIElementCopyMultipleAttributeValues(
            el,
            names as CFArray,
            AXCopyMultipleAttributeOptions(rawValue: 0),
            &values
        )
        guard err == .success, let arr = values as? [Any], arr.count >= 7 else {
            return (
                axStringAttr(el, kAXRoleAttribute),
                axStringAttr(el, kAXTitleAttribute),
                axStringAttr(el, kAXDescriptionAttribute),
                axStringAttr(el, kAXValueAttribute),
                axFrame(el),
                axChildren(el)
            )
        }
        return (
            axAnyString(arr[0]),
            axAnyString(arr[1]),
            axAnyString(arr[2]),
            axAnyString(arr[3]),
            CGRect(origin: axAnyPoint(arr[4]), size: axAnySize(arr[5])),
            (arr[6] as? [AXUIElement]) ?? []
        )
    }

    private func axAnyString(_ value: Any) -> String? {
        if value is NSNull { return nil }
        if let s = value as? String, !s.isEmpty { return s }
        if let a = value as? NSAttributedString, !a.string.isEmpty {
            return a.string
        }
        return nil
    }

    private func axAnyPoint(_ value: Any) -> CGPoint {
        var point = CGPoint.zero
        let cf = value as CFTypeRef
        guard CFGetTypeID(cf) == AXValueGetTypeID() else { return point }
        let axVal = value as! AXValue
        if AXValueGetType(axVal) == .cgPoint {
            AXValueGetValue(axVal, .cgPoint, &point)
        }
        return point
    }

    private func axAnySize(_ value: Any) -> CGSize {
        var size = CGSize.zero
        let cf = value as CFTypeRef
        guard CFGetTypeID(cf) == AXValueGetTypeID() else { return size }
        let axVal = value as! AXValue
        if AXValueGetType(axVal) == .cgSize {
            AXValueGetValue(axVal, .cgSize, &size)
        }
        return size
    }

    private func axStringAttr(_ el: AXUIElement, _ attr: String) -> String? {
        var ref: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(el, attr as CFString, &ref)
        guard err == .success, let val = ref else { return nil }
        if let s = val as? String, !s.isEmpty { return s }
        return nil
    }

    private func axNorm(_ s: String?) -> String? {
        guard let s else { return nil }
        var mapped: [Unicode.Scalar] = []
        mapped.reserveCapacity(s.unicodeScalars.count)
        for scalar in s.unicodeScalars {
            switch scalar.value {
            case 0x200B...0x200F, 0x202A...0x202E, 0x2066...0x2069, 0xFEFF:
                continue
            case 0x2010...0x2015, 0x2212, 0xFE63, 0xFF0D:
                mapped.append(Unicode.Scalar(0x2D)!)
            default:
                mapped.append(scalar)
            }
        }
        let out = String(String.UnicodeScalarView(mapped))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return out.isEmpty ? nil : out
    }

    private func axFrame(_ el: AXUIElement) -> CGRect {
        var posRef: CFTypeRef?
        var sizeRef: CFTypeRef?
        AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &posRef)
        AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sizeRef)
        var point = CGPoint.zero
        var size = CGSize.zero
        if let posRef {
            // swiftlint:disable:next force_cast
            AXValueGetValue(posRef as! AXValue, .cgPoint, &point)
        }
        if let sizeRef {
            // swiftlint:disable:next force_cast
            AXValueGetValue(sizeRef as! AXValue, .cgSize, &size)
        }
        return CGRect(origin: point, size: size)
    }

    private func axActions(_ el: AXUIElement) -> [String] {
        var names: CFArray?
        AXUIElementCopyActionNames(el, &names)
        return (names as? [String]) ?? []
    }

    private func axChildren(_ el: AXUIElement) -> [AXUIElement] {
        var ref: CFTypeRef?
        AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &ref)
        return (ref as? [AXUIElement]) ?? []
    }

    private func quartzWindowBounds(windowID: CGWindowID) -> CGRect? {
        guard let info = CGWindowListCopyWindowInfo(
            [.optionIncludingWindow], windowID
        ) as? [[String: Any]],
              let win = info.first,
              let bounds = win[kCGWindowBounds as String] as? [String: Any],
              let x = (bounds["X"] as? NSNumber)?.doubleValue,
              let y = (bounds["Y"] as? NSNumber)?.doubleValue,
              let w = (bounds["Width"] as? NSNumber)?.doubleValue,
              let h = (bounds["Height"] as? NSNumber)?.doubleValue
        else { return nil }
        return CGRect(x: x, y: y, width: w, height: h)
    }

    private func buildMarkdown(_ elements: [AXElement]) -> String {
        var lines: [String] = []
        for el in elements {
            let indent = String(repeating: "  ", count: el.depth)
            var desc = "\(indent)[\(el.index)] \(el.role)"
            if let label = el.label { desc += " \"\(label)\"" }
            if let value = el.value {
                // Do not ellipsize text values: expect matches these strings.
                desc += " value=\"\(value)\""
            }
            if !el.actions.isEmpty {
                let pressable = el.actions.contains("AXPress")
                if pressable { desc += " [pressable]" }
            }
            let f = el.frame
            if f.width > 0 && f.height > 0 {
                desc += String(
                    format: " {%.0f,%.0f %.0fx%.0f}",
                    f.origin.x, f.origin.y, f.width, f.height
                )
            }
            lines.append(desc)
        }
        return lines.joined(separator: "\n")
    }
}
