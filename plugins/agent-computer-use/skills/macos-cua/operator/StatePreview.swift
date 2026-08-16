import AppKit
import Darwin
import Foundation

struct OperatorState: Codable {
    var version: Int?
    var active: Bool?
    var status: String?
    var app: String?
    var pid: Int?
    var windowID: Int?
    var windowTitle: String?
    var screenshotPath: String?
    var rawScreenshotPath: String?
    var cursorX: Double?
    var cursorY: Double?
    var cursorScreenX: Double?
    var cursorScreenY: Double?
    var cursorRenderedX: Double?
    var cursorRenderedY: Double?
    var cursorUpdateID: String?
    var cursorRenderedUpdateID: String?
    var cursorDurationMS: Double?
    var cursorVisible: Bool?
    var cursorImagePath: String?
    var pipVisible: Bool?
    var harness: String?
    var sessionID: String?
    var message: String?
    var updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case version, active, status, app, pid, harness, message
        case pipVisible = "pip_visible"
        case windowID = "window_id"
        case windowTitle = "window_title"
        case screenshotPath = "screenshot_path"
        case rawScreenshotPath = "raw_screenshot_path"
        case cursorX = "cursor_x"
        case cursorY = "cursor_y"
        case cursorScreenX = "cursor_screen_x"
        case cursorScreenY = "cursor_screen_y"
        case cursorRenderedX = "cursor_rendered_x"
        case cursorRenderedY = "cursor_rendered_y"
        case cursorUpdateID = "cursor_update_id"
        case cursorRenderedUpdateID = "cursor_rendered_update_id"
        case cursorDurationMS = "cursor_duration_ms"
        case cursorVisible = "cursor_visible"
        case cursorImagePath = "cursor_image_path"
        case sessionID = "session_id"
        case updatedAt = "updated_at"
    }
}

final class PreviewView: NSView {
    private let imageView = NSImageView()
    private let cursorTargetView = NSView()
    private let cursorView = NSImageView()
    private var cursorX = 0.5
    private var cursorY = 0.5
    private var cursorVisible = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 10
        layer?.masksToBounds = true

        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.imageAlignment = .alignCenter
        imageView.autoresizingMask = [.width, .height]
        addSubview(imageView)

        cursorTargetView.wantsLayer = true
        cursorTargetView.layer?.backgroundColor = NSColor.systemCyan.withAlphaComponent(0.25).cgColor
        cursorTargetView.layer?.borderColor = NSColor.systemCyan.cgColor
        cursorTargetView.layer?.borderWidth = 2
        cursorTargetView.layer?.cornerRadius = 5
        addSubview(cursorTargetView)

        cursorView.imageScaling = .scaleProportionallyUpOrDown
        cursorView.wantsLayer = true
        cursorView.layer?.shadowColor = NSColor.systemCyan.cgColor
        cursorView.layer?.shadowOpacity = 0.7
        cursorView.layer?.shadowRadius = 4
        cursorView.layer?.shadowOffset = .zero
        addSubview(cursorView)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func render(image: NSImage?, cursorX: Double?, cursorY: Double?,
                cursorVisible: Bool, cursorImagePath: String?) {
        if let image { imageView.image = image }
        self.cursorX = min(1, max(0, cursorX ?? self.cursorX))
        self.cursorY = min(1, max(0, cursorY ?? self.cursorY))
        self.cursorVisible = cursorVisible
        if let path = cursorImagePath,
           let cursor = NSImage(contentsOfFile: path) {
            cursorView.image = cursor
        } else if cursorView.image == nil {
            cursorView.image = NSImage(
                systemSymbolName: "cursorarrow",
                accessibilityDescription: "macos-cua agent cursor"
            )
            cursorView.contentTintColor = .systemCyan
        }
        needsLayout = true
    }

    override func layout() {
        super.layout()
        imageView.frame = bounds
        guard cursorVisible, let image = imageView.image,
              image.size.width > 0, image.size.height > 0 else {
            cursorView.isHidden = true
            cursorTargetView.isHidden = true
            return
        }
        cursorView.isHidden = false
        cursorTargetView.isHidden = false
        let scale = min(bounds.width / image.size.width, bounds.height / image.size.height)
        let fitted = NSSize(width: image.size.width * scale, height: image.size.height * scale)
        let imageRect = NSRect(
            x: bounds.midX - fitted.width / 2,
            y: bounds.midY - fitted.height / 2,
            width: fitted.width,
            height: fitted.height
        )
        let pointerSize = max(24, min(34, min(imageRect.width, imageRect.height) * 0.12))
        let target = NSPoint(
            x: imageRect.minX + cursorX * imageRect.width,
            y: imageRect.maxY - cursorY * imageRect.height
        )
        let pointerX = min(
            max(imageRect.minX, target.x),
            max(imageRect.minX, imageRect.maxX - pointerSize)
        )
        let pointerY = min(
            max(imageRect.minY, target.y - pointerSize),
            max(imageRect.minY, imageRect.maxY - pointerSize)
        )
        cursorView.frame = NSRect(
            x: pointerX,
            y: pointerY,
            width: pointerSize,
            height: pointerSize
        )
        cursorTargetView.frame = NSRect(
            x: min(max(imageRect.minX, target.x - 5), imageRect.maxX - 10),
            y: min(max(imageRect.minY, target.y - 5), imageRect.maxY - 10),
            width: 10,
            height: 10
        )
    }
}
