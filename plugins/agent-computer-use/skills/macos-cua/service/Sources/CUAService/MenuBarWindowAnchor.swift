import AppKit
import CoreGraphics

/// Reads the live **Window Server → Menubar** window and converts to AppKit `NSRect`.
///
/// Apple draws the system menu bar as a dedicated window (`kCGWindowOwnerName` = "Window Server",
/// `kCGWindowName` = "Menubar", layer ≈ `kCGMainMenuWindowLevel`). Do **not** infer placement from
/// `NSScreen.visibleFrame` alone — on multi-display setups Quartz menubar origin can diverge from
/// `frame.maxY - visibleFrame.maxY` (see `CGDisplayBounds` vs `NSScreen.frame`).
///
/// Quartz window bounds use a top-left origin; `NSWindow.setFrame` uses AppKit screen coords
/// (bottom-left origin). Convert via the display's `CGDisplayBounds` + `NSScreen.frame`.
enum MenuBarWindowAnchor {
    struct Strip {
        let screen: NSScreen
        let appKit: NSRect
        let quartz: CGRect
    }

    /// Menubar strip for `screen`, or nil when Window Server hasn't published one yet.
    static func strip(on screen: NSScreen) -> Strip? {
        guard let quartz = quartzMenubar(on: screen) else { return nil }
        let appKit = appKitRect(fromQuartz: quartz, on: screen)
        guard appKit.width > 0, appKit.height > 0 else { return nil }
        return Strip(screen: screen, appKit: appKit, quartz: quartz)
    }

    /// Screen that currently hosts the system menubar window.
    static func menuBarScreen() -> NSScreen {
        if let main = NSScreen.main,
           quartzMenubar(on: main) != nil {
            return main
        }
        for screen in NSScreen.screens {
            if quartzMenubar(on: screen) != nil {
                return screen
            }
        }
        return NSScreen.main ?? NSScreen.screens[0]
    }

    static func islandFrame(contentSize: NSSize, contentHeight: CGFloat, on screen: NSScreen) -> NSRect {
        let width = min(max(contentSize.width, 80), 320)
        if let strip = strip(on: screen) {
            let bar = strip.appKit
            let h = min(contentHeight, bar.height)
            let x = bar.midX - width / 2
            // Fill the Window Server Menubar strip vertically.
            let y = bar.origin.y + max(0, bar.height - h)
            return NSRect(x: x, y: y, width: width, height: h)
        }
        let top = screen.frame.maxY
        let bottom = screen.visibleFrame.maxY
        let stripH = max(top - bottom, NSStatusBar.system.thickness)
        let h = min(contentHeight, stripH)
        let y = bottom + max(0, stripH - h)
        return NSRect(x: screen.frame.midX - width / 2, y: y, width: width, height: h)
    }

    // MARK: - Window Server Menubar lookup

    private static func quartzMenubar(on screen: NSScreen) -> CGRect? {
        guard let displayID = screen.displayID else { return nil }
        let display = CGDisplayBounds(displayID)
        guard let info = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: Any]] else { return nil }

        for win in info {
            guard (win[kCGWindowOwnerName as String] as? String) == "Window Server",
                  (win[kCGWindowName as String] as? String) == "Menubar",
                  let rect = quartzRect(from: win)
            else { continue }
            let midX = rect.midX
            if midX >= display.minX && midX <= display.maxX {
                return rect
            }
        }
        return nil
    }

    private static func quartzRect(from win: [String: Any]) -> CGRect? {
        guard let bounds = win[kCGWindowBounds as String] as? [String: Any],
              let x = (bounds["X"] as? NSNumber)?.doubleValue,
              let y = (bounds["Y"] as? NSNumber)?.doubleValue,
              let w = (bounds["Width"] as? NSNumber)?.doubleValue,
              let h = (bounds["Height"] as? NSNumber)?.doubleValue
        else { return nil }
        return CGRect(x: x, y: y, width: w, height: h)
    }

    /// Quartz (top-left) → AppKit (bottom-left) for one display.
    private static func appKitRect(fromQuartz q: CGRect, on screen: NSScreen) -> NSRect {
        guard let displayID = screen.displayID else {
            return NSRect(x: q.origin.x, y: q.origin.y, width: q.width, height: q.height)
        }
        let cgDisplay = CGDisplayBounds(displayID)
        let sf = screen.frame
        let localTop = q.origin.y - cgDisplay.origin.y
        let localLeft = q.origin.x - cgDisplay.origin.x
        let appKitY = sf.minY + (sf.height - localTop - q.height)
        let appKitX = sf.minX + localLeft
        return NSRect(x: appKitX, y: appKitY, width: q.width, height: q.height)
    }
}

private extension NSScreen {
    var displayID: CGDirectDisplayID? {
        let key = NSDeviceDescriptionKey("NSScreenNumber")
        guard let num = deviceDescription[key] as? NSNumber else { return nil }
        return CGDirectDisplayID(num.uint32Value)
    }
}
