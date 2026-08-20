import Foundation
import CoreGraphics

struct JSONRPCRequest: Codable {
    let jsonrpc: String
    let method: String
    let params: [String: AnyCodable]?
    let id: AnyCodable?

    func param<T>(_ key: String) -> T? {
        params?[key]?.value as? T
    }

    func paramInt(_ key: String) -> Int? {
        if let v: Int = param(key) { return v }
        if let v: Double = param(key) { return Int(v) }
        return nil
    }

    /// JSON whole numbers decode as Int first; `param("x") as Double` then misses.
    func paramDouble(_ key: String) -> Double? {
        if let v: Double = param(key) { return v }
        if let v: Int = param(key) { return Double(v) }
        return nil
    }
}

struct JSONRPCResponse: Codable {
    let jsonrpc: String
    let id: AnyCodable?
    let result: AnyCodable?
    let error: RPCError?

    init(id: AnyCodable?, result: Any? = nil, error: RPCError? = nil) {
        self.jsonrpc = "2.0"
        self.id = id
        self.result = result.map { AnyCodable($0) }
        self.error = error
    }

    static func success(id: AnyCodable?, _ value: Any) -> JSONRPCResponse {
        JSONRPCResponse(id: id, result: value)
    }

    static func error(id: AnyCodable?, code: Int, message: String) -> JSONRPCResponse {
        JSONRPCResponse(id: id, error: RPCError(code: code, message: message))
    }
}

struct RPCError: Codable {
    let code: Int
    let message: String
}

/// Length-prefixed frame codec: 4-byte LE length + UTF-8 JSON body.
enum FrameCodec {
    static func encode(_ response: JSONRPCResponse) throws -> Data {
        let body = try JSONEncoder().encode(response)
        var length = UInt32(body.count).littleEndian
        var frame = Data(bytes: &length, count: 4)
        frame.append(body)
        return frame
    }

    static func decode(from buffer: inout Data) -> JSONRPCRequest? {
        guard buffer.count >= 4 else { return nil }
        let length = buffer.withUnsafeBytes {
            $0.loadUnaligned(as: UInt32.self)
        }
        let frameLen = Int(UInt32(littleEndian: length))
        guard buffer.count >= 4 + frameLen else { return nil }
        let body = buffer.subdata(in: 4..<(4 + frameLen))
        buffer.removeSubrange(0..<(4 + frameLen))
        return try? JSONDecoder().decode(JSONRPCRequest.self, from: body)
    }
}

/// Type-erased Codable wrapper for JSON-RPC params/results.
struct AnyCodable: Codable {
    let value: Any

    init(_ value: Any) { self.value = value }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            value = NSNull()
        } else if let b = try? container.decode(Bool.self) {
            value = b
        } else if let i = try? container.decode(Int.self) {
            value = i
        } else if let d = try? container.decode(Double.self) {
            value = d
        } else if let s = try? container.decode(String.self) {
            value = s
        } else if let a = try? container.decode([AnyCodable].self) {
            value = a.map(\.value)
        } else if let d = try? container.decode([String: AnyCodable].self) {
            value = d.mapValues(\.value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON type"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch value {
        case is NSNull:
            try container.encodeNil()
        case let b as Bool:
            try container.encode(b)
        case let i as Int:
            try container.encode(i)
        case let d as Double:
            try container.encode(d)
        case let f as CGFloat:
            try container.encode(Double(f))
        case let s as String:
            try container.encode(s)
        case let a as [Any]:
            try container.encode(a.map { AnyCodable($0) })
        case let d as [String: Any]:
            try container.encode(d.mapValues { AnyCodable($0) })
        default:
            try container.encodeNil()
        }
    }
}
