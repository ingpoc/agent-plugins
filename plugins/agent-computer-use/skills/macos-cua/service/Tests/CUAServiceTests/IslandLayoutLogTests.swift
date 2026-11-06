import AppKit
import XCTest
@testable import CUAService

/// `island_layout` schema 1 (developer-observability 0.1.7): rectangles only, top-left, change-only, 250 ms coalescing.
@MainActor
final class IslandLayoutLogTests: XCTestCase {
    private var rows: [[String: Any]] = []
    private var now = Date(timeIntervalSince1970: 1_000)
    private var scheduled: [() -> Void] = []
    private var settleScheduled: [() -> Void] = []
    private var logger: IslandLayoutLogger!

    override func setUp() {
        logger = IslandLayoutLogger()
        logger.enabled = true
        logger.clock = { [unowned self] in self.now }
        logger.sink = { [unowned self] in self.rows.append($0) }
        logger.schedule = { [unowned self] _, block in self.scheduled.append(block) }
        logger.settleSchedule = { [unowned self] _, block in self.settleScheduled.append(block) }
    }

    private func post(_ trigger: String, x: CGFloat = 600, width: CGFloat = 75, overlays: [IslandLayoutOverlay] = [],
                      details: IslandLayoutDetails? = nil) {
        logger.island(trigger: trigger, screenId: "1", screen: CGRect(x: 0, y: 0, width: 1512, height: 982), scale: 2,
                      island: CGRect(x: x, y: 6, width: width, height: 24), visible: true, overlays: overlays, details: details)
    }

