import AppKit
import SwiftUI

/// Menu-bar-center island pill anchored to the live Window Server **Menubar** window.
@MainActor
final class SamanthaMenuBarIslandPanel {
    private var panel: NSPanel?
    private weak var model: IslandModel?
    private var screenObserver: NSObjectProtocol?

    func attach(model: IslandModel) {
        self.model = model
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.panel?.isVisible == true else { return }
                self.reposition(on: MenuBarWindowAnchor.menuBarScreen())
            }
        }
    }

    deinit {
        if let screenObserver {
            NotificationCenter.default.removeObserver(screenObserver)
        }
    }

    func show(on screen: NSScreen? = nil) {
        guard let model else { return }
        let target = screen ?? MenuBarWindowAnchor.menuBarScreen()
        if panel == nil {
            panel = makePanel(model: model, on: target)
        }
        reposition(on: target)
        panel?.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
    }

    func reposition(on screen: NSScreen? = nil) {
        guard let panel, let container = panel.contentView,
              let host = container.subviews.first as? NSHostingView<SamanthaIslandBar> else { return }
        let target = screen ?? MenuBarWindowAnchor.menuBarScreen()
        let barHeight = Self.contentHeight(on: target)
        if let model {
            host.rootView = SamanthaIslandBar(
                model: model,
                presentation: .menuBarCenter,
                barHeight: barHeight
            )
        }
        host.layoutSubtreeIfNeeded()
        let fit = host.fittingSize
        let width = max(fit.width, 80)
        let frame = MenuBarWindowAnchor.islandFrame(
            contentSize: NSSize(width: width, height: barHeight),
            contentHeight: barHeight,
            on: target
        )
        panel.setFrame(frame, display: true)
        panel.level = Self.menuBarPanelLevel
        container.frame = NSRect(origin: .zero, size: frame.size)
        host.frame = container.bounds
    }

    static func menuBarScreen() -> NSScreen {
        MenuBarWindowAnchor.menuBarScreen()
    }

    static func menuBarContentHeight(on screen: NSScreen) -> CGFloat {
        contentHeight(on: screen)
    }

    private static func contentHeight(on screen: NSScreen) -> CGFloat {
        if let strip = MenuBarWindowAnchor.strip(on: screen) {
            return strip.appKit.height
        }
        let measured = screen.frame.maxY - screen.visibleFrame.maxY
        return measured > 4 ? measured : NSStatusBar.system.thickness
    }

    /// Same band as Window Server Menubar (24) + 1 so the pill paints inside the strip.
    private static var menuBarPanelLevel: NSWindow.Level {
        NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.mainMenuWindow)) + 1)
    }

    private func makePanel(model: IslandModel, on screen: NSScreen) -> NSPanel {
        let height = Self.contentHeight(on: screen)
        let root = SamanthaIslandBar(model: model, presentation: .menuBarCenter, barHeight: height)
        let host = NSHostingView(rootView: root)
        host.translatesAutoresizingMaskIntoConstraints = true
        host.wantsLayer = true
        host.layer?.masksToBounds = true

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 160, height: height),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.isFloatingPanel = false
        panel.level = Self.menuBarPanelLevel
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.ignoresMouseEvents = true

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 160, height: height))
        container.wantsLayer = true
        container.layer?.masksToBounds = true
        container.addSubview(host)
        host.frame = container.bounds
        panel.contentView = container
        return panel
    }
}
