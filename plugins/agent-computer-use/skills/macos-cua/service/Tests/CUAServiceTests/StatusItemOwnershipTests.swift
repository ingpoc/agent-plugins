import AppKit
import XCTest
@testable import CUAService

/// Guru 2026-11-04 11:29 IST: three ACU icons in the menu bar. One controller must own exactly one
/// item however often install runs, and remove it on teardown. Uses a private NSStatusBar, never .system.
@MainActor
final class StatusItemOwnershipTests: XCTestCase {
    func testRepeatedInstallsKeepExactlyOneItem() {
        let controller = StatusBarController(gate: IslandGate(fixtureState: .locked()), bar: NSStatusBar())
        controller.install()
        let first = controller.statusItem
        XCTAssertNotNil(first)
        for _ in 0..<5 { controller.install() }   // reconnects, toggles, settings reloads
        XCTAssertTrue(controller.statusItem === first, "install must reuse the owned item")
    }

    func testUninstallRemovesTheItemAndReinstallMakesOneNew() {
        let controller = StatusBarController(gate: IslandGate(fixtureState: .locked()), bar: NSStatusBar())
        controller.install()
        let first = controller.statusItem
        controller.uninstall()
        controller.uninstall()   // second teardown is a no-op
        XCTAssertNil(controller.statusItem)
        controller.install()
        controller.install()
        XCTAssertNotNil(controller.statusItem)
        XCTAssertFalse(controller.statusItem === first)
    }

    func testCUAServiceHasASingleStatusItemCreationSite() throws {
        let sources = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/CUAService")
        let files = try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        var sites: [String] = []
        for f in files {
            let text = try String(contentsOf: f, encoding: .utf8)
            let n = text.components(separatedBy: "statusItem(withLength:").count - 1
            if n > 0 { sites.append("\(f.lastPathComponent)×\(n)") }
        }
        XCTAssertEqual(sites, ["StatusBarController.swift×1"])
    }
}
