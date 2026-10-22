import XCTest
import Logging
@testable import CUAService

final class JSONRPCCodecTests: XCTestCase {
    func testFrameEncodeDecode() throws {
        let response = JSONRPCResponse.success(
            id: AnyCodable(1),
            ["ok": true, "method": "test"] as [String: Any]
        )
        let frame = try FrameCodec.encode(response)
        XCTAssertTrue(frame.count > 4)

        // Verify length prefix
        let length = frame.withUnsafeBytes {
            $0.loadUnaligned(as: UInt32.self)
        }
        XCTAssertEqual(Int(UInt32(littleEndian: length)), frame.count - 4)
    }

    func testDecodeRequest() {
        let json = """
        {"jsonrpc":"2.0","method":"list_apps","params":{},"id":1}
        """.data(using: .utf8)!
        var length = UInt32(json.count).littleEndian
        var buffer = Data(bytes: &length, count: 4)
        buffer.append(json)

        let request = FrameCodec.decode(from: &buffer)
        XCTAssertNotNil(request)
        XCTAssertEqual(request?.method, "list_apps")
        XCTAssertEqual(request?.jsonrpc, "2.0")
    }

    func testDecodeIncompleteBuffer() {
        var buffer = Data([0x10, 0x00, 0x00, 0x00]) // says 16 bytes but empty
        let request = FrameCodec.decode(from: &buffer)
        XCTAssertNil(request)
        XCTAssertEqual(buffer.count, 4) // buffer unchanged
    }

    func testAnyCodableRoundTrip() throws {
        let original: [String: Any] = [
            "string": "hello",
            "int": 42,
            "double": 3.14,
            "bool": true,
            "null": NSNull(),
            "array": [1, 2, 3],
            "nested": ["key": "value"],
        ]
        let encoded = try JSONEncoder().encode(AnyCodable(original))
        let decoded = try JSONDecoder().decode(AnyCodable.self, from: encoded)
        let dict = decoded.value as? [String: Any]
        XCTAssertNotNil(dict)
        XCTAssertEqual(dict?["string"] as? String, "hello")
        XCTAssertEqual(dict?["int"] as? Int, 42)
        XCTAssertEqual(dict?["bool"] as? Bool, true)
    }

    func testErrorResponse() throws {
        let response = JSONRPCResponse.error(
            id: AnyCodable(5), code: -32601, message: "Method not found"
        )
        let frame = try FrameCodec.encode(response)
        XCTAssertTrue(frame.count > 4)

        let body = frame.subdata(in: 4..<frame.count)
        let json = try JSONDecoder().decode(JSONRPCResponse.self, from: body)
        XCTAssertNotNil(json.error)
        XCTAssertEqual(json.error?.code, -32601)
        XCTAssertEqual(json.error?.message, "Method not found")
    }
}

final class RequestParamTests: XCTestCase {
    func testParamExtraction() {
        let json = """
        {"jsonrpc":"2.0","method":"click","params":{"app":"Safari","element_index":5,"x":100.5},"id":1}
        """.data(using: .utf8)!
        let request = try! JSONDecoder().decode(JSONRPCRequest.self, from: json)

        let app: String? = request.param("app")
        XCTAssertEqual(app, "Safari")

        let idx = request.paramInt("element_index")
        XCTAssertEqual(idx, 5)

        let x: Double? = request.param("x")
        XCTAssertEqual(x, 100.5)

        let missing: String? = request.param("nonexistent")
        XCTAssertNil(missing)
    }

    func testParamCGWindowIDRejectsNegativeAndOverflow() throws {
        let negative = """
        {"jsonrpc":"2.0","method":"click","params":{"windowId":-1},"id":1}
        """.data(using: .utf8)!
        let overflow = """
        {"jsonrpc":"2.0","method":"click","params":{"windowId":4294967296},"id":1}
        """.data(using: .utf8)!
        let ok = """
        {"jsonrpc":"2.0","method":"click","params":{"windowId":42},"id":1}
        """.data(using: .utf8)!
        let negReq = try JSONDecoder().decode(JSONRPCRequest.self, from: negative)
        let overReq = try JSONDecoder().decode(JSONRPCRequest.self, from: overflow)
        let okReq = try JSONDecoder().decode(JSONRPCRequest.self, from: ok)
        XCTAssertThrowsError(try negReq.paramCGWindowID("windowId"))
        XCTAssertThrowsError(try overReq.paramCGWindowID("windowId"))
        XCTAssertEqual(try okReq.paramCGWindowID("windowId"), 42)
    }

