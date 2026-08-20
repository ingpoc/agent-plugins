import AppKit
import Foundation

/// In-process cursor overlay — no IPC, no file polling.
/// Animates on the main thread via Core Animation.
@MainActor
final class CursorOverlay: @unchecked Sendable {
    private var panel: NSPanel!
    private var overlayView: CUACursorView!
    private var animationTimer: Timer?
    private var animStart = NSPoint.zero
    private var animTarget = NSPoint.zero
    private var animStartTime: TimeInterval = 0
    private let animDuration: TimeInterval = 0.12

    init() {
        setupPanel()
    }

    private func setupPanel() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 210, height: 90),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .popUpMenu
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [
            .canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle
        ]

        overlayView = CUACursorView(
            frame: panel.contentView?.bounds ?? .zero
        )
        overlayView.autoresizingMask = [.width, .height]
        panel.contentView = overlayView
    }

    /// Glide the agent tip to the click point. Returns the Quartz point the
    /// tip aims at (may be window-clamped) and how long to wait before press
    /// so tip and action stay in sync.
    @discardableResult
    func glideTo(
        screenPoint: CGPoint,
        windowID: CGWindowID?,
        axBounds: CGRect? = nil
    ) -> (point: CGPoint, wait: TimeInterval) {
        let cgBounds = windowID.flatMap { Self.quartzBounds(of: $0) }
        // AX window frame is the live app surface. CGWindowList is often a
        // Stage Manager / window-server proxy (tiny bounds, wrong display).
        let windowBounds = Self.preferredWindowBounds(ax: axBounds, cg: cgBounds)
        var quartz = screenPoint
        if let windowBounds {
            quartz = Self.clamp(quartz, to: windowBounds.insetBy(dx: 8, dy: 8))
        }
        let appKitPt = appKitPoint(fromQuartz: quartz, windowBounds: windowBounds)
        let origin = NSPoint(x: appKitPt.x - 15, y: appKitPt.y - 76)

        animationTimer?.invalidate()
        animationTimer = nil
        if !panel.isVisible {
            panel.setFrameOrigin(origin)
            panel.orderFrontRegardless()
            return (quartz, 0)
        }

        let current = panel.frame.origin
        if abs(current.x - origin.x) < 0.5 && abs(current.y - origin.y) < 0.5 {
            panel.setFrameOrigin(origin)
            panel.orderFrontRegardless()
            return (quartz, 0)
        }

        animStart = current
        animTarget = origin
        animStartTime = ProcessInfo.processInfo.systemUptime
        panel.orderFrontRegardless()

        let timer = Timer(
            timeInterval: 1.0 / 60.0,
            target: self,
            selector: #selector(tick),
            userInfo: nil,
            repeats: true
        )
        animationTimer = timer
        RunLoop.main.add(timer, forMode: .common)
        return (quartz, animDuration)
    }

    func hide() {
        animationTimer?.invalidate()
        animationTimer = nil
        panel.orderOut(nil)
    }

    @objc private func tick() {
        let elapsed = ProcessInfo.processInfo.systemUptime - animStartTime
        let progress = min(1, max(0, elapsed / animDuration))
        let eased = 1 - pow(1 - progress, 3)
        let origin = NSPoint(
            x: animStart.x + (animTarget.x - animStart.x) * eased,
            y: animStart.y + (animTarget.y - animStart.y) * eased
        )
        panel.setFrameOrigin(origin)
        if progress >= 1 {
            animationTimer?.invalidate()
            animationTimer = nil
            panel.setFrameOrigin(animTarget)
        }
    }

    private func appKitPoint(fromQuartz point: CGPoint, windowBounds: CGRect?) -> NSPoint {
        let probe = windowBounds.map { CGPoint(x: $0.midX, y: $0.midY) } ?? point
        let screen = Self.screen(forQuartz: probe)
            ?? Self.screen(forQuartz: point)
            ?? Self.closestScreen(toQuartz: probe)
            ?? NSScreen.main
            ?? NSScreen.screens.first
        guard let screen else {
            return NSPoint(x: point.x, y: point.y)
        }
        let qFrame = Self.quartzFrame(for: screen)
        return NSPoint(
            x: screen.frame.minX + point.x - qFrame.minX,
            y: screen.frame.maxY - (point.y - qFrame.minY)
        )
    }

    private static func screen(forQuartz point: CGPoint) -> NSScreen? {
        for screen in NSScreen.screens {
            if quartzFrame(for: screen).insetBy(dx: -2, dy: -2).contains(point) {
                return screen
            }
        }
        return nil
    }

    private static func closestScreen(toQuartz point: CGPoint) -> NSScreen? {
        NSScreen.screens.min { a, b in
            quartzFrame(for: a).distanceSquared(to: point)
                < quartzFrame(for: b).distanceSquared(to: point)
        }
    }

    private static func quartzFrame(for screen: NSScreen) -> CGRect {
        let key = NSDeviceDescriptionKey("NSScreenNumber")
        if let num = screen.deviceDescription[key] as? NSNumber {
            return CGDisplayBounds(CGDirectDisplayID(num.uint32Value))
        }
        return screen.frame
    }

    private static func preferredWindowBounds(ax: CGRect?, cg: CGRect?) -> CGRect? {
        if let ax, ax.width >= 80, ax.height >= 80 {
            return ax
        }
        return cg
    }

    private static func clamp(_ point: CGPoint, to rect: CGRect) -> CGPoint {
        guard rect.width > 0, rect.height > 0 else { return point }
        return CGPoint(
            x: min(max(point.x, rect.minX), rect.maxX),
            y: min(max(point.y, rect.minY), rect.maxY)
        )
    }

    private static func quartzBounds(of windowID: CGWindowID) -> CGRect? {
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
}

