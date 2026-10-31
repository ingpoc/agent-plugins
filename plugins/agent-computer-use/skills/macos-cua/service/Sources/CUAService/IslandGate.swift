import AppKit
import AuthenticationServices
import Combine
import Foundation

// Gate ownership ends at panel visibility. Voice, onboarding, hub and socket APIs run independently.
enum IslandGateState: Equatable {
    case locked(String? = nil)
    case signingIn(String)
    case needsVerify(provider: String, follow: Bool, post: Bool)
    case verifying
    case pendingReview
    case conflict(deviceName: String, boundAt: String?, rebindAvailableAt: String?)
    case unlocked(account: IslandGateAccount, verified: Bool, exp: Date)
    case error(code: String, message: String)
}

@MainActor
final class IslandGate: NSObject, ObservableObject, ASWebAuthenticationPresentationContextProviding {
    static let shared = IslandGate()
    @Published private(set) var state: IslandGateState = .locked()
    @Published private(set) var config: IslandGateConfig?
    @Published private(set) var showOnWall = false
    @Published private(set) var wallUpdating = false
    @Published private(set) var wallError: String?
    @Published var postURL = ""
    @Published var copied = false
    @Published private(set) var verificationMessage: String?
    private(set) var account: IslandGateAccount?
    private var sessionToken: String?
    private var entitlement: String?
    private let store: IslandGateStoring
    private let client: IslandGateClient
    private let verifier: IslandGateVerifier
    private var device: IslandGateDevice?
    private var authentication: ASWebAuthenticationSession?
    private var windowController: IslandGateWindowController?
    private var polling: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var expiryTask: Task<Void, Never>?
    private var operation: Task<Void, Never>?
    private var wallOperation: Task<Void, Never>?
    private var generation = 0
    private let fixture: Bool
    private var windowOpen = false
    var onChange: [() -> Void] = []

