import AppKit
import SwiftUI
import XCTest
@testable import CUAService

/// Onboarding + permission model, offline: no real prompts, no TCC reads that could ask, injected states.
/// Renders: CUA_ISLAND_RENDER_DIR=<dir> swift test --filter OnboardingTests/testRenderOnboarding
@MainActor
final class OnboardingTests: XCTestCase {
    private var suites: [String] = []

    override func tearDown() {
        IslandLayoutRecorder.enabled = false
        IslandLayoutRecorder.shared.reset()
        PermissionCenter.override = nil
        for s in suites { UserDefaults(suiteName: s)?.removePersistentDomain(forName: s) }
        suites = []
    }

    private func defaults() -> UserDefaults {
        let s = "cua.onboarding.test.\(UUID().uuidString)"
        suites.append(s)
        return UserDefaults(suiteName: s)!
    }

    private func store() -> IslandSettingsStore {
        IslandSettingsStore(defaults: defaults(), tokens: MemoryTokenStore())
    }

    // MARK: Model

    func testEveryPermissionHasKeyPaneAndCopy() {
        let keys = ACUPermission.allCases.map(\.key)
        XCTAssertEqual(Set(keys).count, keys.count)
        for p in ACUPermission.allCases {
            XCTAssertEqual(ACUPermission.from(key: p.key), p)
            XCTAssertNotNil(p.paneURL, "\(p) deep link")
            XCTAssertTrue(p.paneURL!.absoluteString.hasPrefix("x-apple.systempreferences:"))
            XCTAssertFalse(p.caption.isEmpty)
        }
        XCTAssertNil(ACUPermission.from(key: "island.calendar.access"))
        XCTAssertEqual(Set(ACUPermission.allCases.filter(\.required)), [.accessibility, .screenRecording, .microphone])
        XCTAssertEqual(Set(ACUPermission.allCases.compactMap(\.bundleID)),
                       ["com.apple.Music", "com.spotify.client", "com.apple.finder", "ai.perplexity.comet",
                        "com.apple.systemevents", "com.apple.TextEdit"])
        // Existing keys keep working (Computer Use focus, earlier tests).
        XCTAssertEqual(IslandSettingsSchema.cuaAccessibility, "cua.permission.accessibility")
        XCTAssertEqual(IslandSettingsSchema.cuaScreenRecording, "cua.permission.screenRecording")
    }

    func testComputerUsePaneIsTheOnboardingRows() {
        let defs = IslandSettingsSchema.defs(for: .computerUse)
        XCTAssertEqual(defs.map(\.key), ACUPermission.allCases.map(\.key))
        for d in defs { guard case .permission = d.kind else { return XCTFail("\(d.key) is not a permission row") } }
        XCTAssertEqual(Set(defs.compactMap(\.section)), ["Required", "Automation", "Optional"])
    }

    func testAutomationStatusClassification() {
        XCTAssertEqual(PermissionProbe.classify(noErr), .granted)
        XCTAssertEqual(PermissionProbe.classify(OSStatus(procNotFound)), .notRunning)
        XCTAssertEqual(PermissionProbe.classify(-1743), .missing)   // errAEEventNotPermitted
        XCTAssertEqual(PermissionProbe.classify(-1744), .missing)   // errAEEventWouldRequireUserConsent
    }

    func testNotRunningTargetKeepsLastAnswer() {
        let d = defaults()
        XCTAssertEqual(PermissionCenter.remembered(.automationMusic, .notRunning, defaults: d), .notRunning)
        XCTAssertEqual(PermissionCenter.remembered(.automationMusic, .granted, defaults: d), .granted)
        XCTAssertEqual(PermissionCenter.remembered(.automationMusic, .notRunning, defaults: d), .granted)
        XCTAssertEqual(PermissionCenter.remembered(.automationMusic, .missing, defaults: d), .missing)
        XCTAssertEqual(PermissionCenter.remembered(.automationMusic, .notRunning, defaults: d), .notRunning, "revoked: forgotten")
    }

