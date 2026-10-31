import XCTest
import Foundation
import CryptoKit
@testable import CUAService

final class IslandGateURLProtocol: URLProtocol, @unchecked Sendable {
    static var handler: ((URLRequest) throws -> (Int, Data))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, data) = try Self.handler!(request)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
    static func client() -> IslandGateClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [IslandGateURLProtocol.self]
        let client = IslandGateClient(baseURL: URL(string: "https://gate.example")!, session: URLSession(configuration: config))
        client.requestTimeout = 0.05
        client.coldStartBudget = 0.35
        client.retryBackoffBase = 0.05
        return client
    }
}

final class IslandGateClientTests: XCTestCase {
    override func tearDown() { IslandGateURLProtocol.handler = nil; super.tearDown() }
    private static func body(_ request: URLRequest) throws -> Data {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }
                data.append(contentsOf: bytes.prefix(count))
            }
        }
        return data
    }
    func testConfigUsesServerShareLinksAndCopy() async throws {
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.url?.path, "/v1/config")
            return (200, Data(#"{"x":{"handle":"guru","profile_url":"https://x.com/guru","intent_url":"https://x.com/intent/post?text=server","preset_text":"server"},"linkedin":{"share_url":"https://www.linkedin.com/sharing/share-offsite/?url=server","preset_text":"from server","follow_url":"https://www.linkedin.com/in/example"},"poll_seconds":60,"grace_days":14,"check_eta_text":"usually within the hour"}"#.utf8))
        }
        let config = try await IslandGateURLProtocol.client().config()
        XCTAssertEqual(config.x.profile_url.absoluteString, "https://x.com/guru")
        XCTAssertEqual(config.x.intent_url.absoluteString, "https://x.com/intent/post?text=server")
        XCTAssertEqual(config.linkedin.preset_text, "from server")
        XCTAssertEqual(config.poll_seconds, 60)
    }
    func testClaimRequestBodyAndSignatureHeaders() async throws {
        let device = IslandGateDevice(id: String(repeating: "c", count: 64), key: P256.Signing.PrivateKey())
        let body: [String: Any] = ["provider": "x", "handle": "friend", "post_url": NSNull(), "device_pubkey": device.publicKey, "device_name": "Mac", "show_on_wall": false]
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.httpMethod, "POST")
            XCTAssertEqual(req.url?.path, "/v1/claims")
            XCTAssertEqual(req.value(forHTTPHeaderField: "X-ACU-Device"), device.id)
            let ts = Int64(req.value(forHTTPHeaderField: "X-ACU-TS")!)!
            let payload = try Self.body(req)
            let json = try XCTUnwrap(JSONSerialization.jsonObject(with: payload) as? [String: Any])
            XCTAssertEqual(json["provider"] as? String, "x")
            XCTAssertEqual(json["handle"] as? String, "friend")
            let message = IslandGateDevice.signMessage(action: "claim", deviceID: device.id, timestamp: ts, body: payload)
            let sigData = try XCTUnwrap(Data(base64Encoded: req.value(forHTTPHeaderField: "X-ACU-Sig")!))
            let signature = try P256.Signing.ECDSASignature(derRepresentation: sigData)
            XCTAssertTrue(device.key.publicKey.isValidSignature(signature, for: Data(message.utf8)))
            return (201, Data(#"{"claim_id":"claim-1","status":"pending_verification","checks":{"follow":"pending","post":"pending"},"poll_after":60}"#.utf8))
        }
        _ = try await IslandGateURLProtocol.client().createClaim(device: device, body: body)
    }
    func testPollUsesGETWithEmptyBodyHash() async throws {
        let device = IslandGateDevice(id: String(repeating: "p", count: 64), key: P256.Signing.PrivateKey())
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.httpMethod, "GET")
            XCTAssertEqual(req.url?.path, "/v1/claims/abc")
            let body = try Self.body(req)
            XCTAssertTrue(body.isEmpty)
            let ts = Int64(req.value(forHTTPHeaderField: "X-ACU-TS")!)!
            let message = IslandGateDevice.signMessage(action: "poll", deviceID: device.id, timestamp: ts, body: body)
            let sigData = try XCTUnwrap(Data(base64Encoded: req.value(forHTTPHeaderField: "X-ACU-Sig")!))
            let signature = try P256.Signing.ECDSASignature(derRepresentation: sigData)
            XCTAssertTrue(device.key.publicKey.isValidSignature(signature, for: Data(message.utf8)))
            return (200, Data(#"{"status":"pending_verification","checks":{"follow":"confirmed","post":"pending"}}"#.utf8))
        }
        let reply = try await IslandGateURLProtocol.client().pollClaim(id: "abc", device: device)
        XCTAssertEqual(reply.checks?.follow, "confirmed")
    }
    func testEveryContractErrorDecodedWithFriendlyCopy() async throws {
        let codes = ["invalid_handle", "post_url_invalid", "post_url_used", "post_not_found", "post_author_mismatch", "post_too_old", "post_missing_tag", "follow_missing", "post_missing", "claim_expired", "rejected", "device_conflict", "device_taken", "rebind_cooldown", "device_moved", "revoked", "bad_signature", "rate_limited", "server_error", "not_found"]
        for code in codes {
            IslandGateURLProtocol.handler = { _ in
                (422, try JSONSerialization.data(withJSONObject: ["error": code, "message": "DO NOT SHOW RAW SERVER TEXT", "device_name": "Studio", "rebind_available_at": "2026-11-01T00:00:00Z"]))
            }
            do {
                let device = IslandGateDevice(id: String(repeating: "d", count: 64), key: P256.Signing.PrivateKey())
                _ = try await IslandGateURLProtocol.client().createClaim(device: device, body: ["provider": "x", "handle": "a", "post_url": NSNull(), "device_pubkey": device.publicKey, "device_name": "Mac", "show_on_wall": false])
                XCTFail("Expected failure for \(code)")
            } catch let failure as IslandGateFailure {
                XCTAssertEqual(failure.code, code)
                XCTAssertFalse(failure.message.contains(code))
                XCTAssertFalse(failure.message.contains("DO NOT SHOW"))
            }
        }
    }
    func testRejectedReasonCopy() {
        XCTAssertEqual(IslandGateFailure.rejectedMessage(reason: "follow_missing"), IslandGateFailure.friendlyMessage(for: "follow_missing"))
        XCTAssertEqual(IslandGateFailure.rejectedMessage(reason: "post_author_mismatch"), IslandGateFailure.friendlyMessage(for: "post_author_mismatch"))
        XCTAssertEqual(IslandGateFailure.rejectedMessage(reason: "claim_expired"), IslandGateFailure.friendlyMessage(for: "claim_expired"))
    }
    func testMalformedResponseHasFriendlyError() async {
        IslandGateURLProtocol.handler = { _ in (500, Data("not json".utf8)) }
        do {
            _ = try await IslandGateURLProtocol.client().config()
            XCTFail("Expected error")
        } catch { XCTAssertEqual((error as? IslandGateFailure)?.code, "server_error") }
    }
}

final class IslandGateValidationTests: XCTestCase {
    func testHandleValidation() {
        XCTAssertEqual(IslandGateValidation.normalizeXHandle("@friend"), "friend")
        XCTAssertEqual(IslandGateValidation.normalizeXHandle("friend"), "friend")
        XCTAssertNil(IslandGateValidation.normalizeXHandle("bad handle"))
        XCTAssertNil(IslandGateValidation.normalizeXHandle(String(repeating: "a", count: 16)))
    }
    func testLinkedInProfileValidation() {
        XCTAssertEqual(IslandGateValidation.normalizeLinkedInProfile("https://www.linkedin.com/in/Alice"), "/in/alice")
        XCTAssertNil(IslandGateValidation.normalizeLinkedInProfile("https://example.com/in/alice"))
    }
}
