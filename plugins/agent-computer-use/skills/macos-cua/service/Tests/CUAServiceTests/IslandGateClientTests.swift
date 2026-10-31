import XCTest
import Foundation
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
        return IslandGateClient(baseURL: URL(string: "https://gate.example")!, session: URLSession(configuration: config))
    }
}

final class IslandGateClientTests: XCTestCase {
    override func tearDown() { IslandGateURLProtocol.handler = nil; super.tearDown() }
    func testStartURLQueryItems() {
        let client = IslandGateURLProtocol.client()
        for provider in ["x", "linkedin"] {
            let url = client.startURL(provider: provider, deviceID: "hex", deviceName: "Guru's Mac & Studio", challenge: "abc_-")
            XCTAssertEqual(url.path, "/v1/auth/\(provider)/start")
            let items = Dictionary(uniqueKeysWithValues: URLComponents(url: url, resolvingAgainstBaseURL: false)!.queryItems!.map { ($0.name, $0.value!) })
            XCTAssertEqual(items, ["device_id": "hex", "device_name": "Guru's Mac & Studio", "app_challenge": "abc_-", "link": "0"])
            XCTAssertNil(items["session"])
        }
    }
    func testConfigUsesServerShareLinksAndCopy() async throws {
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.url?.path, "/v1/config")
            return (200, Data(#"{"x":{"handle":"guru","intent_url":"https://x.com/intent/post?text=server","preset_text":"server"},"linkedin":{"share_url":"https://www.linkedin.com/sharing/share-offsite/?url=server","preset_text":"from server","follow_url":"https://www.linkedin.com/in/example"}}"#.utf8))
        }
        let config = try await IslandGateURLProtocol.client().config()
        XCTAssertEqual(config.x.intent_url.absoluteString, "https://x.com/intent/post?text=server")
        XCTAssertEqual(config.linkedin.preset_text, "from server")
        XCTAssertEqual(config.linkedin.follow_url?.path, "/in/example")
    }
    func testEveryContractErrorDecodedWithFriendlyCopy() async throws {
        let codes = ["not_signed_in", "session_expired", "follow_missing", "post_missing", "post_url_invalid", "post_url_used", "pending_review", "rejected", "device_conflict", "device_taken", "rebind_cooldown", "device_moved", "revoked", "x_unavailable", "server_error", "not_found"]
        for code in codes {
            IslandGateURLProtocol.handler = { _ in
                (422, try JSONSerialization.data(withJSONObject: ["error": code, "message": "DO NOT SHOW RAW SERVER TEXT", "follow": true, "post": false, "device_name": "Studio", "bound_at": "2026-10-20T00:00:00Z", "rebind_available_at": "2026-11-01T00:00:00Z"]))
            }
            do {
                let _: IslandGateReply = try await IslandGateURLProtocol.client().request("v1/verify/x", body: [:], as: IslandGateReply.self)
                XCTFail("Expected failure for \(code)")
            } catch let failure as IslandGateFailure {
                XCTAssertEqual(failure.code, code)
                XCTAssertEqual(failure.follow, true); XCTAssertEqual(failure.post, false)
                XCTAssertEqual(failure.device_name, "Studio"); XCTAssertEqual(failure.httpStatus, 422)
                XCTAssertFalse(failure.message.contains(code)); XCTAssertFalse(failure.message.contains("DO NOT SHOW"))
            }
        }
    }
    func testPending202AndAuthorization() async throws {
        IslandGateURLProtocol.handler = { req in
            XCTAssertEqual(req.httpMethod, "POST")
            XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer local-test-session")
            return (202, Data(#"{"status":"pending_review"}"#.utf8))
        }
        let reply = try await IslandGateURLProtocol.client().request("v1/verify/linkedin", body: ["post_url": "https://www.linkedin.com/posts/example"], bearer: "local-test-session", as: IslandGateReply.self)
        XCTAssertEqual(reply.status, "pending_review")
    }
    func testMalformedResponseHasFriendlyError() async {
        IslandGateURLProtocol.handler = { _ in (500, Data("not json".utf8)) }
        do { let _: IslandGateReply = try await IslandGateURLProtocol.client().request("v1/status", as: IslandGateReply.self); XCTFail("Expected error") }
        catch { XCTAssertEqual((error as? IslandGateFailure)?.code, "server_error") }
    }
}