    func testNotInstalledTargetsAreHidden() {
        PermissionCenter.override = [.automationSpotify: .notInstalled, .automationComet: .granted]
        let keys = IslandSettingsRoot.visibleDefs(.computerUse, store: store()).map(\.key)
        XCTAssertFalse(keys.contains(ACUPermission.automationSpotify.key))
        XCTAssertTrue(keys.contains(ACUPermission.automationComet.key))
        XCTAssertTrue(IslandSettingsSchema.permissionGranted(ACUPermission.automationComet.key))
        XCTAssertFalse(IslandSettingsSchema.permissionGranted(ACUPermission.accessibility.key))
    }

    func testShowsOnFirstLaunchAndWhenSomethingIsMissing() {
        let d = defaults()
        XCTAssertTrue(OnboardingModel.shouldShow(defaults: d, missing: []), "first launch")
        d.set(true, forKey: OnboardingModel.completedKey)
        XCTAssertFalse(OnboardingModel.shouldShow(defaults: d, missing: []))
        XCTAssertTrue(OnboardingModel.shouldShow(defaults: d, missing: [.screenRecording]), "revoked later")
        PermissionCenter.override = Dictionary(uniqueKeysWithValues: ACUPermission.allCases.map { ($0, PermissionState.granted) })
        XCTAssertEqual(PermissionCenter.missingRequired(), [])
        PermissionCenter.override?[.microphone] = .missing
        XCTAssertEqual(PermissionCenter.missingRequired(), [.microphone])
    }

    func testReopensOnlyWhenSomethingNewGoesMissing() {
        XCTAssertFalse(OnboardingModel.shouldReopen(previous: [], now: []))
        XCTAssertTrue(OnboardingModel.shouldReopen(previous: [], now: [.accessibility]))
        XCTAssertFalse(OnboardingModel.shouldReopen(previous: [.accessibility], now: [.accessibility]), "no nagging per poll")
        XCTAssertTrue(OnboardingModel.shouldReopen(previous: [.accessibility], now: [.accessibility, .microphone]))
        XCTAssertFalse(OnboardingModel.shouldReopen(previous: [.accessibility], now: []))
    }

    func testAgentAPIFailsFastWithPermissionMissing() {
        let none: (ACUPermission) -> PermissionState = { _ in .missing }
        let all: (ACUPermission) -> PermissionState = { _ in .granted }
        for m in ["click", "type_text", "get_app_state", "press_key", "execute_plan", "drag", "scroll"] {
            XCTAssertEqual(PermissionGate.missing(for: m, state: none, axTrusted: { false }), .accessibility, m)
            XCTAssertNil(PermissionGate.missing(for: m, state: all, axTrusted: { true }), m)
        }
        // AX on, PostEvent off: reads still work, input fails fast.
        XCTAssertNil(PermissionGate.missing(for: "get_app_state", state: none, axTrusted: { true }))
        XCTAssertEqual(PermissionGate.missing(for: "click", state: none, axTrusted: { true }), .accessibility)
        for m in ["list_apps", "open_settings", "hide_agent_cursor", "wait"] { XCTAssertNil(PermissionGate.missing(for: m, state: none), m) }
        XCTAssertEqual(PermissionGate.message(.accessibility), "PERMISSION_MISSING:accessibility")
        XCTAssertEqual(PermissionGate.message(.screenRecording), "PERMISSION_MISSING:screenRecording")
    }

    func testUsageStringsCoverEveryConsent() throws {
        let res = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources")
        let app = try XCTUnwrap(NSDictionary(contentsOf: res.appendingPathComponent("Info.plist")) as? [String: Any])
        for k in ["NSAppleEventsUsageDescription", "NSMicrophoneUsageDescription", "NSCalendarsFullAccessUsageDescription",
                  "NSCalendarsUsageDescription", "NSFocusStatusUsageDescription"] {
            XCTAssertFalse((app[k] as? String ?? "").isEmpty, "Info.plist \(k)")
        }
        let helper = try XCTUnwrap(NSDictionary(contentsOf: res.appendingPathComponent("voice-cua-Info.plist")) as? [String: Any])
        for k in ["NSAppleEventsUsageDescription", "NSMicrophoneUsageDescription"] {
            XCTAssertFalse((helper[k] as? String ?? "").isEmpty, "voice-cua-Info.plist \(k)")
        }
        let ent = try XCTUnwrap(NSDictionary(contentsOf: res.appendingPathComponent("CUAService.entitlements")) as? [String: Any])
        XCTAssertEqual(ent["com.apple.security.automation.apple-events"] as? Bool, true)
    }