    func testPinPlanParamsCopiesWindowWithoutOverwrite() {
        let pinned = MethodRouter.pinPlanParams(["element_index": 1], windowID: 77)
        XCTAssertEqual(pinned["windowId"] as? Int, 77)
        let own = MethodRouter.pinPlanParams(["windowId": 9], windowID: 77)
        XCTAssertEqual(own["windowId"] as? Int, 9)
        let snake = MethodRouter.pinPlanParams(["window_id": 9], windowID: 77)
        XCTAssertNil(snake["windowId"])
        XCTAssertEqual(snake["window_id"] as? Int, 9)
    }
}

final class AppResolverTests: XCTestCase {
    func testFindsFinderInCoreServices() {
        let resolver = AppResolver(logger: Logger(label: "AppResolverTests"))
        XCTAssertEqual(
            resolver.applicationURL(named: "Finder")?.path,
            "/System/Library/CoreServices/Finder.app"
        )
    }
}

final class CometMotionTests: XCTestCase {
    private let a = CGPoint(x: 100, y: 100)
    private let b = CGPoint(x: 500, y: 100)

    func testDefaultStyleIsCometSwoop() {
        XCTAssertEqual(CursorMotionStyle.default, .cometSwoop)
        let s = CursorOverlay.resolveMotionSettings(env: [:], storedStyle: nil, storedRecording: nil)
        XCTAssertEqual(s.style, .cometSwoop)
        XCTAssertFalse(s.recording)
    }

    func testEnvOverridesStoredAndLinearSelectable() {
        let s = CursorOverlay.resolveMotionSettings(
            env: ["MACOS_CUA_CURSOR_MOTION": "linear", "MACOS_CUA_CURSOR_RECORDING": "1"],
            storedStyle: "comet_swoop", storedRecording: false)
        XCTAssertEqual(s.style, .linear)
        XCTAssertTrue(s.recording)
        let stored = CursorOverlay.resolveMotionSettings(
            env: ["MACOS_CUA_CURSOR_MOTION": "wobble"], storedStyle: "linear", storedRecording: true)
        XCTAssertEqual(stored.style, .linear)
        XCTAssertTrue(stored.recording)
        XCTAssertNil(CursorOverlay.parseBool("maybe"))
    }

    func testFastModeKeepsLegacyBudget() {
        for style in CursorMotionStyle.allCases {
            XCTAssertEqual(CometMotion.duration(style: style, recording: false, distance: 1500, targetWidth: 4), 0.12)
        }
        XCTAssertEqual(CometMotion.trailSeconds(recording: false), 0.08)
        // linear ignores recording mode
        XCTAssertEqual(CometMotion.duration(style: .linear, recording: true, distance: 1500, targetWidth: 4), 0.12)
    }

    func testRecordingModeUsesCuaFitts() {
        // 150 + 120*log2(400/40+1) = 565.1 ms * 1.15
        let d = CometMotion.duration(style: .cometSwoop, recording: true, distance: 400, targetWidth: 40)
        XCTAssertEqual(d, (150 + 120 * log2(11.0)) * 1.15 / 1000, accuracy: 1e-9)
        XCTAssertEqual(CometMotion.fittsSeconds(distance: 1, targetWidth: 100), 0.345, accuracy: 1e-9)
        XCTAssertEqual(CometMotion.fittsSeconds(distance: 100_000, targetWidth: 1), 1.15, accuracy: 1e-9)
        // W floors at 4
        XCTAssertEqual(CometMotion.fittsSeconds(distance: 400, targetWidth: 1),
                       CometMotion.fittsSeconds(distance: 400, targetWidth: 4))
        XCTAssertEqual(CometMotion.trailSeconds(recording: true), 0.18)
    }

    func testEaseInOutCubicEndpoints() {
        XCTAssertEqual(CometMotion.easeInOutCubic(0), 0)
        XCTAssertEqual(CometMotion.easeInOutCubic(0.5), 0.5, accuracy: 1e-12)
        XCTAssertEqual(CometMotion.easeInOutCubic(1), 1)
    }

