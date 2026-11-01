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
        case "locked", "01-start", "02-followed", "03-ready", "locked-x", "locked-x-ready", "locked-x-no-launch", "locked-x-prelaunch": state = .locked()
        case "locked-linkedin", "09-linkedin": state = .locked(linkedIn: true)
        case "waking", "08-waking": state = .waking
        case "checking", "04-checking":
            state = .checking(claimID: "fixture-claim", since: Date(), follow: .confirmed, post: .pending)
        case "conflict": state = .conflict(deviceName: "Studio Mac", rebindAvailableAt: nil)
        case "unlocked", "05-unlocked": state = .unlocked(account: account, verified: true, exp: ISO8601DateFormatter().date(from: "2026-11-14T12:00:00Z")!)
        case "pending-follow": state = .pendingFollow(account: account, since: Date())
        case "trial-3days": state = .trial(account: account, ends: Date().addingTimeInterval(3 * 86_400))
        case "trial-prelaunch": state = .trial(account: account, ends: Date().addingTimeInterval(3 * 86_400))
        case "trial-nudge-day2": state = .trial(account: account, ends: Date().addingTimeInterval(36 * 3_600))
        case "trial-lastday": state = .trial(account: account, ends: Date().addingTimeInterval(12 * 3_600))
        case "trial-expired": state = .trialExpired(account: account)
        case "repost-checking": state = .repostChecking(account: account, trialEnds: nil)
        case "rejected": state = .rejected(message: IslandGateFailure(code: "follow_missing").message)
        case "error", "06-error-offline": state = .error(code: "offline", message: IslandGateFailure(code: "offline").message)
        case "07-error-notfound": state = .error(code: "post_not_found", message: IslandGateFailure(code: "post_not_found").message)
        default: throw ValidationError("Unknown gate state: \(name)")
        }
        let bundled = IslandGateConfig.bundled
        let noLaunch = ["locked-x-no-launch", "locked-x-prelaunch", "trial-prelaunch"].contains(name)
        let config = IslandGateConfig(
            x: .init(handle: bundled.x.handle, profile_url: bundled.x.profile_url, intent_url: bundled.x.intent_url, preset_text: bundled.x.preset_text,
                     follow_url: bundled.x.follow_url, launch_post_id: noLaunch ? nil : "987654321", repost_url: noLaunch ? nil : URL(string: "https://x.com/intent/retweet?tweet_id=987654321")),
            linkedin: .init(share_url: bundled.linkedin.share_url, preset_text: bundled.linkedin.preset_text, follow_url: bundled.linkedin.follow_url,
                            launch_post_url: URL(string: "https://www.linkedin.com/feed/update/urn:li:activity:123456"), repost_url: URL(string: "https://www.linkedin.com/feed/update/urn:li:activity:123456")),
            match: bundled.match, grace_days: bundled.grace_days, poll_seconds: bundled.poll_seconds, check_eta_text: bundled.check_eta_text,
            trial_hours: 72, trial_eta_text: "Usually starts within a few minutes")
        let gate = IslandGate(store: IslandGateFixtureStore(), fixtureState: state, fixtureAccount: account, urlOpener: urlOpener ?? IslandGateRecordingURLOpener(), pasteboard: IslandGateRecordingPasteboard())
        gate.setConfigForTests(config)
        let followed = ["02-followed", "03-ready", "04-checking", "05-unlocked", "07-error-notfound", "locked-x-ready"].contains(name)
        gate.setFixtureSteps(follow: followed, post: false)
        if ["03-ready", "04-checking", "05-unlocked", "07-error-notfound", "locked-x-ready", "trial-expired"].contains(name) { gate.handleText = "@acu_demo_g2" }
        if name == "03-ready" { gate.focusInput = true }
        if name == "locked" || name == "locked-linkedin" || name == "locked-x" || name == "locked-x-no-launch" || name == "locked-x-prelaunch" {
            gate.handleText = name == "locked-linkedin" ? "https://www.linkedin.com/in/islandfriend" : (name == "locked-x" ? "" : "@islandfriend")
        }
        return gate
    }
}