    init(store: IslandGateStoring = IslandGateKeychain(), client: IslandGateClient? = nil,
         publicKey: Data? = nil, device: IslandGateDevice? = nil, fixtureState: IslandGateState? = nil, fixtureAccount: IslandGateAccount? = nil) {
        let environment = IslandGateEnvironment()
        self.store = store; self.client = client ?? IslandGateClient(baseURL: environment.baseURL)
        verifier = IslandGateVerifier(publicKey: publicKey ?? environment.publicKey)
        self.device = device; fixture = fixtureState != nil
        super.init()
        if let fixtureState { state = fixtureState; account = fixtureAccount }
    }
    deinit {
        polling?.cancel(); refreshTask?.cancel(); expiryTask?.cancel(); operation?.cancel(); wallOperation?.cancel()
    }
    var isUnlocked: Bool {
        if case .unlocked(_, _, let exp) = state { return Date() < exp }
        return false
    }
    var provider: String {
        if case .needsVerify(let provider, _, _) = state { return provider }
        return account?.provider ?? "x"
    }
    var accessSummary: String {
        if case .unlocked(let account, _, _) = state { return "\(account.handle) · \(account.provider == "x" ? "X" : "LinkedIn")" }
        return "Unlock Dynamic Island…"
    }
    func transition(_ next: IslandGateState) {
        state = next
        expiryTask?.cancel(); expiryTask = nil
        if case .unlocked(_, _, let exp) = next {
            expiryTask = Task { [weak self] in
                let delay = max(0, exp.timeIntervalSinceNow)
                // Bound sleep conversion even for malformed but correctly signed timestamps.
                try? await Task.sleep(nanoseconds: UInt64(min(delay + 0.01, 60 * 60 * 24 * 365) * 1_000_000_000))
                guard !Task.isCancelled, let self else { return }
                if !self.isUnlocked { self.transition(.locked("Island access has expired. Connect and sign in to renew it.")) }
            }
        }
        onChange.forEach { $0() }
        updatePolling()
    }
    func start() {
        guard !fixture else { return }
        do {
            if device == nil { device = try IslandGateDevice.load(store: store) }
            sessionToken = try store.read("session").flatMap { String(data: $0, encoding: .utf8) }
            if let data = try store.read("account") {
                account = try JSONDecoder().decode(IslandGateAccount.self, from: data)
                showOnWall = account?.show_on_wall ?? false
            }
            entitlement = try store.read("entitlement").flatMap { String(data: $0, encoding: .utf8) }
            if let entitlement { try accept(entitlement) }
        } catch { handle(error) }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 86_400_000_000_000)
            }
        }
    }
    func refresh() async {
        guard !fixture, let device, entitlement != nil else { return }
        let version = generation
        do {
            let reply = try await client.request("v1/entitlement/refresh", body: device.signedBody(action: "refresh"), as: IslandGateReply.self)
            guard version == generation else { return }
            if let token = reply.entitlement { try accept(token) }
        } catch {
            guard version == generation else { return }
            let failure = error as? IslandGateFailure
            if failure?.code == "offline" || (failure?.httpStatus ?? 0) >= 500 {
                if let entitlement, (try? verifier.verify(entitlement, deviceID: device.id)) != nil { return }
                if case .unlocked = state { transition(.locked("Connect to renew your island access.")) }
                return
            }
            if failure?.code == "not_found", entitlement == nil { return }
            handle(error)
        }
    }
    private func accept(_ token: String) throws {
        guard let device else { throw IslandGateFailure(code: "device_identity") }
        let claim = try verifier.verify(token, deviceID: device.id)
        var identity = account ?? IslandGateAccount(id: claim.acct, provider: claim.prov, handle: claim.handle)
        identity.show_on_wall = showOnWall
        guard identity.id == claim.acct else { throw IslandGateFailure(code: "invalid_entitlement") }
        try store.write(try JSONEncoder().encode(identity), name: "account")
        try store.write(Data(token.utf8), name: "entitlement")
        entitlement = token; account = identity
        transition(.unlocked(account: identity, verified: identity.verified ?? false, exp: Date(timeIntervalSince1970: claim.exp)))
    }
    func show() {
        if windowController == nil { windowController = IslandGateWindowController(gate: self) }
        opened()
        windowController?.showWindow(nil)
        windowController?.window?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        updatePolling()
    }
    func opened() {
        windowOpen = true
        updatePolling()
        if !fixture, sessionToken != nil, entitlement == nil, case .locked = state {
            perform { gate in
                gate.config = try await gate.client.config()
                return try await gate.client.request("v1/status", bearer: gate.sessionToken, as: IslandGateReply.self)
            }
        }
    }
    func closed() {
        windowOpen = false; polling?.cancel(); polling = nil
        if case .signingIn = state { cancelSignIn() }
    }
    func cancelSignIn() {
        generation += 1; authentication?.cancel(); authentication = nil
        operation?.cancel(); operation = nil
        transition(.locked())
    }
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor { windowController!.window! }
    func signIn(_ provider: String) {
        guard !fixture else { return }
        if windowController == nil { show() }
        operation?.cancel(); generation += 1
        let version = generation
        do {
            if device == nil { device = try IslandGateDevice.load(store: store) }
            guard let device else { return }
            let pkce = try IslandGatePKCE()
            transition(.signingIn(provider))
            let url = client.startURL(provider: provider, deviceID: device.id, deviceName: Host.current().localizedName ?? "Mac", challenge: pkce.challenge)
            let auth = ASWebAuthenticationSession(url: url, callbackURLScheme: "acu-island") { [weak self] callback, error in
                Task { @MainActor in
                    guard let self, self.generation == version else { return }
                    self.authentication = nil
                    if let error = error as? ASWebAuthenticationSessionError, error.code == .canceledLogin { self.cancelSignIn(); return }
                    guard error == nil, let callback, callback.scheme == "acu-island", callback.host == "auth" else { self.handle(IslandGateFailure(code: "invalid_callback")); return }
                    let params: [URLQueryItem]
                    if let fragment = callback.fragment, !fragment.isEmpty {
                        params = URLComponents(string: "?\(fragment)")?.queryItems ?? []
                    } else {
                        params = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
                    }
                    if let code = params.first(where: { $0.name == "error" })?.value { self.handle(IslandGateFailure(code: code)); return }
                    guard let code = params.first(where: { $0.name == "code" })?.value, !code.isEmpty else { self.handle(IslandGateFailure(code: "invalid_callback")); return }
                    self.operation = Task { await self.redeem(code: code, pkce: pkce, version: version) }
                }
            }
            authentication = auth
            auth.presentationContextProvider = self
            auth.prefersEphemeralWebBrowserSession = false
            if !auth.start() { authentication = nil; handle(IslandGateFailure(code: "invalid_callback")) }
        } catch { handle(error) }
    }
    private func redeem(code: String, pkce: IslandGatePKCE, version: Int) async {
        guard let device else { return }
        do {
            let reply = try await client.request("v1/session/redeem", body: ["code": code, "app_verifier": pkce.verifier, "device_id": device.id, "device_pubkey": device.publicKey], as: IslandGateReply.self)
            guard version == generation else { return }
            if let token = reply.session {
                try store.write(Data(token.utf8), name: "session")
                wallOperation?.cancel(); wallUpdating = false; wallError = nil
                sessionToken = token
            }
            if let identity = reply.account {
                try store.write(try JSONEncoder().encode(identity), name: "account")
                account = identity; showOnWall = reply.show_on_wall ?? identity.show_on_wall ?? false
            }
            // Sharing configuration is needed only for the verification checklist.
            if reply.status == "needs_verify", reply.entitlement == nil {
                config = try await client.config()
                guard version == generation else { return }
            }
            try apply(reply)
        } catch { if version == generation { handle(error) } }
    }
    func apply(_ reply: IslandGateReply) throws {
        if let identity = reply.account { account = identity }
        if let consent = reply.show_on_wall ?? reply.account?.show_on_wall { showOnWall = consent }
        if var identity = account {
            if let verified = reply.verified { identity.verified = verified }
            identity.show_on_wall = showOnWall
            try store.write(try JSONEncoder().encode(identity), name: "account")
            account = identity
        }
        if let token = reply.entitlement { try accept(token) }
        else if reply.status == "pending_review" { transition(.pendingReview) }
        else if reply.status == "needs_verify" { transition(.needsVerify(provider: provider, follow: false, post: false)) }
        else { throw IslandGateFailure(code: "server_error") }
    }
    func verify() {
        perform { gate in
            if gate.config == nil { gate.config = try await gate.client.config() }
            guard let token = gate.sessionToken else { throw IslandGateFailure(code: "not_signed_in") }
            var body: [String: Any] = ["show_on_wall": gate.showOnWall]
            if gate.provider == "linkedin" { body["post_url"] = gate.postURL.trimmingCharacters(in: .whitespacesAndNewlines) }
            return try await gate.client.request("v1/verify/\(gate.provider)", body: body, bearer: token, as: IslandGateReply.self)
        }
    }
    func rebind() {
        // A restored refresh credential is insufficient: only a fresh sign-in obtains a session on this Mac.
        if sessionToken == nil { signIn(provider); return }
        perform { gate in
            guard let device = gate.device else { throw IslandGateFailure(code: "device_identity") }
            return try await gate.client.request("v1/device/rebind", body: device.signedBody(action: "rebind"), bearer: gate.sessionToken, as: IslandGateReply.self)
        }
    }
    private func perform(_ work: @escaping (IslandGate) async throws -> IslandGateReply) {
        guard !fixture else { return }
        operation?.cancel(); generation += 1
        let version = generation
        transition(.verifying)
        operation = Task { [weak self] in
            guard let self else { return }
            do {
                let reply = try await work(self)
                guard version == self.generation else { return }
                try self.apply(reply)
            } catch { if version == self.generation { self.handle(error) } }
        }
    }
    func poll() async {
        guard !fixture, windowOpen, case .pendingReview = state, let sessionToken else { return }
        let version = generation
        do {
            let reply = try await client.request("v1/status", bearer: sessionToken, as: IslandGateReply.self)
            guard version == generation, windowOpen else { return }
            if reply.status != "pending_review" { try apply(reply) }
        } catch {
            guard version == generation else { return }
            let f = error as? IslandGateFailure
            if f?.code != "offline" && (f?.httpStatus ?? 0) < 500 { handle(error) }
        }
    }
    private func updatePolling() {
        if case .pendingReview = state, windowOpen, !fixture {
            guard polling == nil else { return }
            polling = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 60_000_000_000)
                    guard !Task.isCancelled else { break }
                    await self?.poll()
                }
            }
        } else { polling?.cancel(); polling = nil }
    }
    func handle(_ error: Error) {
        let failure = (error as? IslandGateFailure) ?? IslandGateFailure(code: "server_error")
        switch failure.code {
        case "follow_missing", "post_missing": transition(.needsVerify(provider: "x", follow: failure.follow ?? false, post: failure.post ?? false))
        case "post_url_invalid", "post_url_used", "rejected":
            verificationMessage = failure.message
            transition(.needsVerify(provider: "linkedin", follow: false, post: false))
        case "not_signed_in", "session_expired":
            sessionToken = nil
            try? store.delete("session")
            transition(.locked(failure.message))
        case "pending_review": transition(.pendingReview)
        case "device_conflict": transition(.conflict(deviceName: failure.device_name ?? "another Mac", boundAt: failure.bound_at, rebindAvailableAt: failure.rebind_available_at))
        case "rebind_cooldown": transition(.conflict(deviceName: "your other Mac", boundAt: nil, rebindAvailableAt: failure.rebind_available_at))
        case "device_moved", "revoked", "not_found":
            entitlement = nil
            do { try store.delete("entitlement") } catch { transition(.error(code: "keychain", message: IslandGateFailure(code: "keychain").message)); return }
            transition(.locked(failure.message))
        default: transition(.error(code: failure.code, message: failure.message))
        }
    }
    func retry() {
        if sessionToken != nil { verify() } else { signIn(provider) }
    }
    func signOut() {
        generation += 1; operation?.cancel(); authentication?.cancel(); authentication = nil
        // Lock immediately, even if Keychain removal fails.
        wallOperation?.cancel(); wallOperation = nil
        showOnWall = false; wallUpdating = false; wallError = nil
        entitlement = nil; sessionToken = nil; account = nil; postURL = ""; copied = false; verificationMessage = nil
        transition(.locked())
        do { for name in ["entitlement", "session", "account"] { try store.delete(name) } }
        catch { handle(error) }
    }
    var wallConsentTitle: String {
        if provider == "linkedin" { return "Show my name on the Agent Plugins site" }
        let handle = (account?.handle ?? "your handle").trimmingCharacters(in: CharacterSet(charactersIn: "@"))
        return "Show @\(handle) on the Agent Plugins site"
    }
    func setVerificationWallConsent(_ show: Bool) {
        showOnWall = show; wallError = nil
        onChange.forEach { $0() }
    }
    func setWallVisibility(_ show: Bool) {
        guard !wallUpdating, show != showOnWall else { return }
        let previous = showOnWall
        showOnWall = show; wallUpdating = true; wallError = nil
        onChange.forEach { $0() }
        guard !fixture else { wallUpdating = false; return }
        let session = sessionToken
        wallOperation = Task { [weak self] in
            guard let self else { return }
            do {
                guard let sessionToken = self.sessionToken else { throw IslandGateFailure(code: "not_signed_in") }
                try await self.client.setWallVisibility(show, bearer: sessionToken)
                guard self.sessionToken == session else { return }
                self.account?.show_on_wall = show
                if let account = self.account {
                    do { try self.store.write(try JSONEncoder().encode(account), name: "account") }
                    catch { self.wallError = "Your site preference was updated, but couldn't be saved on this Mac. Try again after unlocking your login Keychain." }
                }
            } catch {
                guard self.sessionToken == session else { return }
                self.showOnWall = previous
                let failure = (error as? IslandGateFailure) ?? IslandGateFailure(code: "server_error")
                self.wallError = "Couldn't update your site visibility. \(failure.message) Your previous setting is shown. Try again."
            }
            self.wallUpdating = false
            self.onChange.forEach { $0() }
        }
    }
    func openFollow() {
        let url = provider == "linkedin" ? config?.linkedin.follow_url : URL(string: "https://x.com/intent/follow?screen_name=gurusharan")
        if let url, url.scheme == "https" { NSWorkspace.shared.open(url) }
    }
    func openShare() {
        guard let config else { handle(IslandGateFailure(code: "server_error")); return }
        let url: URL
        if provider == "linkedin" {
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(config.linkedin.preset_text, forType: .string)
            copied = true; url = config.linkedin.share_url
        } else { url = config.x.intent_url }
        if url.scheme == "https" { NSWorkspace.shared.open(url) }
    }
}
