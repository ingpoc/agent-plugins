import AppKit
import Darwin
import Foundation

@MainActor
final class HermesPointerView: NSView {
    var image: NSImage? {
        didSet { needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let image else { return }
        let pointerRect = NSRect(x: 18, y: 18, width: 28, height: 28)

        NSGraphicsContext.saveGraphicsState()
        let cyanGlow = NSShadow()
        cyanGlow.shadowColor = NSColor(
            calibratedRed: 73 / 255,
            green: 182 / 255,
            blue: 1,
            alpha: 0.9
        )
        cyanGlow.shadowBlurRadius = 12
        cyanGlow.shadowOffset = .zero
        cyanGlow.set()
        image.draw(in: pointerRect)
        NSGraphicsContext.restoreGraphicsState()

        NSGraphicsContext.saveGraphicsState()
        let depthShadow = NSShadow()
        depthShadow.shadowColor = NSColor.black.withAlphaComponent(0.45)
        depthShadow.shadowBlurRadius = 4
        depthShadow.shadowOffset = NSSize(width: 0, height: -2)
        depthShadow.set()
        image.draw(in: pointerRect)
        NSGraphicsContext.restoreGraphicsState()

        image.draw(in: pointerRect)
    }
}

final class CursorOverlayView: NSView {
    private let cursorContainer = NSView()
    private let cursorView = HermesPointerView()
    private let label = NSTextField(labelWithString: "macos-cua · Agent")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor

        cursorContainer.wantsLayer = true
        cursorContainer.layer?.masksToBounds = false
        addSubview(cursorContainer)

        cursorView.wantsLayer = true
        cursorView.layer?.masksToBounds = false
        cursorContainer.addSubview(cursorView)

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

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func render(cursorImagePath: String?, harness: String) {
        if let path = cursorImagePath,
           let image = NSImage(contentsOfFile: path) {
            cursorView.image = image
        } else {
            let fallback = NSImage(
                systemSymbolName: "cursorarrow",
                accessibilityDescription: "macos-cua agent cursor"
            )
            cursorView.image = fallback
        }
        let agent = harness.trimmingCharacters(in: .whitespacesAndNewlines)
        label.stringValue = agent.isEmpty ? "macos-cua · Agent" : "macos-cua · \(agent)"
        needsLayout = true
    }

    override func layout() {
        super.layout()
        // Match Hermes Chrome's rendered CSS, including enough transparent
        // panel margin for its 12 px cyan drop-shadow not to be clipped.
        cursorContainer.frame = NSRect(x: -4, y: 30, width: 64, height: 64)
        cursorView.frame = cursorContainer.bounds
        label.frame = NSRect(x: 37, y: 20, width: 160, height: 26)
    }
}
