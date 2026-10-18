import CoreGraphics
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