    func testSwoopLandsExactlyAndArcsUpward() {
        let c = CometMotion.controlPoints(from: a, to: b, screen: nil)
        // Cua offsets: 0.168 and 0.192 of the chord, on the upward side.
        XCTAssertEqual(c.0.y - a.y, 400 * 0.168, accuracy: 1e-6)
        XCTAssertEqual(c.1.y - b.y, 400 * 0.192, accuracy: 1e-6)
        XCTAssertEqual(c.0.x, 220, accuracy: 1e-6)
        for d in [0.12, 0.6] {
            let end = CometMotion.position(style: .cometSwoop, from: a, to: b, controls: c, elapsed: d, duration: d)
            XCTAssertEqual(end, b)
            let mid = CometMotion.position(style: .cometSwoop, from: a, to: b, controls: c, elapsed: d / 2, duration: d)
            XCTAssertEqual(mid.y - a.y, 400 * 0.135, accuracy: 1e-6)  // 3/8*(c1+c2) offsets
            XCTAssertEqual(CometMotion.position(style: .cometSwoop, from: a, to: b, controls: c,
                                                elapsed: d + 1, duration: d), b)
        }
        // Leftward horizontal still bends up.
        let back = CometMotion.controlPoints(from: b, to: a, screen: nil)
        XCTAssertGreaterThan(back.0.y, a.y)
    }

    func testLinearStaysStraight() {
        let c = CometMotion.controlPoints(from: a, to: b, screen: nil)
        let mid = CometMotion.position(style: .linear, from: a, to: b, controls: c, elapsed: 0.06, duration: 0.12)
        XCTAssertEqual(mid.y, a.y)
        XCTAssertEqual(CometMotion.position(style: .linear, from: a, to: b, controls: c, elapsed: 0.12, duration: 0.12), b)
    }

    func testShortHopIsStraight() {
        let near = CGPoint(x: 110, y: 100)
        let c = CometMotion.controlPoints(from: a, to: near, screen: nil)
        XCTAssertEqual(c.0.y, a.y)
        XCTAssertEqual(c.1.y, a.y)
    }

    func testControlPointsClampedToScreen() {
        let top = CGPoint(x: 100, y: 990), top2 = CGPoint(x: 1500, y: 990)
        let screen = CGRect(x: 0, y: 0, width: 1600, height: 1000)
        let c = CometMotion.controlPoints(from: top, to: top2, screen: screen)
        XCTAssertLessThanOrEqual(c.0.y, 1000)
        XCTAssertLessThanOrEqual(c.1.y, 1000)
        for i in 0...20 {
            let p = CometMotion.bezier(top, c.0, c.1, top2, Double(i) / 20)
            XCTAssertTrue(screen.insetBy(dx: -0.01, dy: -0.01).contains(p))
        }
    }

    func testTrailSpec() {
        let c = CometMotion.controlPoints(from: a, to: b, screen: nil)
        let dur = 0.6
        let sample: (TimeInterval) -> CGPoint = {
            CometMotion.position(style: .cometSwoop, from: self.a, to: self.b, controls: c, elapsed: $0, duration: dur)
        }
        let segs = CometMotion.trail(at: 0.3, secs: 0.18, sample: sample)
        XCTAssertFalse(segs.isEmpty)
        XCTAssertLessThanOrEqual(segs.count, 26)
        for (x, y) in zip(segs, segs.dropFirst()) {
            XCTAssertLessThan(x.alpha, y.alpha)
            XCTAssertLessThan(x.width, y.width)
        }
        XCTAssertLessThanOrEqual(segs.last!.alpha, 0.38 + 1e-9)
        XCTAssertLessThanOrEqual(segs.last!.width, 9 + 1e-9)
        // Head anchored behind the tip.
        let tip = sample(0.3)
        XCTAssertGreaterThan(hypot(segs.last!.b.x - tip.x, segs.last!.b.y - tip.y), 10)
        // Stationary (landed) trail is empty.
        XCTAssertTrue(CometMotion.trail(at: dur + 0.5, secs: 0.18, sample: sample).isEmpty)
        // Fast mode trail exists too.
        let fast: (TimeInterval) -> CGPoint = {
            CometMotion.position(style: .cometSwoop, from: self.a, to: self.b, controls: c, elapsed: $0, duration: 0.12)
        }
        XCTAssertFalse(CometMotion.trail(at: 0.06, secs: 0.08, sample: fast).isEmpty)
    }

