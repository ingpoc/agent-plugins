import AppKit
import Combine
import Foundation
import os

enum IslandGateCheckStatus: String, Equatable {
    case pending, confirmed, failed
    case notApplicable = "n/a"
    init(raw: String) {
        self = IslandGateCheckStatus(rawValue: raw) ?? .pending
    }
}

enum IslandGateState: Equatable {
    case locked(linkedIn: Bool = false, message: String? = nil)
    case submitting
    case waking
    case checking(claimID: String, since: Date, follow: IslandGateCheckStatus, post: IslandGateCheckStatus)
    case unlocked(account: IslandGateAccount, verified: Bool, exp: Date)
    case pendingFollow(account: IslandGateAccount, since: Date)
    case trial(account: IslandGateAccount, ends: Date)
    case trialExpired(account: IslandGateAccount)
    case repostChecking(account: IslandGateAccount, trialEnds: Date?)
    case rejected(message: String)
    case conflict(deviceName: String?, rebindAvailableAt: String?)
    case error(code: String, message: String)

    var name: String {
        switch self {
        case .locked: return "locked"
        case .submitting: return "submitting"
        case .waking: return "waking"
        case .checking: return "checking"
        case .unlocked: return "unlocked"
        case .pendingFollow: return "pending_follow"
        case .trial: return "trial"
        case .trialExpired: return "trial_expired"
        case .repostChecking: return "repost_checking"
        case .rejected: return "rejected"
        case .conflict: return "conflict"
        case .error: return "error"
        }
    }
}

@MainActor
final class IslandGate: ObservableObject {
    private static let log = Logger(subsystem: "com.ingpoc.cua-service", category: "gate")
    static let shared = IslandGate()
    @Published private(set) var state: IslandGateState = .locked()
    @Published private(set) var config: IslandGateConfig? = .bundled
    @Published private(set) var showOnWall = false
    @Published private(set) var wallUpdating = false
    @Published private(set) var wallError: String?
    @Published var handleText = ""
    @Published private(set) var followDone: Bool
    @Published private(set) var postDone: Bool
    @Published var focusInput = false
    private let defaults: UserDefaults?
    private var pendingSteps: Set<Int> = []
    private var activationObserver: AnyCancellable?
    private let wakeSleep: () async throws -> Void
    @Published private(set) var deepLinkBanner: String?
    private(set) var account: IslandGateAccount?
    private var entitlement: String?
    private var verifiedEntitlementExpiry: Date?
    private var entitlementTier: String?
    private var claimID: String?
    private var pollInterval: TimeInterval = 60
    #if DEBUG
    var debugPollInterval: TimeInterval?
    #endif
    private let store: IslandGateStoring
    private let client: IslandGateClient
    private let verifier: IslandGateVerifier
    private var device: IslandGateDevice?
    private var windowController: IslandGateWindowController?
    private var polling: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var expiryTask: Task<Void, Never>?
    private var operation: Task<Void, Never>?
    private var configTask: Task<Void, Never>?
    private var fetchedConfig = false
    let urlOpener: IslandGateURLOpening
    let pasteboard: IslandGatePasteboard
    private var wallOperation: Task<Void, Never>?
    private var generation = 0
    private let fixture: Bool
    /// Fixture renders (gate-snapshot) draw a still frame: offscreen cacheDisplay skips in-flight Core Animation layers.
    var isFixture: Bool { fixture }
    private var windowOpen = false
    private var unlockLinkedInRoute = false
    var onChange: [() -> Void] = []

