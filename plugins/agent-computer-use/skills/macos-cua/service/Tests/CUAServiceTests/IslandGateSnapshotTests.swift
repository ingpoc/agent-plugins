import AppKit
import SwiftUI
import XCTest
@testable import CUAService

@MainActor
final class IslandGateSnapshotTests: XCTestCase {
    private func output(_ name: String) -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("IslandGate-\(UUID())-\(name).png")
    }
    func testLiveCaptureCanBeRequestedWithoutOffscreenOutput() throws {
        let command = try IslandGateSnapshotCommand.parse(["--state", "locked", "--live-capture", "/tmp/gate native.png"])
        XCTAssertNil(command.out)
        XCTAssertEqual(command.liveCapture, "/tmp/gate native.png")
        XCTAssertEqual(command.state, "locked")
    }
    func testSnapshotRequiresAnOutputDestination() {
        XCTAssertThrowsError(try IslandGateSnapshotCommand.parse([]))
    }
    func testNativeCaptureFailsClearlyWithoutScreenRecordingPermission() {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 300), styleMask: [.titled], backing: .buffered, defer: false)
        let url = output("denied")
        XCTAssertThrowsError(try IslandGateWindowCapture.capture(window: window, to: url, screenRecordingAllowed: { false })) { error in
            XCTAssertTrue(String(describing: error).contains("Screen Recording permission"))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        XCTAssertFalse(window.isVisible)
    }
    func testEveryOffscreenStateHasUniformWidthAndOpaqueDarkEdges() throws {
        for state in ["01-start", "02-followed", "03-ready", "04-checking", "05-unlocked", "06-error-offline", "07-error-notfound", "08-waking", "09-linkedin", "conflict", "rejected", "locked-x", "locked-x-ready", "locked-x-no-launch", "locked-x-prelaunch", "locked-linkedin", "pending-follow", "trial-3days", "trial-nudge-day2", "trial-lastday", "trial-prelaunch", "trial-expired", "trial-expired-unfollowed", "locked-x-valid", "trial-linkedin", "repost-checking", "unlocked"] {
            let url = output(state)
            let command = try IslandGateSnapshotCommand.parse(["--state", state, "--out", url.path])
            try command.run()
            let bitmap = try XCTUnwrap(NSBitmapImageRep(data: Data(contentsOf: url)))
            XCTAssertEqual(bitmap.pixelsWide, 880, state)
            XCTAssertGreaterThan(bitmap.pixelsHigh, 400, state)
            for x in [0, bitmap.pixelsWide / 2, bitmap.pixelsWide - 1] {
                for y in [0, bitmap.pixelsHigh - 1] {
                    let color = try XCTUnwrap(bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB))
                    XCTAssertGreaterThan(color.alphaComponent, 0.99, "\(state) edge must be opaque")
                    XCTAssertGreaterThan(color.redComponent, 0.08, "\(state) edge must use the window background rather than clipped black pixels")
                    XCTAssertLessThan(max(color.redComponent, color.greenComponent, color.blueComponent), 0.2, "\(state) edge must be dark")
                }
            }
        }
    }
    func testPrimaryFillRemainsVisibleWhenInactiveAndDimsWhenDisabled() throws {
        _ = NSApplication.shared
        let cases: [(xBrand: Bool, disabled: Bool)] = [(true, false), (false, false), (false, true)]
        var fills: [NSColor] = []
        for (xBrand, disabled) in cases {
            let root = Button("Primary") {}
                .buttonStyle(IslandGatePrimaryButtonStyle(xBrand: xBrand))
                .disabled(disabled)
                .accentColor(.blue)
                .environment(\.controlActiveState, .inactive)
                .frame(width: 440, height: 80)
                .background(Color.black)
            let view = NSHostingView(rootView: root)
            view.appearance = NSAppearance(named: .darkAqua)
            let window = NSWindow(contentRect: NSRect(x: -10_000, y: -10_000, width: 440, height: 80), styleMask: [.borderless], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            window.contentView = view
            window.orderFrontRegardless()
            view.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            let url = output("button")
            try IslandGateSnapshotCommand.render(content: view, to: url)
            let bitmap = try XCTUnwrap(NSBitmapImageRep(data: Data(contentsOf: url)))
            fills.append(try XCTUnwrap(bitmap.colorAt(x: 40, y: 80)?.usingColorSpace(.deviceRGB)))
            window.close()
        }
        XCTAssertGreaterThan(fills[0].redComponent, 0.95)
        XCTAssertGreaterThan(fills[0].greenComponent, 0.95)
        XCTAssertGreaterThan(fills[0].blueComponent, 0.95)
        XCTAssertGreaterThan(fills[1].blueComponent, 0.7)
        XCTAssertLessThan(fills[1].redComponent, 0.3)
        XCTAssertLessThan(fills[2].blueComponent, fills[1].blueComponent * 0.6)
    }

    func testCloseAndTrialDoneFootersHaveNoPrimaryBlueFill() throws {
        for state in ["locked-x", "locked-x-ready", "locked-x-prelaunch", "locked-linkedin", "trial-expired", "trial-expired-unfollowed", "pending-follow", "repost-checking", "trial-3days", "trial-nudge-day2", "trial-lastday", "trial-prelaunch"] {
            let url = output(state)
            defer { try? FileManager.default.removeItem(at: url) }
            try IslandGateSnapshotCommand.parse(["--state", state, "--out", url.path]).run()
            let bitmap = try XCTUnwrap(NSBitmapImageRep(data: Data(contentsOf: url)))
            // The final 95 pt contains the footer and its bottom caption, below any Repost action.
            for y in (bitmap.pixelsHigh - 190)..<bitmap.pixelsHigh {
                let color = try XCTUnwrap(bitmap.colorAt(x: 70, y: y)?.usingColorSpace(.deviceRGB))
                XCTAssertFalse(color.blueComponent > 0.6 && color.redComponent < 0.25 && color.greenComponent > 0.25, "\(state) footer must stay neutral at row \(y)")
            }
        }
    }
}