    func testPointerShrinkKeepsHotspot() {
        XCTAssertEqual(HermesPointerView.pointerSize, 21)
        func tip(_ r: NSRect) -> CGPoint {
            let s = r.width / 512  // path tip ~ (4, 4) in the viewBox; container at (-4, 30)
            return CGPoint(x: -4 + r.minX + 4 * s, y: 30 + r.maxY - 4 * s)
        }
        let new = tip(HermesPointerView.drawRect)
        let old = tip(NSRect(x: 18, y: 18, width: 28, height: 28))
        XCTAssertEqual(new.x, old.x, accuracy: 0.1)
        XCTAssertEqual(new.y, old.y, accuracy: 0.1)
        XCTAssertEqual(new.x, CursorOverlay.artHotspot.x, accuracy: 1)
        XCTAssertEqual(new.y, CursorOverlay.artHotspot.y, accuracy: 1)
        XCTAssertEqual(CursorOverlay.hotspot.x, 15 + CursorOverlay.trailMargin)
    }
}

final class LabelAliasTests: XCTestCase {
    private func element(_ index: Int, _ label: String?) -> AXElement {
        AXElement(
            index: index,
            role: "AXButton",
            label: label,
            value: nil,
            frame: CGRect(x: 0, y: 0, width: 1, height: 1),
            actions: [],
            children: [],
            parentIndex: nil,
            depth: 1
        )
    }

    private func snapshot(_ elements: [AXElement]) -> AXSnapshot {
        AXSnapshot(elements: elements, markdown: "", windowFrame: nil, timestamp: 0)
    }

    /// Calculator retitles its clear button between "All Clear" and "Clear".
    /// A plan naming either spelling must resolve against the current tree.
    func testRetitledClearResolvesFromEitherSpelling() {
        let walker = AXTreeWalker(logger: Logger(label: "LabelAliasTests"))

        let showsClear = snapshot([element(0, "Clear"), element(1, "Delete")])
        XCTAssertEqual(walker.findElementByLabel(showsClear, label: "All Clear")?.index, 0)
        XCTAssertEqual(walker.findElementByLabel(showsClear, label: "Clear")?.index, 0)

        let showsAllClear = snapshot([element(0, "All Clear"), element(1, "Delete")])
        XCTAssertEqual(walker.findElementByLabel(showsAllClear, label: "Clear")?.index, 0)
        XCTAssertEqual(walker.findElementByLabel(showsAllClear, label: "All Clear")?.index, 0)
    }

    func testExactLabelStillWinsOverAlias() {
        let walker = AXTreeWalker(logger: Logger(label: "LabelAliasTests"))
        let both = snapshot([element(3, "All Clear"), element(7, "Clear")])
        XCTAssertEqual(walker.findElementByLabel(both, label: "Clear")?.index, 7)
        XCTAssertEqual(walker.findElementByLabel(both, label: "All Clear")?.index, 3)
    }

    func testUnknownLabelIsStillNil() {
        let walker = AXTreeWalker(logger: Logger(label: "LabelAliasTests"))
        let tree = snapshot([element(0, "Clear"), element(1, "Delete")])
        XCTAssertNil(walker.findElementByLabel(tree, label: "Tangent"))
    }
}

final class RuntimeContractTests: XCTestCase {
    func testRejectsStaleMissingAndChangedClientIdentity() {
        let expected: [String: Any] = ["version": "1.2.3", "sha256": "candidate"]
        XCTAssertTrue(MethodRouter.contractMatches(["version": "1.2.3", "sha256": "candidate"], expected: expected))
        XCTAssertFalse(MethodRouter.contractMatches(nil, expected: expected))
        XCTAssertFalse(MethodRouter.contractMatches(["version": "1.2.2", "sha256": "candidate"], expected: expected))
        XCTAssertFalse(MethodRouter.contractMatches(["version": "1.2.3", "sha256": "old"], expected: expected))
        XCTAssertFalse(MethodRouter.contractMatches([:], expected: [:]))
    }
}