    init(store: IslandGateStoring = IslandGateKeychain(), client: IslandGateClient? = nil,
         publicKey: Data? = nil, device: IslandGateDevice? = nil, fixtureState: IslandGateState? = nil, fixtureAccount: IslandGateAccount? = nil, urlOpener: IslandGateURLOpening? = nil, pasteboard: IslandGatePasteboard? = nil, defaults: UserDefaults? = nil, wakeSleep: @escaping () async throws -> Void = { try await Task.sleep(nanoseconds: 1_500_000_000) }) {
        self.wakeSleep = wakeSleep
        let settings = defaults ?? ((IslandGateSafety.restricted || fixtureState != nil) ? nil : UserDefaults(suiteName: "com.ingpoc.cua-service"))
        self.defaults = settings
        followDone = settings?.bool(forKey: "islandGate.followDone") ?? false
        postDone = settings?.bool(forKey: "islandGate.postDone") ?? false
        self.urlOpener = urlOpener ?? IslandGateURLOpenerDefault.make(fixtureState: fixtureState)
        self.pasteboard = pasteboard ?? ((IslandGateSafety.restricted || fixtureState != nil) ? IslandGateRecordingPasteboard() : IslandGateSystemPasteboard())
        self.store = store; self.client = client ?? IslandGateClient(baseURL: IslandGateEnvironment().baseURL)
        verifier = IslandGateVerifier(publicKey: publicKey ?? IslandGateEnvironment().publicKey)
        self.device = device; fixture = fixtureState != nil
        if let fixtureState { state = fixtureState; account = fixtureAccount }
        if !IslandGateSafety.restricted {
            activationObserver = NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification).sink { [weak self] _ in
                Task { @MainActor in self?.didBecomeActive() }
            }
        }
    }
    deinit {
        configTask?.cancel(); polling?.cancel(); refreshTask?.cancel(); expiryTask?.cancel(); operation?.cancel(); wallOperation?.cancel()
    }
    var isUnlocked: Bool {
        if entitlement != nil, let verifiedEntitlementExpiry { return Date() < verifiedEntitlementExpiry }
        if case .unlocked(_, _, let exp) = state { return Date() < exp }
        if case .trial(_, let ends) = state { return Date() < ends }
        if case .repostChecking(_, let ends?) = state { return Date() < ends }
        return false
    }
    var stateName: String { state.name }
    func isLaunchConfigured(for provider: String) -> Bool {
        guard let config else { return false }
        return provider == "linkedin" ? config.linkedin.launch_post_url != nil : config.x.launch_post_id != nil
    }
    var lockedProvider: String {
        if case .locked(let linkedIn, _) = state { return linkedIn ? "linkedin" : "x" }
        if case .submitting = state { return unlockLinkedInRoute ? "linkedin" : "x" }
        if case .waking = state { return unlockLinkedInRoute ? "linkedin" : "x" }
        if case .checking = state { return account?.provider ?? (unlockLinkedInRoute ? "linkedin" : "x") }
        if case .pendingFollow(let account, _) = state { return account.provider }
        if case .trial(let account, _) = state { return account.provider }
        if case .trialExpired(let account) = state { return account.provider }
        if case .repostChecking(let account, _) = state { return account.provider }
        return account?.provider ?? "x"
    }
    var accessSummary: String {
        if case .unlocked(let account, _, _) = state { return "\(account.handle) · \(account.provider == "x" ? "X" : "LinkedIn")" }
        if case .trial(let account, _) = state { return "\(account.handle) · trial" }
        if case .trialExpired(let account) = state { return "\(account.handle) · trial ended" }
        if case .repostChecking(let account, _) = state { return "\(account.handle) · repost checking" }
        return "Unlock Dynamic Island…"
    }
    var checkETAText: String {
        let trimmed = config?.check_eta_text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? "usually within the hour" : trimmed
    }
    func openFromDeepLink() {
        deepLinkBanner = isUnlocked ? "Your island is already unlocked." : nil
        show()
    }
    func transition(_ next: IslandGateState) {
        Self.log.info("Gate state \(self.state.name, privacy: .public) -> \(next.name, privacy: .public)")
        if case .trialExpired(let identity) = next {
            handleText = identity.handle
            unlockLinkedInRoute = identity.provider == "linkedin"
        }
        state = next
        expiryTask?.cancel(); expiryTask = nil
        if case .unlocked(_, _, let exp) = next {
            expiryTask = Task { [weak self] in
                let delay = max(0, exp.timeIntervalSinceNow)
                try? await Task.sleep(nanoseconds: UInt64(min(delay + 0.01, 60 * 60 * 24 * 365) * 1_000_000_000))
                guard !Task.isCancelled, let self else { return }
                if !self.isUnlocked { self.transition(.locked(message: "Island access has expired. Connect and unlock again to renew it.")) }
            }
        }
        if case .trial(let identity, let ends) = next, ends > Date() {
            expiryTask = Task { [weak self] in
                let delay = max(0, ends.timeIntervalSinceNow)
                try? await Task.sleep(nanoseconds: UInt64(min(delay + 0.01, 60 * 60 * 24 * 365) * 1_000_000_000))
                guard !Task.isCancelled, let self, case .trial = self.state else { return }
                self.trialReachedEnd(identity)
            }
        }
        if case .repostChecking(let identity, let ends?) = next, ends > Date() {
            expiryTask = Task { [weak self] in
                let delay = max(0, ends.timeIntervalSinceNow)
                try? await Task.sleep(nanoseconds: UInt64(min(delay + 0.01, 60 * 60 * 24 * 365) * 1_000_000_000))
                guard !Task.isCancelled, let self, case .repostChecking(_, let currentEnds?) = self.state, currentEnds == ends else { return }
                self.trialReachedEnd(identity)
            }
        }
        if expiryTask == nil, entitlement != nil, let expiry = verifiedEntitlementExpiry, expiry > Date() {
            expiryTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(min(max(0, expiry.timeIntervalSinceNow + 0.01), 60 * 60 * 24 * 365) * 1_000_000_000))
                guard !Task.isCancelled, let self, !self.isUnlocked else { return }
                if self.entitlementTier == "trial", let account = self.account { self.trialReachedEnd(account) }
                else { self.transition(.locked(message: "Island access has expired. Connect to renew it.")) }
            }
        }
        onChange.forEach { $0() }
        updatePolling()
    }
    func start() {
        guard !fixture else { return }
        do {
            if device == nil { device = try IslandGateDevice.load(store: store) }
            claimID = try store.read("claim-id").flatMap { String(data: $0, encoding: .utf8) }
            if let data = try store.read("account") {
                account = try JSONDecoder().decode(IslandGateAccount.self, from: data)
                showOnWall = account?.show_on_wall ?? false
            }
            entitlement = try store.read("entitlement").flatMap { String(data: $0, encoding: .utf8) }
            entitlementTier = try store.read("entitlement-tier").flatMap { String(data: $0, encoding: .utf8) }
            if let entitlement {
                do { try accept(entitlement) }
                catch {
                    if entitlementTier == "trial", let account,
                       let raw = try store.read("trial-ends-at").flatMap({ String(data: $0, encoding: .utf8) }),
                       let ends = IslandGateTrialNoticeSchedule.parseDate(raw), ends <= Date() {
                        transition(isLaunchConfigured(for: account.provider) ? .trialExpired(account: account) : .trial(account: account, ends: ends))
                    } else { throw error }
                }
            }
            else if entitlementTier == "trial", let account,
                    let raw = try store.read("trial-ends-at").flatMap({ String(data: $0, encoding: .utf8) }),
                    let ends = IslandGateTrialNoticeSchedule.parseDate(raw), ends <= Date() {
                transition(.trial(account: account, ends: ends))
            }
            else if let claimID, let data = try store.read("pending-follow"), let pending = try? JSONDecoder().decode(IslandGateAccount.self, from: data) {
                transition(.pendingFollow(account: pending, since: Date()))
            } else if claimID != nil { transition(.checking(claimID: claimID!, since: Date(), follow: .pending, post: .pending)) }
        } catch { handle(error) }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 86_400_000_000_000)
            }
        }
    }
    func refresh() async {
        guard !fixture, let device, entitlement != nil || (entitlementTier == "trial" && account != nil) else { return }
        let version = generation
        do {
            let reply = try await client.refreshEntitlement(device: device)
            guard version == generation else { return }
            if let token = reply.entitlement { try accept(token) }
        } catch {
            guard version == generation else { return }
            let failure = error as? IslandGateFailure
            let bindingRemoved = (failure?.code == "revoked" && failure?.httpStatus == 403)
                || (failure?.code == "device_moved" && failure?.httpStatus == 409)
                || (failure?.code == "not_found" && failure?.httpStatus == 404)
            if !bindingRemoved, entitlement != nil, let verifiedEntitlementExpiry, Date() < verifiedEntitlementExpiry { return }
            if failure?.code == "offline" || failure?.code == "service_wake_timeout" || (failure?.httpStatus ?? 0) >= 500 {
                if let entitlement, (try? verifier.verify(entitlement, deviceID: device.id)) != nil { return }
                if case .unlocked = state { transition(.locked(message: "Connect to renew your island access.")) }
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
        entitlement = token
        verifiedEntitlementExpiry = Date(timeIntervalSince1970: claim.exp)
        try store.write(try JSONEncoder().encode(identity), name: "account")
        try store.write(Data(token.utf8), name: "entitlement")
        entitlement = token; account = identity
        try store.delete("pending-follow")
        entitlementTier = claim.tier
        let expiry = Date(timeIntervalSince1970: claim.exp)
        if entitlementTier == "trial" {
            try store.write(Data("trial".utf8), name: "entitlement-tier")
            try store.write(Data(ISO8601DateFormatter().string(from: expiry).utf8), name: "trial-ends-at")
            transition(.trial(account: identity, ends: expiry))
            if isLaunchConfigured(for: identity.provider) { IslandGateTrialNoticeSchedule.schedule(accountID: identity.id, trialEnds: expiry, fixture: fixture) }
            else { IslandGateTrialNoticeSchedule.cancel(accountID: identity.id, fixture: fixture) }
        } else {
            try store.delete("entitlement-tier"); try store.delete("trial-ends-at")
            IslandGateTrialNoticeSchedule.cancel(accountID: identity.id, fixture: fixture)
            clearClaimID(); transition(.unlocked(account: identity, verified: identity.verified ?? false, exp: expiry))
        }
    }
    private func clearClaimID() {
        claimID = nil
        try? store.delete("claim-id")
        try? store.delete("pending-follow")
    }
    private func persistClaimID(_ id: String) throws {
        claimID = id
        try store.write(Data(id.utf8), name: "claim-id")
    }
    func show() {
        if windowController == nil { windowController = IslandGateWindowController(gate: self) }
        opened()
        windowController?.showWindow(nil)
        windowController?.window?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func opened() {
        windowOpen = true
        updatePolling()
        if !fixture, !fetchedConfig, configTask == nil {
            configTask = Task { [weak self] in
                guard let self else { return }
                do {
                    let config = try await self.client.config()
                    guard !Task.isCancelled else { return }
                    self.config = config
                    self.fetchedConfig = true
                    if let seconds = self.config?.poll_seconds { self.pollInterval = min(300, max(1, TimeInterval(seconds))) }
                    self.reconcileTrialForLaunchConfig()
                    self.onChange.forEach { $0() }
                } catch { /* A later window opening can retry the best-effort prefetch. */ }
                self.configTask = nil
            }
        }
    }
    func closed() {
        windowOpen = false
        fetchedConfig = false
        deepLinkBanner = nil
        updatePolling()
    }
    private func trialReachedEnd(_ identity: IslandGateAccount) {
        if isLaunchConfigured(for: identity.provider) {
            transition(.trialExpired(account: identity))
        } else {
            // Keep the identity and claim polling active until a fresh signed token arrives.
            transition(.trial(account: identity, ends: Date()))
            Task { await refresh() }
        }
    }
    private func reconcileTrialForLaunchConfig() {
        guard let account else { return }
        switch state {
        case .trial(_, let ends):
            guard isLaunchConfigured(for: account.provider) else {
                IslandGateTrialNoticeSchedule.cancel(accountID: account.id, fixture: fixture)
                return
            }
            if ends <= Date() { transition(.trialExpired(account: account)) }
            else { IslandGateTrialNoticeSchedule.schedule(accountID: account.id, trialEnds: ends, fixture: fixture) }
        case .repostChecking(_, let ends?):
            if isLaunchConfigured(for: account.provider), ends <= Date() { transition(.trialExpired(account: account)) }
        default: break
        }
    }
    private func trialExpiredByServer() {
        guard let account else { transition(.locked(message: "Your trial ended.")); return }
        if isLaunchConfigured(for: account.provider) {
            entitlement = nil
            do { try store.delete("entitlement") }
            catch { transition(.error(code: "keychain", message: IslandGateFailure(code: "keychain").message)); return }
            transition(.trialExpired(account: account))
        } else if !fixture, configTask == nil {
            // The server may have launched since our cached prelaunch config. Refresh
            // config once; further recovery uses the normal poll/refresh intervals.
            let version = generation
            configTask = Task { [weak self] in
                guard let self else { return }
                defer { self.configTask = nil }
                do {
                    let updated = try await self.client.config()
                    guard version == self.generation else { return }
                    self.config = updated
                    if self.isLaunchConfigured(for: account.provider) { self.trialExpiredByServer() }
                } catch { }
            }
        }
    }
    func useLinkedInRoute() {
        guard case .locked = state else { return }
        transition(.locked(linkedIn: true))
    }
    func useXRoute() {
        guard case .locked(let linkedIn, _) = state, linkedIn else { return }
        transition(.locked(linkedIn: false))
    }
    var isHandleValid: Bool {
        lockedProvider == "linkedin"
            ? IslandGateInputParser.parseLinkedIn(handleText) != nil
            : IslandGateInputParser.parseX(handleText) != nil
    }
    var activeStep: Int { !followDone ? 1 : !postDone ? 2 : 3 }
    func didBecomeActive() {
        if pendingSteps.contains(1) { followDone = true; defaults?.set(true, forKey: "islandGate.followDone") }
        if pendingSteps.contains(2) { postDone = true; defaults?.set(true, forKey: "islandGate.postDone") }
        pendingSteps.removeAll()
    }
    func startOver() {
        operation?.cancel(); generation += 1
        clearClaimID()
        followDone = false; postDone = false; pendingSteps.removeAll()
        defaults?.removeObject(forKey: "islandGate.followDone"); defaults?.removeObject(forKey: "islandGate.postDone")
        handleText = ""; transition(.locked(linkedIn: unlockLinkedInRoute))
    }
    func changeInput() { transition(.locked(linkedIn: unlockLinkedInRoute)); focusInput = true }
    func setFixtureSteps(follow: Bool, post: Bool) { followDone = follow; postDone = post }
    func submitCheck() {
        guard !fixture, isHandleValid else { return }
        operation?.cancel(); generation += 1
        let version = generation
        if case .locked(let linkedIn, _) = state { unlockLinkedInRoute = linkedIn }
        transition(.submitting)
        operation = Task { [weak self] in
            guard let self else { return }
            let wakeTask = Task { @MainActor [weak self] in
                guard let self else { return }
                do { try await self.wakeSleep() } catch { return }
                guard !Task.isCancelled, version == self.generation, self.state == .submitting else { return }
                self.transition(.waking)
            }
            defer { wakeTask.cancel() }
            do {
                if self.device == nil { self.device = try IslandGateDevice.load(store: self.store) }
                guard let device = self.device else { return }
                guard version == self.generation else { return }
                if let seconds = self.config?.poll_seconds { self.pollInterval = min(300, max(1, TimeInterval(seconds))) }
                let provider = self.lockedProvider
                var body: [String: Any] = [
                    "provider": provider, "device_pubkey": device.publicKey,
                    "device_name": Host.current().localizedName ?? "Mac", "show_on_wall": self.showOnWall
                ]
                if provider == "linkedin" {
                    guard let link = IslandGateInputParser.parseLinkedIn(self.handleText), let handle = link.path.split(separator: "/").dropFirst().first else {
                        throw IslandGateFailure(code: "invalid_handle")
                    }
                    body["handle"] = String(handle).lowercased()
                } else {
                    guard let parsed = IslandGateInputParser.parseX(self.handleText) else {
                        throw IslandGateFailure(code: "invalid_handle")
                    }
                    body["handle"] = parsed.handle
                }
                let reply = try await self.client.createClaim(device: device, body: body, onWaking: { wakeTask.cancel() })
                guard version == self.generation else { return }
                try self.applyClaim(reply, since: Date())
            } catch {
                if version == self.generation { self.handleSubmitError(error) }
            }
        }
    }
    private func handleSubmitError(_ error: Error) {
        let failure = (error as? IslandGateFailure) ?? IslandGateFailure(code: "server_error")
        if failure.code == "service_wake_timeout" || (failure.code == "offline" && (state == .submitting || state == .waking)) {
            transition(.error(code: "service_wake_timeout", message: IslandGateFailure.friendlyMessage(for: "service_wake_timeout")))
            return
        }
        handle(error)
    }
    func applyClaim(_ reply: IslandGateClaimResponse, since: Date) throws {
        if let after = reply.poll_after { pollInterval = min(300, max(1, TimeInterval(after))) }
        if let id = reply.claim_id { try persistClaimID(id) }
        if let token = reply.entitlement {
            try accept(token)
            return
        }
        if reply.status == "trial_expired" || reply.status == "expired" {
            if let account {
                if reply.status == "trial_expired" { trialExpiredByServer() }
                else { transition(.trialExpired(account: account)) }
            }
            else { handle(IslandGateFailure(code: reply.reason ?? "trial_expired", rebind_available_at: reply.rebind_available_at)) }
            return
        }
        if reply.status == "rejected" {
            handle(IslandGateFailure(code: reply.reason ?? (reply.status == "expired" ? "claim_expired" : "rejected"), rebind_available_at: reply.rebind_available_at))
            return
        }
        if let id = reply.claim_id {
            try persistClaimID(id)
            let provider = lockedProvider
            let followPending = reply.checks?.follow == "pending"
            if provider == "x", followPending, ["pending_follow", "starting_trial", "pending_verification"].contains(reply.status ?? "") {
                let handle = provider == "linkedin"
                    ? IslandGateInputParser.parseLinkedIn(handleText)?.path.split(separator: "/").dropFirst().first.map(String.init) ?? handleText
                    : IslandGateInputParser.parseX(handleText)?.handle ?? handleText
                let pending = IslandGateAccount(id: "pending", provider: provider, handle: handle.lowercased())
                try store.write(try JSONEncoder().encode(pending), name: "pending-follow")
                if let after = reply.poll_after { pollInterval = min(300, max(1, TimeInterval(after))) }
                transition(.pendingFollow(account: pending, since: since))
                return
            }
            let follow = IslandGateCheckStatus(raw: reply.checks?.follow ?? "pending")
            let post = IslandGateCheckStatus(raw: reply.checks?.post ?? "pending")
            transition(.checking(claimID: id, since: since, follow: follow, post: post))
            return
        }
        throw IslandGateFailure(code: "server_error")
    }
    func pollOnce() async {
        guard !fixture, let device, let claimID else { return }
        let version = generation
        do {
            let reply = try await client.pollClaim(id: claimID, device: device)
            guard version == generation else { return }
            if let after = reply.poll_after { pollInterval = min(300, max(1, TimeInterval(after))) }
            if let token = reply.entitlement {
                try accept(token)
                if reply.status == "trial", let account, case .trial(_, let ends) = state {
                    try persistClaimID(claimID)
                    if isLaunchConfigured(for: account.provider) { IslandGateTrialNoticeSchedule.schedule(accountID: account.id, trialEnds: ends, fixture: fixture) }
                }
                return
            }
            switch reply.status {
            case "trial":
                // trial_ends_at is informational; only a verified signed entitlement extends access.
                break
            case "trial_expired":
                trialExpiredByServer()
            case "unlocked":
                if let token = reply.entitlement { try accept(token) }
                else { throw IslandGateFailure(code: "server_error") }
            case "rejected", "expired":
                clearClaimID()
                let reason = reply.reason ?? (reply.status == "expired" ? "claim_expired" : "rejected")
                handle(IslandGateFailure(code: reason, rebind_available_at: reply.rebind_available_at))
            case "pending_follow", "starting_trial", "pending_verification", .none:
                if case .pendingFollow = state { break }
                if case .checking(let id, let since, _, _) = state {
                    let follow = IslandGateCheckStatus(raw: reply.checks?.follow ?? "pending")
                    let post = IslandGateCheckStatus(raw: reply.checks?.post ?? "pending")
                    transition(.checking(claimID: id, since: since, follow: follow, post: post))
                }
            default:
                break
            }
        } catch {
            guard version == generation else { return }
            if IslandGateFailure.isTransient(error) { return }
            let failure = error as? IslandGateFailure
            if failure?.code != "offline" && (failure?.httpStatus ?? 0) < 500 { handle(error) }
        }
    }
    private func updatePolling() {
        let shouldPoll: Bool
        if case .checking = state, claimID != nil, !fixture { shouldPoll = true }
        else if case .pendingFollow = state, claimID != nil, !fixture { shouldPoll = true }
        else if case .trial = state, claimID != nil, !fixture { shouldPoll = true }
        else if case .repostChecking = state, claimID != nil, !fixture { shouldPoll = true }
        else { shouldPoll = false }
        if shouldPoll {
            guard polling == nil else { return }
            var interval = pollInterval
            #if DEBUG
            if let debugPollInterval { interval = debugPollInterval }
            #endif
            polling = Task { [weak self] in
                while !Task.isCancelled {
                    do { try await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000)) }
                    catch { break }
                    guard !Task.isCancelled else { break }
                    await self?.pollOnce()
                    #if DEBUG
                    if self?.debugPollInterval == nil { interval = min(interval * 1.5, 300) }
                    #else
                    interval = min(interval * 1.5, 300)
                    #endif
                }
            }
        } else {
            polling?.cancel(); polling = nil
        }
    }
    func handle(_ error: Error) {
        let failure = (error as? IslandGateFailure) ?? IslandGateFailure(code: "server_error")
        switch failure.code {
        case "device_conflict":
            transition(.conflict(deviceName: failure.device_name, rebindAvailableAt: failure.rebind_available_at))
        case "rebind_cooldown":
            transition(.conflict(deviceName: failure.device_name, rebindAvailableAt: failure.rebind_available_at))
        case "device_moved", "revoked", "not_found":
            entitlement = nil
            clearClaimID()
            do { try store.delete("entitlement") } catch { transition(.error(code: "keychain", message: IslandGateFailure(code: "keychain").message)); return }
            transition(.locked(message: failure.message))
        case "trial_expired":
            trialExpiredByServer()
        case "post_not_found", "post_author_mismatch", "post_too_old", "post_missing_tag":
            transition(.error(code: failure.code, message: failure.message))
        case "invalid_handle", "post_url_invalid", "post_url_used", "follow_missing", "post_missing", "claim_expired", "rejected":
            transition(.rejected(message: failure.message))
        case "device_taken":
            transition(.rejected(message: failure.message))
        default:
            transition(.error(code: failure.code, message: failure.message))
        }
    }
    func retryFromRejected() {
        transition(.locked(linkedIn: lockedProvider == "linkedin"))
    }
    func retry() {
        transition(.locked(linkedIn: unlockLinkedInRoute))
        submitCheck()
    }
    func signOut() {
        generation += 1; operation?.cancel(); polling?.cancel(); polling = nil
        wallOperation?.cancel(); wallOperation = nil
        showOnWall = false; wallUpdating = false; wallError = nil
        entitlement = nil; entitlementTier = nil; account = nil; handleText = ""
        clearClaimID()
        transition(.locked())
        do { for name in ["entitlement", "entitlement-tier", "trial-ends-at", "account", "claim-id", "pending-follow"] { try store.delete(name) } }
        catch { handle(error) }
    }
    func setLocalWallConsent(_ show: Bool) {
        showOnWall = show
        onChange.forEach { $0() }
    }
    var wallConsentTitle: String {
        if lockedProvider == "linkedin" { return "Show my name on the Agent Plugins site" }
        let handle = (handleText.isEmpty ? (account?.handle ?? "your handle") : handleText).trimmingCharacters(in: CharacterSet(charactersIn: "@"))
        return "Show @\(handle) on the Agent Plugins site"
    }
    func setWallVisibility(_ show: Bool) {
        guard !wallUpdating, show != showOnWall else { return }
        let previous = showOnWall
        showOnWall = show; wallUpdating = true; wallError = nil
        onChange.forEach { $0() }
        guard !fixture else { wallUpdating = false; return }
        guard isUnlocked else {
            showOnWall = previous; wallUpdating = false; wallError = "Unlock the island before changing site visibility."
            onChange.forEach { $0() }
            return
        }
        wallOperation = Task { [weak self] in
            guard let self else { return }
            do {
                guard let device = self.device ?? (try? IslandGateDevice.load(store: self.store)) else {
                    throw IslandGateFailure(code: "device_identity")
                }
                self.device = device
                try await self.client.setWallVisibility(show, device: device)
                self.account?.show_on_wall = show
                if let account = self.account {
                    do { try self.store.write(try JSONEncoder().encode(account), name: "account") }
                    catch { self.wallError = "Your site preference was updated, but couldn't be saved on this Mac. Try again after unlocking your login Keychain." }
                }
            } catch {
                self.showOnWall = previous
                let failure = (error as? IslandGateFailure) ?? IslandGateFailure(code: "server_error")
                self.wallError = "Couldn't update your site visibility. \(failure.message) Your previous setting is shown. Try again."
            }
            self.wallUpdating = false
            self.onChange.forEach { $0() }
        }
    }
    func openFollow() {
        guard let config else { return }
        let url = lockedProvider == "linkedin" ? config.linkedInFollowURL : (config.x.follow_url ?? config.x.profile_url)
        if let url, url.scheme == "https" { pendingSteps.insert(1); urlOpener.open(url) }
    }
    func openRepost() {
        guard let config else { return }
        let provider = account?.provider ?? lockedProvider
        let url = provider == "linkedin" ? config.linkedin.repost_url : config.x.repost_url
        guard let url, url.scheme?.lowercased() == "https" else { return }
        if let identity = account {
            if case .trial(_, let ends) = state { transition(.repostChecking(account: identity, trialEnds: ends)) }
            else { transition(.repostChecking(account: identity, trialEnds: nil)) }
        }
        urlOpener.open(url)
    }
    func tryMoveHere() {
        guard case .conflict(_, let available) = state else { return }
        guard !IslandGateView.inCooldown(available) else { return }
        transition(.locked(linkedIn: lockedProvider == "linkedin"))
    }
}

// Test hook for entitlement-only apply
extension IslandGate {
    func applyEntitlementForTests(_ token: String) throws { try accept(token) }
    func setConfigForTests(_ config: IslandGateConfig) { self.config = config }
    func setClientTimingForTests(requestTimeout: TimeInterval, coldStartBudget: TimeInterval, retryBackoffBase: TimeInterval = 0.01) {
        client.requestTimeout = requestTimeout
        client.coldStartBudget = coldStartBudget
        client.retryBackoffBase = retryBackoffBase
    }
    func stopPollingForTests() {
        polling?.cancel()
        polling = nil
    }
    func armCheckingClaimForTests(id: String) throws {
        try persistClaimID(id)
        transition(.checking(claimID: id, since: Date(), follow: .pending, post: .pending))
        stopPollingForTests()
    }
}
