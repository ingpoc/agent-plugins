import AppKit
import ArgumentParser
import SwiftUI

struct IslandGateSnapshotCommand: ParsableCommand {
    static let configuration = CommandConfiguration(commandName: "gate-snapshot", abstract: "Render the Dynamic Island unlock window using local fixtures")
    @Option var state: String = "locked"
    @Option var out: String?
    @Option(name: .long, help: "Capture the real on-screen gate window as a PNG (requires Screen Recording access)")
    var liveCapture: String?
    @Flag var live = false
    mutating func validate() throws {
        guard out != nil || liveCapture != nil else { throw ValidationError("Specify --out <png> or --live-capture <png>.") }
    }
    func run() throws {
        let stateName = state, path = out, live = live, capturePath = liveCapture
        try MainActor.assumeIsolated {
            let app = NSApplication.shared
            app.setActivationPolicy(.accessory)
            let gate = try IslandGateFixtures.gate(stateName)
            let controller = IslandGateWindowController(gate: gate)
            guard let window = controller.window, let content = window.contentView else { throw ValidationError("Cannot create snapshot window") }
            // Lay out the same NSHostingView and AppKit controls used by the interactive window.
            defer { window.close() }
            controller.fitContent()
            // Render offscreen unless the caller explicitly requested a visible window.
            if !live && capturePath == nil { window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000)) }
            window.orderFrontRegardless()
            content.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.2))
            if let path { try Self.render(content: content, to: URL(fileURLWithPath: path)) }
            if let capturePath {
                try IslandGateWindowCapture.capture(window: window, to: URL(fileURLWithPath: capturePath))
            }
            if live {
                app.activate(ignoringOtherApps: true)
                window.center()
                window.makeKeyAndOrderFront(nil)
                RunLoop.main.run(until: Date().addingTimeInterval(3))
            }
        }
    }
    @MainActor
    static func render(content: NSView, to url: URL) throws {
        let bounds = content.bounds
        guard bounds.width == 440,
              let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 880, pixelsHigh: Int(ceil(bounds.height * 2)), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { throw ValidationError("Cannot allocate a 440 pt gate snapshot") }
        bitmap.size = bounds.size
        content.cacheDisplay(in: bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw ValidationError("Cannot encode snapshot") }
        try png.write(to: url)
    }
}

@MainActor
enum IslandGateFixtures {
    static func gate(_ name: String) throws -> IslandGate {
        let account = IslandGateAccount(id: "fixture", provider: name == "needs-verify-linkedin" ? "linkedin" : "x", handle: "@islandfriend", name: "Island Friend", verified: true)
        let state: IslandGateState
        switch name {
        case "locked": state = .locked()
        case "signing-in": state = .signingIn("x")
        case "needs-verify-x": state = .needsVerify(provider: "x", follow: true, post: false)
        case "needs-verify-linkedin": state = .needsVerify(provider: "linkedin", follow: false, post: false)
        case "pending": state = .pendingReview
        case "conflict": state = .conflict(deviceName: "Studio Mac", boundAt: "2026-10-24T12:00:00Z", rebindAvailableAt: nil)
        case "unlocked": state = .unlocked(account: account, verified: true, exp: Date().addingTimeInterval(14 * 86400))
        case "error": state = .error(code: "offline", message: IslandGateFailure(code: "offline").message)
        default: throw ValidationError("Unknown gate state: \(name)")
        }
        return IslandGate(fixtureState: state, fixtureAccount: account)
    }
}
