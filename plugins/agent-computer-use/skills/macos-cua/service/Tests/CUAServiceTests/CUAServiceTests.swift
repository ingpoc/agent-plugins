import XCTest
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
}
