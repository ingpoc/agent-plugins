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
    struct X: Decodable { let handle: String; let profile_url: URL; let intent_url: URL; let preset_text: String }
    struct LinkedIn: Decodable { let share_url: URL; let preset_text: String; let follow_url: URL? }
    struct Match: Decodable { let tag: String?; let ref_x: String?; let ref_li: String? }
    let x: X
    let linkedin: LinkedIn
    let match: Match?
    let grace_days: Int?
    let poll_seconds: Int?
    let check_eta_text: String?
}

extension IslandGateConfig {
    static let bundled: IslandGateConfig = {
        let xText = "Just unlocked the Dynamic Island in Agent Computer Use. My agents' runs now show live at the top of my Mac. Free, by @gurusharan https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-x #AgentComputerUse"
        let linkedInText = "Just unlocked the Dynamic Island in Agent Computer Use. My agents' runs now show live at the top of my Mac. Free, by Gurusharan Gupta https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-li #AgentComputerUse"
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_.!~*'()"))
        func encoded(_ value: String) -> String { value.addingPercentEncoding(withAllowedCharacters: allowed)! }
        return IslandGateConfig(
            x: X(handle: "gurusharan", profile_url: URL(string: "https://x.com/gurusharan")!, intent_url: URL(string: "https://x.com/intent/post?text=\(encoded(xText))")!, preset_text: xText),
            linkedin: LinkedIn(share_url: URL(string: "https://www.linkedin.com/sharing/share-offsite/?url=\(encoded("https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-li"))")!, preset_text: linkedInText, follow_url: URL(string: "https://www.linkedin.com/in/gurusharangupta/")!),
            match: nil, grace_days: 14, poll_seconds: 60, check_eta_text: "usually within the hour")
    }()

    var linkedInFollowURL: URL? {
        guard let url = linkedin.follow_url, url.scheme?.lowercased() == "https", let host = url.host,
              !host.lowercased().contains("placeholder"), !host.lowercased().contains("replace"),
              !url.path.lowercased().contains("placeholder"), !url.path.lowercased().contains("replace_with"),
              !url.path.isEmpty, url.path != "/" else { return nil }
        return url
    }
}

struct IslandGateChecks: Decodable, Equatable {
    let follow: String
    let post: String
}

struct IslandGateClaimResponse: Decodable {
    var claim_id: String?
    var status: String?
    var checks: IslandGateChecks?
    var poll_after: Int?
    var reason: String?
    var entitlement: String?
    var rebind_available_at: String?
    var device_name: String?
}

struct IslandGateEntitlementResponse: Decodable {
    let entitlement: String?
}

struct IslandGateWallResponse: Decodable {
    let show: Bool?
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
    var message: String { Self.friendlyMessage(for: code) }
    static func friendlyMessage(for code: String) -> String {
        switch code {
        case "invalid_handle": return "Enter a valid X handle (letters, numbers, and underscores only)."
        case "post_url_invalid": return "Paste a public post link starting with https://, or leave it blank."
        case "post_url_used": return "That post has already been used. Share a new one and try again."
        case "post_not_found": return "We couldn't find that post. Check the link or leave it blank so we can search."
        case "post_author_mismatch": return "That post wasn't published by the handle you entered."
        case "post_too_old": return "That post is too old. Share a fresh ACU post from the last day."
        case "post_missing_tag": return "Your post needs the #AgentComputerUse tag or the ACU link from the preset."
        case "follow_missing": return "We couldn't see you following @gurusharan yet."
        case "post_missing": return "We couldn't find your ACU post yet. Publish it, then try Check again."
        case "claim_expired": return "This check timed out after a few days. Follow the steps and tap Check again."
        case "rejected": return "We couldn't approve that post. Check the shared text and try again."
        case "device_conflict": return "Your island is active on another Mac. You can move it here with a fresh post."
        case "device_taken": return "This Mac is already unlocked with a different account. Sign out first to switch."
        case "rebind_cooldown": return "Your island was moved recently. You can move it again on the date shown."
        case "device_moved": return "Your island was moved to another Mac. Unlock again here with a fresh post."
        case "revoked": return "Island access was withdrawn. Unlock again to check your account."
        case "bad_signature": return "This Mac couldn't prove its identity to the island service. Restart ACU and try again."
        case "rate_limited": return "Too many tries. Wait a minute and try again."
        case "not_found": return "This Mac has no island access yet. Unlock to get started."
        case "invalid_entitlement": return "Island access couldn't be validated for this Mac. Unlock again to renew it."
        case "keychain": return "We couldn't securely save island access. Unlock your login Keychain and try again."
        case "device_identity": return "This Mac couldn't identify itself. Restart ACU and try again."
        case "offline": return "We couldn't reach the island service. Check your connection and try again."
        case "service_wake_timeout": return "The unlock service is still waking up. Check your connection and try again."
        default: return "The island service couldn't complete your request. Please try again."
        }
    }
    static func rejectedMessage(reason: String?) -> String {
        guard let reason, !reason.isEmpty else { return friendlyMessage(for: "rejected") }
        return friendlyMessage(for: reason)
    }
    static func isTransient(_ error: Error) -> Bool {
        if let failure = error as? IslandGateFailure {
            if failure.code == "offline" { return true }
            if let status = failure.httpStatus, [502, 503, 504].contains(status) { return true }
            return false
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .timedOut, .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet, .dnsLookupFailed, .cannotFindHost:
                return true
            default: return false
            }
        }
        return false
    }
}

