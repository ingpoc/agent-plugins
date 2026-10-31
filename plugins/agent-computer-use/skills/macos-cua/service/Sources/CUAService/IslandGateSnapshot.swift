import AppKit
import ArgumentParser
import SwiftUI

private final class IslandGateFixtureStore: IslandGateStoring {
    private var values: [String: Data] = [:]
    func read(_ name: String) throws -> Data? { values[name] }
    func write(_ data: Data, name: String) throws { values[name] = data }
    func delete(_ name: String) throws { values.removeValue(forKey: name) }
}

struct IslandGateSnapshotCommand: ParsableCommand {
    static let configuration = CommandConfiguration(commandName: "gate-snapshot", abstract: "Render the Dynamic Island unlock window using local fixtures")
    @Option var state: String = "locked"
    @Option var out: String?
    @Option(name: .long, help: "Capture the real on-screen gate window as a PNG (requires Screen Recording access)")
    var liveCapture: String?
    @Flag var live = false
    @Flag var realOpen = false
    mutating func validate() throws {
        guard out != nil || liveCapture != nil else { throw ValidationError("Specify --out <png> or --live-capture <png>.") }
        if realOpen { try IslandGateValidatedURLOpener.validate(.bundled) }
    }
    func run() throws {
        let stateName = state, path = out, live = live, capturePath = liveCapture
        try MainActor.assumeIsolated {
            let app = NSApplication.shared
            app.setActivationPolicy(.accessory)
            let gate = try IslandGateFixtures.gate(stateName, urlOpener: realOpen ? IslandGateValidatedURLOpener() : nil)
            let controller = IslandGateWindowController(gate: gate)
            guard let window = controller.window, let content = window.contentView else { throw ValidationError("Cannot create snapshot window") }
            defer { window.close() }
            controller.fitContent()
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
    static func gate(_ name: String, urlOpener: IslandGateURLOpening? = nil) throws -> IslandGate {
        let account = IslandGateAccount(id: "fixture", provider: name.contains("linkedin") ? "linkedin" : "x", handle: "@acu_demo_g2", name: "Island Friend", verified: true)
        let state: IslandGateState
        switch name {
        case "locked", "01-start", "02-followed", "03-ready": state = .locked()
        case "locked-linkedin", "09-linkedin": state = .locked(linkedIn: true)
        case "waking", "08-waking": state = .waking
        case "checking", "04-checking":
            state = .checking(claimID: "fixture-claim", since: Date(), follow: .confirmed, post: .pending)
        case "conflict": state = .conflict(deviceName: "Studio Mac", rebindAvailableAt: nil)
        case "unlocked", "05-unlocked": state = .unlocked(account: account, verified: true, exp: ISO8601DateFormatter().date(from: "2026-11-14T12:00:00Z")!)
        case "rejected": state = .rejected(message: IslandGateFailure(code: "follow_missing").message)
        case "error", "06-error-offline": state = .error(code: "offline", message: IslandGateFailure(code: "offline").message)
        case "07-error-notfound": state = .error(code: "post_not_found", message: IslandGateFailure(code: "post_not_found").message)
        default: throw ValidationError("Unknown gate state: \(name)")
        }
        let gate = IslandGate(store: IslandGateFixtureStore(), fixtureState: state, fixtureAccount: account, urlOpener: urlOpener ?? IslandGateRecordingURLOpener(), pasteboard: IslandGateRecordingPasteboard())
        gate.setFixtureSteps(follow: ["02-followed", "03-ready", "04-checking", "05-unlocked", "07-error-notfound"].contains(name), post: ["03-ready", "04-checking", "05-unlocked", "07-error-notfound"].contains(name))
        if ["03-ready", "04-checking", "05-unlocked", "07-error-notfound"].contains(name) { gate.handleText = "@acu_demo_g2" }
        if name == "03-ready" { gate.focusInput = true }
        if name == "locked" || name == "locked-linkedin" {
            gate.handleText = name == "locked-linkedin" ? "https://www.linkedin.com/in/islandfriend" : "@islandfriend"
        }
        return gate
    }
}