    // MARK: Layout gate + renders (dark only, like ACU Settings)

    struct State { var name: String; var view: OnboardingView }

    private static let granted: (String) -> Bool = { _ in true }
    private static let missing: (String) -> Bool = { _ in false }
    private static let mixed: (String) -> Bool = { k in
        [ACUPermission.accessibility, .microphone, .automationMusic, .automationFinder].map(\.key).contains(k)
    }

    func states() -> [State] {
        [
            State(name: "welcome", view: OnboardingView(model: OnboardingModel(step: .welcome), store: store(), rendering: true)),
            State(name: "permissions-missing", view: OnboardingView(model: OnboardingModel(step: .permissions), store: store(),
                                                                    rendering: true, permissions: Self.missing)),
            State(name: "permissions-mixed", view: OnboardingView(model: OnboardingModel(step: .permissions), store: store(),
                                                                  rendering: true, permissions: Self.mixed)),
            State(name: "permissions-granted", view: OnboardingView(model: OnboardingModel(step: .permissions), store: store(),
                                                                    rendering: true, permissions: Self.granted)),
            State(name: "worst-permissions", view: OnboardingView(model: OnboardingModel(step: .permissions), store: store(),
                                                                  rendering: true, permissions: Self.missing,
                                                                  defs: IslandSettingsLayoutGateTests.worstDefs)),
        ]
    }

    func testOnboardingLayoutGate() throws {
        IslandLayoutRecorder.enabled = true
        var issues: [String] = [], checked: [String] = []
        for st in states() {
            IslandLayoutRecorder.shared.reset()
            let r = ImageRenderer(content: st.view.environment(\.colorScheme, .dark))
            r.scale = 2
            XCTAssertNotNil(r.cgImage, st.name)
            let (items, containers) = IslandLayoutRecorder.shared.snapshot()
            let name = "onboarding-\(st.name)-dark"
            let rows = containers.keys.filter { $0.hasPrefix("row|") }.count
            checked.append("\(name): \(items.count) probes, \(rows) rows")
            if st.name != "welcome" && rows < 9 { issues.append("\(name): only \(rows) rows") }
            issues += IslandLayoutGate.issues(state: name, items: items, containers: containers)
            issues += IslandLayoutGate.formIssues(state: name, items: items, containers: containers,
                                                  rowHeight: SettingsStyle.rowHeight, tokenRowHeight: SettingsStyle.tokenRowHeight)
        }
        if let path = ProcessInfo.processInfo.environment["CUA_ONBOARDING_GATE_REPORT"] {
            try (["# Onboarding layout gate", "states checked: \(checked.count)", ""] + checked + ["", "issues: \(issues.count)"] + issues)
                .joined(separator: "\n").appending("\n").write(toFile: path, atomically: true, encoding: .utf8)
        }
        XCTAssertTrue(issues.isEmpty, "onboarding layout gate (\(issues.count)):\n" + issues.prefix(60).joined(separator: "\n"))
    }

    /// 2x renders of every state plus a 2x zoom crop of the first card (status pills + Grant buttons).
    func testRenderOnboarding() throws {
        guard let dir = ProcessInfo.processInfo.environment["CUA_ISLAND_RENDER_DIR"] else { throw XCTSkip("set CUA_ISLAND_RENDER_DIR to render") }
        func save(_ img: CGImage, _ name: String) throws {
            let url = URL(fileURLWithPath: dir).appendingPathComponent("onboarding-\(name)-dark.png")
            XCTAssertFalse(FileManager.default.fileExists(atPath: url.path), "never overwrite \(url.lastPathComponent)")
            try NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: url)
        }
        for st in states() {
            let r = ImageRenderer(content: st.view.environment(\.colorScheme, .dark))
            r.scale = 2
            guard let img = r.cgImage else { XCTFail("render \(st.name)"); continue }
            try save(img, st.name)
            if st.name == "permissions-mixed" {
                let z = ImageRenderer(content: st.view.environment(\.colorScheme, .dark))
                z.scale = 4
                if let big = z.cgImage, let crop = big.cropping(to: CGRect(x: 0, y: 0, width: big.width, height: min(big.height, 1400))) {
                    try save(crop, "permissions-mixed-zoom2x")
                }
            }
        }
    }
}