enum IslandGateSendMode {
    /// Submit / other user-initiated calls: retry cold start, surface wake UI, error after budget.
    case interactive
    /// Poll loop: retry cold start silently, swallow failures after budget.
    case poll
    /// Background refresh, config prefetch, wall: retry cold start, throw after budget.
    case background
}

final class IslandGateClient {
    let baseURL: URL
    private let session: URLSession
    /// Per-attempt URL timeout (Render free cold start).
    var requestTimeout: TimeInterval = 75
    /// Total retry budget for transient failures.
    var coldStartBudget: TimeInterval = 90
    var retryBackoffBase: TimeInterval = 1

    init(baseURL: URL, session: URLSession = .shared) { self.baseURL = baseURL; self.session = session }

    func config(onWaking: (@MainActor () -> Void)? = nil) async throws -> IslandGateConfig {
        try await request("v1/config", method: "GET", body: Data(), device: nil, action: nil, mode: .background, onWaking: onWaking, as: IslandGateConfig.self)
    }

    func createClaim(device: IslandGateDevice, body: [String: Any], onWaking: (@MainActor () -> Void)? = nil) async throws -> IslandGateClaimResponse {
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await request("v1/claims", method: "POST", body: data, device: device, action: "claim", mode: .interactive, onWaking: onWaking, as: IslandGateClaimResponse.self)
    }

    func pollClaim(id: String, device: IslandGateDevice) async throws -> IslandGateClaimResponse {
        try await request("v1/claims/\(id)", method: "GET", body: Data(), device: device, action: "poll", mode: .poll, onWaking: nil, as: IslandGateClaimResponse.self)
    }

    func refreshEntitlement(device: IslandGateDevice) async throws -> IslandGateEntitlementResponse {
        let empty = Data("{}".utf8)
        return try await request("v1/entitlement/refresh", method: "POST", body: empty, device: device, action: "refresh", mode: .background, onWaking: nil, as: IslandGateEntitlementResponse.self)
    }

    func setWallVisibility(_ show: Bool, device: IslandGateDevice) async throws {
        let payload = try JSONSerialization.data(withJSONObject: ["show": show])
        _ = try await request("v1/wall", method: "POST", body: payload, device: device, action: "wall", mode: .background, onWaking: nil, as: IslandGateWallResponse.self)
    }

    private func request<T: Decodable>(
        _ path: String, method: String, body: Data, device: IslandGateDevice?, action: String?, mode: IslandGateSendMode,
        onWaking: (@MainActor () -> Void)?, as type: T.Type
    ) async throws -> T {
        let raw = try await send(path, method: method, body: body, device: device, action: action, mode: mode, onWaking: onWaking)
        do { return try JSONDecoder().decode(type, from: raw) }
        catch { throw IslandGateFailure(code: "server_error") }
    }

    private func send(
        _ path: String, method: String, body: Data, device: IslandGateDevice?, action: String?, mode: IslandGateSendMode,
        onWaking: (@MainActor () -> Void)?
    ) async throws -> Data {
        let started = Date()
        var backoff = retryBackoffBase
        var attempts = 0
        while true {
            attempts += 1
            do {
                return try await sendOnce(path, method: method, body: body, device: device, action: action)
            } catch {
                let elapsed = Date().timeIntervalSince(started)
                guard IslandGateFailure.isTransient(error), elapsed < coldStartBudget else {
                    if IslandGateFailure.isTransient(error), elapsed >= coldStartBudget {
                        if mode == .poll { throw IslandGateFailure(code: "offline") }
                        throw IslandGateFailure(code: "service_wake_timeout")
                    }
                    throw error
                }
                if mode == .interactive {
                    await MainActor.run { onWaking?() }
                }
                let remaining = coldStartBudget - elapsed
                try await Task.sleep(nanoseconds: UInt64(min(backoff, remaining) * 1_000_000_000))
                backoff = min(backoff * 2, 8)
            }
        }
    }

    private func sendOnce(_ path: String, method: String, body: Data, device: IslandGateDevice?, action: String?) async throws -> Data {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.timeoutInterval = requestTimeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        if method != "GET" {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let device, let action {
            let signed = try device.sign(action: action, body: body)
            req.setValue(signed.deviceID, forHTTPHeaderField: "X-ACU-Device")
            req.setValue(String(signed.timestamp), forHTTPHeaderField: "X-ACU-TS")
            req.setValue(signed.signatureBase64, forHTTPHeaderField: "X-ACU-Sig")
        }
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
