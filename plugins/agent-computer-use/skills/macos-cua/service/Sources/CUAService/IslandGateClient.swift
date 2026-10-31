import Foundation

struct IslandGateAccount: Codable, Equatable {
    var id: String
    var provider: String
    var handle: String
    var name: String?
    var linked: [String]?
    var verified: Bool?
    var show_on_wall: Bool?
}

struct IslandGateConfig: Decodable {
    struct X: Decodable { let handle: String; let intent_url: URL; let preset_text: String }
    struct LinkedIn: Decodable { let share_url: URL; let preset_text: String; let follow_url: URL? }
    let x: X
    let linkedin: LinkedIn
}

struct IslandGateReply: Decodable {
    var session: String?
    var account: IslandGateAccount?
    var status: String?
    var entitlement: String?
    var verified: Bool?
    var show_on_wall: Bool?
}

struct IslandGateFailure: Error, Decodable, Equatable {
    let code: String
    var follow: Bool?
    var post: Bool?
    var device_name: String?
    var bound_at: String?
    var rebind_available_at: String?
    var httpStatus: Int?
    enum CodingKeys: String, CodingKey { case code = "error", follow, post, device_name, bound_at, rebind_available_at }
    init(code: String, follow: Bool? = nil, post: Bool? = nil, device_name: String? = nil, bound_at: String? = nil, rebind_available_at: String? = nil, httpStatus: Int? = nil) {
        self.code = code; self.follow = follow; self.post = post; self.device_name = device_name
        self.bound_at = bound_at; self.rebind_available_at = rebind_available_at; self.httpStatus = httpStatus
    }
    var message: String {
        switch code {
        case "not_signed_in", "session_expired": return "Sign in again to continue unlocking your island."
        case "follow_missing": return "Follow @gurusharan on X, then verify again."
        case "post_missing": return "Publish your ACU post on X, then verify again."
        case "post_url_invalid": return "Paste a public LinkedIn post link starting with https://www.linkedin.com/posts/ or /feed/update/."
        case "post_url_used": return "That LinkedIn post has already been used. Share a new post and paste its link."
        case "pending_review": return "Your post is being reviewed. We'll check while this window is open."
        case "rejected": return "We couldn't approve that post. Check the shared text and submit a new public post."
        case "device_conflict": return "Your island is active on another Mac. You can move it here."
        case "device_taken": return "Sign in with the account that unlocked this Mac."
        case "rebind_cooldown": return "Your island was moved recently. You can move it again on the date shown."
        case "device_moved": return "Your island was moved to another Mac. Sign in to move it back."
        case "revoked": return "Island access was withdrawn. Sign in again to check your account."
        case "x_unavailable": return "X verification is unavailable right now. Try again later or use LinkedIn."
        case "not_found": return "This Mac has no island access yet. Sign in to unlock it."
        case "invalid_entitlement": return "Island access couldn't be validated for this Mac. Sign in to renew it."
        case "keychain": return "We couldn't securely save island access. Unlock your login Keychain and try again."
        case "device_identity": return "We couldn't identify this Mac. Restart ACU and try again."
        case "offline": return "We couldn't reach the island service. Check your connection and try again."
        case "invalid_callback": return "Sign-in couldn't be completed. Please try again."
        default: return "The island service couldn't complete your request. Please try again."
        }
    }
}

final class IslandGateClient {
    let baseURL: URL
    private let session: URLSession
    init(baseURL: URL, session: URLSession = .shared) { self.baseURL = baseURL; self.session = session }
    func startURL(provider: String, deviceID: String, deviceName: String, challenge: String) -> URL {
        var parts = URLComponents(url: baseURL.appendingPathComponent("v1/auth/\(provider)/start"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "device_id", value: deviceID), .init(name: "device_name", value: deviceName), .init(name: "app_challenge", value: challenge), .init(name: "link", value: "0")]
        return parts.url!
    }
    func config() async throws -> IslandGateConfig { try await request("v1/config", as: IslandGateConfig.self) }
    func request<T: Decodable>(_ path: String, body: [String: Any]? = nil, bearer: String? = nil, as type: T.Type) async throws -> T {
        let data = try await send(path, body: body, bearer: bearer)
        do { return try JSONDecoder().decode(type, from: data) }
        catch { throw IslandGateFailure(code: "server_error") }
    }
    func setWallVisibility(_ show: Bool, bearer: String) async throws {
        _ = try await send("v1/wall", body: ["show": show], bearer: bearer)
    }
    private func send(_ path: String, body: [String: Any]?, bearer: String?) async throws -> Data {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.timeoutInterval = 30
        req.cachePolicy = .reloadIgnoringLocalCacheData
        if let body { req.httpMethod = "POST"; req.httpBody = try JSONSerialization.data(withJSONObject: body); req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let bearer { req.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
        let data: Data; let response: URLResponse
        do { (data, response) = try await session.data(for: req) }
        catch { throw IslandGateFailure(code: "offline") }
        guard let http = response as? HTTPURLResponse else { throw IslandGateFailure(code: "server_error") }
        guard (200..<300).contains(http.statusCode) else {
            var failure = (try? JSONDecoder().decode(IslandGateFailure.self, from: data)) ?? IslandGateFailure(code: "server_error")
            failure.httpStatus = http.statusCode
            throw failure
        }
        return data
    }
}