private extension CGRect {
    func distanceSquared(to point: CGPoint) -> CGFloat {
        if contains(point) { return 0 }
        let dx = point.x < minX ? minX - point.x : (point.x > maxX ? point.x - maxX : 0)
        let dy = point.y < minY ? minY - point.y : (point.y > maxY ? point.y - maxY : 0)
        return dx * dx + dy * dy
    }
}

/// Pointer view matching the original OperatorDelegate style:
/// Dark angular pointer shape with cyan glow + depth shadow + floating label.
final class CUACursorView: NSView {
    private let cursorContainer = NSView()
    private let pointerView = HermesPointerView()
    private let label = NSTextField(labelWithString: "macos-cua · Agent")

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor

        cursorContainer.wantsLayer = true
        cursorContainer.layer?.masksToBounds = false
        addSubview(cursorContainer)

        pointerView.wantsLayer = true
        pointerView.layer?.masksToBounds = false
        cursorContainer.addSubview(pointerView)

        label.font = .systemFont(ofSize: 12, weight: .semibold)
        label.textColor = .white
        label.alignment = .center
        label.lineBreakMode = .byTruncatingTail
        label.wantsLayer = true
        label.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.78).cgColor
        label.layer?.borderColor = NSColor.systemCyan.withAlphaComponent(0.9).cgColor
        label.layer?.borderWidth = 1
        label.layer?.cornerRadius = 8
        addSubview(label)

        let floatAnimation = CABasicAnimation(keyPath: "transform.translation.y")
        floatAnimation.fromValue = 0
        floatAnimation.toValue = 1
        floatAnimation.duration = 0.85
        floatAnimation.autoreverses = true
        floatAnimation.repeatCount = .infinity
        floatAnimation.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        cursorContainer.layer?.add(floatAnimation, forKey: "hermes-pointer-idle")
    }

    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        cursorContainer.frame = NSRect(x: -4, y: 30, width: 64, height: 64)
        pointerView.frame = cursorContainer.bounds
        label.frame = NSRect(x: 37, y: 20, width: 160, height: 26)
    }
}

/// Draws the pointer shape from SVG path data — no file I/O, no third-party SVG libs.
/// Dark angular pointer with cyan glow + depth shadow, matching the original operator design.
final class HermesPointerView: NSView {
    /// Pointer shape from pointer-shape-animated.svg, viewBox 0 0 512 512.
    /// Scaled to fit the draw rect at render time.
    private static let pointerPath: CGPath = {
        let p = CGMutablePath()
        p.move(to: CGPoint(x: 27.4, y: 4.8))
        p.addCurve(to: CGPoint(x: 2.7, y: 20.0),
                    control1: CGPoint(x: 15.9, y: 1.2),
                    control2: CGPoint(x: 5.1, y: 8.1))
        p.addCurve(to: CGPoint(x: 4.0, y: 36.7),
                    control1: CGPoint(x: 1.5, y: 25.5),
                    control2: CGPoint(x: 2.1, y: 31.4))
        p.addLine(to: CGPoint(x: 164.9, y: 486.1))
        p.addCurve(to: CGPoint(x: 188.1, y: 507.2),
                    control1: CGPoint(x: 169.0, y: 497.7),
                    control2: CGPoint(x: 177.2, y: 507.2))
        p.addCurve(to: CGPoint(x: 207.5, y: 488.8),
                    control1: CGPoint(x: 198.5, y: 507.2),
                    control2: CGPoint(x: 204.0, y: 499.0))
        p.addLine(to: CGPoint(x: 275.9, y: 277.9))
        p.addLine(to: CGPoint(x: 493.6, y: 201.8))
        p.addCurve(to: CGPoint(x: 510.6, y: 183.6),
                    control1: CGPoint(x: 504.2, y: 198.1),
                    control2: CGPoint(x: 511.1, y: 191.4))
        p.addCurve(to: CGPoint(x: 493.1, y: 167.2),
                    control1: CGPoint(x: 510.1, y: 176.2),
                    control2: CGPoint(x: 503.4, y: 170.6))
        p.addLine(to: CGPoint(x: 38.5, y: 8.1))
        p.addCurve(to: CGPoint(x: 27.4, y: 4.8),
                    control1: CGPoint(x: 34.9, y: 6.8),
                    control2: CGPoint(x: 31.1, y: 5.9))
        p.closeSubpath()
        return p
    }()

    override init(frame: NSRect) {
        super.init(frame: frame)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        let drawRect = NSRect(x: 18, y: 18, width: 28, height: 28)

        // Scale 512x512 viewBox to drawRect, flip Y for AppKit
        ctx.saveGState()
        ctx.translateBy(x: drawRect.minX, y: drawRect.maxY)
        ctx.scaleBy(x: drawRect.width / 512, y: -drawRect.height / 512)

        // Cyan glow pass
        ctx.setShadow(
            offset: .zero, blur: 12,
            color: NSColor(calibratedRed: 73/255, green: 182/255, blue: 1, alpha: 0.9).cgColor
        )
        ctx.addPath(Self.pointerPath)
        ctx.setFillColor(NSColor(calibratedRed: 0x11/255, green: 0x16/255, blue: 0x1b/255, alpha: 1).cgColor)
        ctx.fillPath()

        // Depth shadow pass
        ctx.setShadow(
            offset: CGSize(width: 0, height: 8), blur: 4,
            color: NSColor.black.withAlphaComponent(0.45).cgColor
        )
        ctx.addPath(Self.pointerPath)
        ctx.fillPath()

        // Clean final pass — no shadow
        ctx.setShadow(offset: .zero, blur: 0, color: nil)
        ctx.addPath(Self.pointerPath)
        ctx.fillPath()

        ctx.restoreGState()
    }
}