    func testPayloadShapeIsRectanglesOnly() throws {
        let ring = IslandLayoutOverlay(kind: "comet_circle", id: "comet_circle", rect: IslandLayoutRect(CGRect(x: 606, y: 8, width: 20, height: 20)),
                                       visible: true, expectedInsideIsland: true)
        post("show", overlays: [ring])
        XCTAssertEqual(rows.count, 1)
        let row = rows[0]
        XCTAssertEqual(Set(row.keys), ["ts", "ts_epoch", "event", "kind", "source", "schema", "session_id", "turn_id",
                                       "version", "trigger", "screen", "island", "overlays", "island_origin", "ring_outer_rect",
                                       "dot_rect", "voice_slot", "glyphs", "status", "app_identity", "transition_id", "transition_phase", "transition_trigger"])
        XCTAssertEqual(row["event"] as? String, "island_layout")
        XCTAssertEqual(row["schema"] as? Int, 1)
        XCTAssertTrue(row["turn_id"] is NSNull)
        XCTAssertNotNil(ISO8601DateFormatter().date(from: (row["ts"] as? String ?? "").replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)))
        let island = try XCTUnwrap(row["island"] as? [String: Any])
        XCTAssertEqual(Set(island.keys), ["x", "y", "w", "h", "visible"])
        let screen = try XCTUnwrap(row["screen"] as? [String: Any])
        XCTAssertEqual(Set(screen.keys), ["id", "x", "y", "w", "h", "scale"])
        XCTAssertEqual(row["island_origin"] as? [String: Double], ["x": 600, "y": 6])
        XCTAssertTrue(row["ring_outer_rect"] is NSNull)
        XCTAssertTrue(row["dot_rect"] is NSNull)
        XCTAssertEqual((row["voice_slot"] as? [String: Any])?["contents"] as? String, "none")
        XCTAssertEqual(row["status"] as? String, "unknown")
        XCTAssertEqual(row["app_identity"] as? [String], [])
        let overlay = try XCTUnwrap((row["overlays"] as? [[String: Any]])?.first)
        XCTAssertEqual(Set(overlay.keys), ["kind", "id", "x", "y", "w", "h", "visible", "expected_inside_island"])
        XCTAssertEqual(overlay["expected_inside_island"] as? Bool, true)
        XCTAssertTrue(JSONSerialization.isValidJSONObject(row))
        // Only the allowed string fields; every geometry value is a number.
        for (key, value) in island where key != "visible" { XCTAssertTrue(value is Double, key) }
    }

    func testTopLeftConversion() {
        let r = IslandLayoutEvent.topLeft(CGRect(x: 10, y: 900, width: 100, height: 30), primaryHeight: 982)
        XCTAssertEqual(r, CGRect(x: 10, y: 52, width: 100, height: 30))
    }

    func testEmitsOnChangeOnly() {
        post("show"); post("resize"); post("resize")
        XCTAssertEqual(rows.count, 1, "unchanged geometry is not logged again")
        now += 1
        post("resize", width: 115)
        XCTAssertEqual(rows.count, 2)
    }

    func testStatusAndAppSwapsEmitAtTheSameWidth() {
        func details(status: String, apps: [String]) -> IslandLayoutDetails {
            IslandLayoutDetails(origin: CGPoint(x: 600, y: 6), ringOuterRect: nil, dotRect: nil,
                                voiceSlotRect: nil, voiceContents: "none", glyphs: [], status: status, appIdentity: apps)
        }
        post("show", details: details(status: "acting", apps: ["com.apple.calculator"]))
        now += 1
        post("state", details: details(status: "done", apps: ["com.apple.calculator"]))
        now += 1
        post("state", details: details(status: "done", apps: ["com.apple.finder"]))
        XCTAssertEqual(rows.count, 3, "status and app identity changes are logged even when geometry is unchanged")
        XCTAssertEqual(rows[1]["status"] as? String, "done")
        XCTAssertEqual(rows[2]["app_identity"] as? [String], ["com.apple.finder"])
    }

    func testLayoutDetailsContainSnappedRingDotVoiceAndGlyphGeometry() {
        let model = IslandModel()
        model.idleActivity = "duo"
        var use = IslandPluginUse(plugin: "acu", app: "Agent Computer Use", agent: "Samantha", last_seen_at: 10)
        use.target_app = "com.apple.calculator"
        model.apply(IslandSnapshot(kind: "listening", plugins: [use], samantha_on: true))
        let shape = model.shapeRect
        let details = IslandLayoutLogger.details(model: model, shape: shape, origin: CGPoint(x: 601, y: 7), scale: 2)
        XCTAssertNotNil(details.ringOuterRect)
        XCTAssertNotNil(details.dotRect)
        XCTAssertNotNil(details.voiceSlotRect)
        XCTAssertEqual(details.voiceContents, "listening")
        XCTAssertEqual(details.status, "listening")
        XCTAssertEqual(details.appIdentity, ["com.apple.calculator"])
        XCTAssertEqual(Set(details.glyphs.map(\.kind)), ["target", "app", "plugin", "harness"])
        XCTAssertTrue(details.glyphs.allSatisfy { $0.alpha == 1 })
    }

    func testTransitionSettlesOnlyAfterQuietAndRetainsFinalGeometry() throws {
        post("show")
        let id = try XCTUnwrap(rows.first?["transition_id"] as? String)
        XCTAssertEqual(rows.first?["transition_phase"] as? String, "start")
        now += 0.3
        post("resize", width: 100)
        settleScheduled.removeFirst()()
        XCTAssertEqual(rows.count, 2, "an early timer must not claim settlement")
        now += 0.5
        settleScheduled.removeFirst()()
        let settled = try XCTUnwrap(rows.last)
        XCTAssertEqual(settled["transition_id"] as? String, id)
        XCTAssertEqual(settled["transition_phase"] as? String, "settled")
        XCTAssertEqual(settled["transition_trigger"] as? String, "show")
        XCTAssertEqual((settled["island"] as? [String: Any])?["w"] as? Double, 100)
        now += 0.1
        post("hide", width: 75)
        XCTAssertNotEqual(rows.last?["transition_id"] as? String, id)
    }

    func testMovesCoalesceToOnePer250msWithTheLatestFrame() {
        post("show")
        for i in 1...10 {
            now += 0.02
            post("move", x: 600 + CGFloat(i))
        }
        XCTAssertEqual(rows.count, 1, "moves inside 250 ms wait")
        XCTAssertEqual(scheduled.count, 1, "one trailing flush")
        now += 0.3
        scheduled.removeFirst()()
        XCTAssertEqual(rows.count, 2)
        XCTAssertEqual((rows[1]["island"] as? [String: Any])?["x"] as? Double, 610, "latest frame wins")
        XCTAssertEqual(rows[1]["trigger"] as? String, "move")
    }

    func testCometCircleSitsInsideTheIslandForTheNormalLayout() {
        let model = IslandModel()
        model.idleActivity = "duo"
        var comet = IslandPluginUse(plugin: "comet-control", app: "Comet Control", agent: "Samantha", last_seen_at: 300)
        comet.target_app = nil
        model.apply(IslandSnapshot(kind: "acting", title: "Opening netflix.com", plugins: [comet]))
        let shape = model.shapeRect
        let origin = CGPoint(x: 400, y: 0)
        let overlays = IslandLayoutLogger.overlays(model: model, shape: shape, origin: origin)
        let island = IslandLayoutRect(shape.offsetBy(dx: origin.x, dy: origin.y))
        let circle = overlays.first { $0.kind == "comet_circle" }
        XCTAssertNotNil(circle)
        XCTAssertTrue(circle!.expectedInsideIsland)
        XCTAssertTrue(island.contains(circle!.rect), "comet circle within the island (2 pt tolerance)")
        for o in overlays where !o.expectedInsideIsland { XCTAssertFalse(island.contains(o.rect, tolerance: 0), o.kind) }
        XCTAssertTrue(overlays.contains { $0.kind == "plugin_mark" && !$0.expectedInsideIsland })
        XCTAssertTrue(overlays.contains { $0.kind == "harness_circle" && !$0.expectedInsideIsland }, "Samantha outside left")
    }

    func testAgentCursorOverlayHides() throws {
        post("show")
        logger.cursorOverlay(trigger: "overlay_show", rect: CGRect(x: 10, y: 10, width: 210, height: 90), visible: true)
        now += 1
        logger.cursorOverlay(trigger: "overlay_hide", rect: nil, visible: false)
        let last = try XCTUnwrap((rows.last?["overlays"] as? [[String: Any]])?.first)
        XCTAssertEqual(last["kind"] as? String, "agent_cursor")
        XCTAssertEqual(last["visible"] as? Bool, false)
        XCTAssertEqual(last["expected_inside_island"] as? Bool, false)
    }
}
