import AppKit

final class ProofSlider: NSSlider {
    override func accessibilityValue() -> Any? {
        doubleValue
    }

    override func setAccessibilityValue(_ accessibilityValue: Any?) {
        guard let number = accessibilityValue as? NSNumber else { return }
        doubleValue = number.doubleValue
        needsDisplay = true
    }
}

final class DragSurface: NSView {
    private let slider = ProofSlider(value: 0, minValue: 0, maxValue: 100, target: nil, action: nil)
    private var valueObservation: NSKeyValueObservation?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        slider.frame = NSRect(x: 42, y: 52, width: 336, height: 34)
        slider.isContinuous = true
        slider.setAccessibilityLabel("Agent drag slider")
        addSubview(slider)
        valueObservation = slider.observe(\.doubleValue, options: [.new]) { [weak self] _, _ in
            self?.needsDisplay = true
        }
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        bounds.fill()

        let title = "macos-cua drag proof" as NSString
        title.draw(
            at: NSPoint(x: 28, y: bounds.height - 42),
            withAttributes: [
                .font: NSFont.systemFont(ofSize: 18, weight: .semibold),
                .foregroundColor: NSColor.labelColor,
            ]
        )
        let subtitle = "Drag the agent-controlled thumb across the track" as NSString
        subtitle.draw(
            at: NSPoint(x: 28, y: bounds.height - 66),
            withAttributes: [
                .font: NSFont.systemFont(ofSize: 12),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]
        )

        let complete = slider.doubleValue >= 50
        let indicator = NSBezierPath(ovalIn: NSRect(x: 18, y: 8, width: 50, height: 50))
        (complete ? NSColor.systemGreen : NSColor.systemRed).setFill()
        indicator.fill()
        let state = (complete ? "Complete  100" : "Ready  0") as NSString
        state.draw(
            at: NSPoint(x: bounds.width - 118, y: 18),
            withAttributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
                .foregroundColor: complete
                    ? NSColor.systemGreen
                    : NSColor.systemRed,
            ]
        )
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: 420, height: 180),
    styleMask: [.titled, .closable],
    backing: .buffered,
    defer: false
)
window.title = "macos-cua Drag Fixture"
window.center()

let surface = DragSurface(frame: window.contentView?.bounds ?? .zero)
surface.autoresizingMask = [.width, .height]
surface.setAccessibilityElement(false)
window.contentView = surface

window.makeKeyAndOrderFront(nil)
window.makeFirstResponder(surface)
app.activate(ignoringOtherApps: true)
app.run()
