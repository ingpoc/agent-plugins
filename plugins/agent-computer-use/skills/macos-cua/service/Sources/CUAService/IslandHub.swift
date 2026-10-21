import Foundation
import Network

/// The island's own publish endpoint, hosted by CUAService (always up, independent of Samantha).
///
/// Loopback only (127.0.0.1:8777). Every request must carry `Authorization: Bearer <token>` for its
/// `source` (Keychain service `samantha.island`, account = source) unless the local-dev flag
/// `island.hub.allowUnsignedLocalDev` is on (off by default). Requests with an `Origin` header or a
/// non-loopback `Host` are rejected (browser / DNS-rebinding guard). Sources switched off in
/// Settings → AI Agents are dropped with 403.
@MainActor
final class IslandHub {
    nonisolated static let defaultPort: UInt16 = 8777
    nonisolated static let allowUnsignedKey = "island.hub.allowUnsignedLocalDev"
    /// Port actually bound by this process (nil = bind failed → the voice helper gets no hub URL).
    static var boundPort: UInt16?
    /// Samantha's posts are accepted only while CUAService runs her session (409 otherwise, the helper
    /// retries), so a dying helper can't resurrect her state after Stop.
    var samanthaActive = false

    struct Entry: Equatable {
        var source: AgentSource
        var agent: IslandAgent
        var updated: Date
        var ttl: TimeInterval
        var started: Date = .distantPast
    }

    struct Pending: Equatable, Codable {
        var source: AgentSource
        var approval: IslandAgentApproval
        var created: Date
        var expires: Date = .distantFuture
    }

    /// approved/denied (approval kind), answered/dismissed (choice), expired (TTL ran out).
    enum Decision: String, Codable { case pending, approved, denied, answered, dismissed, expired, unknown }

    struct Answer: Equatable, Codable {
        var status: Decision
        var values: [String] = []
        var text: String? = nil
        var at: Date = Date()
    }

    /// Pending cards + recent answers survive a CUAService restart (0600 JSON, TTL-pruned on load).
    var persistURL: URL? {
        didSet { loadPersisted() }
    }

    let store: IslandSettingsStore
    private(set) var samantha: IslandSnapshot?
    private(set) var entries: [String: Entry] = [:]   // key = source:id
    private(set) var approvals: [Pending] = []
    /// Timed-out cards waiting to be acknowledged (hollow red ring on the source circle).
    private(set) var expiredNotices: [IslandExpired] = []
    nonisolated static let maxNotices = 10
    /// Choice/approval TTL when the poster doesn't set ttl_s (review P0: 15 min).
    nonisolated static let defaultCardTTL: TimeInterval = 900
    private var decisions: [String: Answer] = [:]
    private var waiters: [String: [UUID: CheckedContinuation<Void, Never>]] = [:]
    private var decisionOrder: [String] = []
    /// Composed snapshot after every change (Samantha's state + other sources' agents/approval).
    var onSnapshot: ((IslandSnapshot) -> Void)?
    /// A3: `kind=peek` posts (one-line Quick Peek; never stored as a row).
    var onPeek: ((IslandPeekItem) -> Void)?
    /// 7: island Stop/Retry clicks queued for the posting agent (key = source:id → action).
    private(set) var pendingControls: [String: String] = [:]
    private var controlWaiters: [String: [UUID: CheckedContinuation<Void, Never>]] = [:]
    nonisolated static let allowedControls: Set<String> = ["stop", "retry"]

    init(store: IslandSettingsStore) {
        self.store = store
    }

    // MARK: Composition

    func compose(now: Date = Date()) -> IslandSnapshot {
        var s = samantha ?? IslandSnapshot(kind: "idle")
        let live = entries.values.filter { now.timeIntervalSince($0.updated) < $0.ttl }
            .sorted { $0.updated > $1.updated }
        s.agents += live.map { e in
            var a = e.agent
            if a.status == "running", e.started != .distantPast { a.elapsed_s = max(0, now.timeIntervalSince(e.started)) }
            return a
        }
        // Queued cards: approvals jump the queue (then arrival order); the first owns the pill,
        // all show as needs-you rows + count.
        let queue = queued
        s.agents += queue.map {
            IslandAgent(id: "pending:" + $0.approval.id, name: $0.source.title, app: $0.approval.app,
                        title: $0.approval.title, status: "needs_you", needs_you: true,
                        elapsed_s: now.timeIntervalSince($0.created))
        }
        s.agent_approval = queue.first?.approval ?? s.agent_approval
        s.pending_count = queue.count
        s.expired = expiredNotices
        return s
    }

    /// Display order: approvals (non-choice) first, then choices; arrival order within each.
    var queued: [Pending] {
        approvals.enumerated().sorted { x, y in
            let a = x.element.approval.isChoice ? 1 : 0, b = y.element.approval.isChoice ? 1 : 0
            return a != b ? a < b : x.offset < y.offset
        }.map(\.element)
    }

    private func changed() { onSnapshot?(compose()) }

    /// Drop expired agent rows and cards (1 s clock).
    func expire(now: Date = Date()) {
        let before = entries.count
        entries = entries.filter { now.timeIntervalSince($0.value.updated) < $0.value.ttl }
        // A row that expired takes its queued Stop/Retry with it; parked long-polls wake to an empty answer.
        for id in pendingControls.keys where entries[id] == nil { pendingControls[id] = nil }
        for id in controlWaiters.keys where entries[id] == nil {
            for c in (controlWaiters.removeValue(forKey: id) ?? [:]).values { c.resume() }
        }
        let dead = approvals.filter { $0.expires <= now }
        for d in dead {
            record(d.approval.id, Answer(status: .expired, at: now))
            IslandEventLog.log("expired", id: d.approval.id, source: d.source.cliName, kind: d.approval.kind)
            expiredNotices.removeAll { $0.id == d.approval.id }
            expiredNotices.append(IslandExpired(id: d.approval.id, agent: d.source.title, app: d.approval.app,
                                                title: d.approval.title, at: now.timeIntervalSince1970))
            if expiredNotices.count > Self.maxNotices { expiredNotices.removeFirst(expiredNotices.count - Self.maxNotices) }
        }
        approvals.removeAll { $0.expires <= now }
        if !dead.isEmpty { persist() }
        if entries.count != before || !dead.isEmpty { changed() }
    }

    func clearSamantha() {
        samantha = nil
        changed()
    }

    // MARK: Requests (transport-independent, unit-testable)

    struct Response: Equatable {
        var status: Int
        var body: [String: String]
        /// Choice answers (JSON array / string in the reply body).
        var values: [String]? = nil
        var text: String? = nil

        /// JSON object for both transports.
        var json: [String: Any] {
            var o: [String: Any] = body
            if let values { o["values"] = values }
            if let text { o["text"] = text }
            return o
        }
        static func ok(_ b: [String: String] = [:]) -> Response { .init(status: 200, body: ["ok": "true"].merging(b) { $1 }) }
        static func err(_ code: Int, _ msg: String) -> Response { .init(status: code, body: ["ok": "false", "error": msg]) }
    }

    struct Request {
        var method: String
        var path: String
        var query: [String: String] = [:]
        var headers: [String: String] = [:]   // lowercased names
        var body: Data = Data()
        var localPort: UInt16 = 0
        /// Set only by `IslandHubSocketServer` after getpeereid() == our uid (never by HTTP parsing).
        var peerVerified = false
    }

    /// Unix socket path actually bound (nil = not listening → the helper falls back to HTTP).
    static var boundSocketPath: String?

    func handle(_ r: Request, port: UInt16, now: Date = Date()) -> Response {
        if r.headers["origin"] != nil { return .err(403, "browser-origin requests are not allowed") }
        if r.method == "GET", r.path == "/health" {
            return .ok(["service": "island-hub"])
        }
        if !r.peerVerified {
            let host = (r.headers["host"] ?? "").lowercased()
            let allowedHosts = ["127.0.0.1:\(port)", "localhost:\(port)", "127.0.0.1", "localhost"]
            if !allowedHosts.contains(host) { return .err(403, "bad host") }
        }
        let json = (try? JSONSerialization.jsonObject(with: r.body)) as? [String: Any] ?? [:]
        let rawSource = (json["source"] as? String) ?? r.query["source"] ?? r.headers["x-island-source"] ?? ""
        guard let source = AgentSource.parse(rawSource) else { return .err(400, "unknown source (samantha|grok-bot|cursor|codex)") }
        if r.peerVerified {
            // Same-uid local peer: the source must be opted in (token created by the user's Rotate /
            // Samantha toggle). CUAService reads its own item — no Keychain prompt for anyone.
            guard optedIn(source) else {
                return .err(401, "\(source.cliName) is not set up: Island Settings → AI Agents → \(source.title) → Rotate")
            }
        } else {
            guard authorized(r, source: source) else { return .err(401, "missing or invalid token for \(source.cliName)") }
        }

        switch (r.method, r.path) {
        case ("POST", "/api/island/publish"):
            if source == .samantha {
                // Samantha off in AI Agents is applied by the model (confirm cards still surface).
                guard samanthaActive else { return .err(409, "Samantha's session is not active") }
                return publishSamantha(r.body)
            }
            guard store.sourceEnabled(source) else { return .err(403, "source disabled in Island Settings") }
            return publishAgent(json, source: source, now: now)
        case ("GET", "/api/agent/approval"):
            let id = Self.key(source, r.query["id"] ?? "")
            return answerResponse(id)
        case ("POST", "/api/agent/approval"):
            // Stub kept for scripts: a source may answer/withdraw its own approval.
            let id = Self.key(source, json["id"] as? String ?? "")
            guard let p = approvals.first(where: { $0.approval.id == id && $0.source == source }) else { return .err(404, "no such approval") }
            guard !p.approval.isChoice else { return .err(400, "choice cards are answered on the island (or cleared with kind=clear)") }
            resolveApproval(id: id, approved: json["approved"] as? Bool ?? false, via: "api")
            return answerResponse(id)
        case ("GET", "/api/agent/control"):
            // The posting agent picks up Stop/Retry clicks for its own row (delivered once).
            let id = Self.key(source, r.query["id"] ?? "")
            let action = entries[id] == nil ? "" : (pendingControls.removeValue(forKey: id) ?? "")
            if !action.isEmpty { IslandEventLog.log("control-delivered", id: id, source: source.cliName, kind: action) }
            return .ok(["id": r.query["id"] ?? "", "action": action])
        case ("GET", "/api/island/state"):
            let s = compose(now: now)
            return .ok(["kind": s.kind, "title": s.title, "agents": String(s.agents.count),
                        "approval": s.agent_approval?.id ?? ""])
        default:
            return .err(404, "not found")
        }
    }

    /// Socket peers: the local-dev unsigned flag does NOT apply here (HTTP only).
    private func optedIn(_ source: AgentSource) -> Bool {
        return store.tokenForVerification(source) != nil || store.tokenForVerification(source, refresh: true) != nil
    }

    private func authorized(_ r: Request, source: AgentSource) -> Bool {
        if store.defaults.bool(forKey: Self.allowUnsignedKey) { return true }
        let auth = r.headers["authorization"] ?? ""
        guard auth.hasPrefix("Bearer ") else { return false }
        let supplied = String(auth.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        guard !supplied.isEmpty else { return false }
        if let expected = store.tokenForVerification(source), Self.constantTimeEqual(supplied, expected) { return true }
        // Cached value may be stale (item changed outside Rotate): re-read once.
        guard let fresh = store.tokenForVerification(source, refresh: true) else { return false }
        return Self.constantTimeEqual(supplied, fresh)
    }

    nonisolated static func constantTimeEqual(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        var diff = UInt8(x.count == y.count ? 0 : 1)
        for i in 0..<max(x.count, y.count) {
            diff |= (i < x.count ? x[i] : 0) ^ (i < y.count ? y[i] : 0)
        }
        return diff == 0
    }

    nonisolated static func key(_ s: AgentSource, _ id: String) -> String { "\(s.rawValue):\(id)" }

    private func publishSamantha(_ body: Data) -> Response {
        guard var snap = try? JSONDecoder().decode(IslandSnapshot.self, from: body) else { return .err(400, "bad snapshot") }
        snap.agents = []          // Samantha can't speak for other sources
        snap.agent_approval = nil
        ingestSamantha(snap)
        return .ok()
    }

    /// Samantha's latest state (HTTP path above; capture harness feeds it directly).
    func ingestSamantha(_ snap: IslandSnapshot) {
        samantha = snap
        changed()
    }

    /// Agent schema: {source, id, kind: running|needs_you|done|error|approval|clear, title, detail, app,
    /// destructive, ttl_s}.
    private func publishAgent(_ j: [String: Any], source: AgentSource, now: Date) -> Response {
        let rawId = (j["id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "default"
        guard rawId.count <= 80 else { return .err(400, "id too long") }
        let id = Self.key(source, rawId)
        let kind = (j["kind"] as? String ?? "running").lowercased()
        let title = String((j["title"] as? String ?? "").prefix(120))
        let detail = String((j["detail"] as? String ?? "").prefix(200))
        let app = String((j["app"] as? String ?? source.defaultApp).prefix(40))
        switch kind {
        case "clear":
            entries[id] = nil
            pendingControls[id] = nil
            if let p = approvals.first(where: { $0.approval.id == id }) {
                approvals.removeAll { $0.approval.id == id }
                record(id, Answer(status: .dismissed, at: now))   // answered in chat: wake any waiter
                IslandEventLog.log("answered", id: id, source: source.cliName, kind: p.approval.kind, ["via": "chat-clear"])
                persist()
            }
            if expiredNotices.contains(where: { $0.id == id }) {
                expiredNotices.removeAll { $0.id == id }   // handled in chat: the hollow ring goes
                persist()
            }
            changed()
            return .ok(["id": rawId])
        case "approval", "choice":
            guard store.mayRaiseApproval(source) else { return .err(403, "approval cards are off for \(source.cliName)") }
            let prompt = String(((j["prompt"] as? String) ?? title).prefix(160))
            guard !prompt.isEmpty else { return .err(400, "title/prompt required") }
            var options: [IslandChoiceOption] = []
            for o in (j["options"] as? [Any]) ?? [] {
                if let d = o as? [String: Any] {
                    let label = String(((d["label"] as? String) ?? (d["value"] as? String) ?? "").prefix(40))
                    let value = String(((d["value"] as? String) ?? label).prefix(80))
                    if !label.isEmpty { options.append(.init(label: label, value: value)) }
                } else if let str = o as? String, !str.isEmpty {
                    options.append(.init(label: String(str.prefix(40)), value: String(str.prefix(80))))
                }
            }
            guard options.count <= 6 else { return .err(400, "at most 6 options") }
            guard Set(options.map(\.value)).count == options.count else { return .err(400, "option values must be unique") }
            let allowText = j["allow_text"] as? Bool ?? false
            if kind == "choice", options.isEmpty, !allowText { return .err(400, "choice needs options or allow_text") }
            let ttl = min(86_400, max(30, (j["ttl_s"] as? NSNumber)?.doubleValue ?? Self.defaultCardTTL))
            var a = IslandAgentApproval(id: id, agent: source.title, app: app, title: prompt, subtitle: detail,
                                        destructive: j["destructive"] as? Bool ?? false, kind: kind,
                                        options: options, multi: j["multi"] as? Bool ?? false, allow_text: allowText,
                                        primary: String((j["primary"] as? String ?? "").prefix(80)),
                                        danger: String((j["danger"] as? String ?? "").prefix(80)))
            a.expires_at = now.addingTimeInterval(ttl).timeIntervalSince1970
            expiredNotices.removeAll { $0.id == id }   // re-posted: it's live again
            if let i = approvals.firstIndex(where: { $0.approval.id == id }) {
                approvals[i].approval = a          // re-post updates in place (keeps queue position)
                approvals[i].expires = now.addingTimeInterval(ttl)
            } else {
                approvals.append(Pending(source: source, approval: a, created: now, expires: now.addingTimeInterval(ttl)))
            }
            decisions[id] = nil
            persist()
            changed()
            return .ok(["id": rawId, "status": Decision.pending.rawValue])
        case "peek":
            // A3 Quick Peek: one line (title = sender/headline), optional body; shown ~3 s, never a row.
            guard !title.isEmpty else { return .err(400, "title required") }
            let cap = store.cap(source)
            guard cap.rank >= AttentionCap.info.rank else { return .err(403, "peeks need attention cap info or higher for \(source.cliName)") }
            guard store.bool(IslandSettingsSchema.agentPeeks) else { return .err(403, "agent peeks are off in Island settings") }
            onPeek?(IslandPeekItem(id: id, source: source.title, app: app, symbol: String((j["symbol"] as? String ?? "").prefix(40)),
                                   title: title, body: detail, at: now))
            return .ok(["id": rawId])
        case "running", "needs_you", "done", "error":
            guard !title.isEmpty else { return .err(400, "title required") }
            let cap = store.cap(source)
            let needs = kind == "needs_you" && cap.rank >= AttentionCap.attention.rank
            let defaultTTL: TimeInterval = (kind == "done" || kind == "error") ? 30 : 600
            let ttl = min(3600, max(5, (j["ttl_s"] as? NSNumber)?.doubleValue ?? defaultTTL))
            let controls = ((j["controls"] as? [Any]) ?? []).compactMap { ($0 as? String)?.lowercased() }
                .filter { Self.allowedControls.contains($0) }
            let started = entries[id].map { $0.agent.status == "running" && kind == "running" ? $0.started : now } ?? now
            let agent = IslandAgent(id: id, name: source.title, app: app, title: title,
                                    status: kind == "needs_you" ? "needs_you" : kind, needs_you: needs,
                                    controls: Array(Set(controls)).sorted())
            entries[id] = Entry(source: source, agent: agent, updated: now, ttl: ttl, started: started)
            changed()
            return .ok(["id": rawId])
        default:
            return .err(400, "kind must be running|needs_you|done|error|approval|choice|clear")
        }
    }

    // MARK: Row controls (island Stop / Retry)

    /// Queue a control for the agent that owns row `id` (source:id). Stop is offered on every running
    /// row; other controls only when advertised. False when the row is gone or the control is not
    /// allowed — the island then reverts with a red flash (an agent that never acks times out the same way).
    @discardableResult
    func requestControl(id: String, action: String) -> Bool {
        guard Self.allowedControls.contains(action), let e = entries[id],
              e.agent.controls.contains(action) || (action == "stop" && e.agent.status == "running") else { return false }
        pendingControls[id] = action
        IslandEventLog.log("control-queued", id: id, source: e.source.cliName, kind: action)
        let ws = controlWaiters.removeValue(forKey: id) ?? [:]
        for c in ws.values { c.resume() }
        return true
    }

    /// False when the waiter cap is hit (caller answers 503 instead of an instant empty poll).
    @discardableResult
    func waitForControl(_ id: String, timeout: TimeInterval) async -> Bool {
        guard pendingControls[id] == nil else { return true }
        guard controlWaiters.values.reduce(0, { $0 + $1.count }) < Self.maxWaiters, (controlWaiters[id]?.count ?? 0) < Self.maxWaitersPerCard else { return false }
        let token = UUID()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            controlWaiters[id, default: [:]][token] = c
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                guard let self, let c = self.controlWaiters[id]?.removeValue(forKey: token) else { return }
                if self.controlWaiters[id]?.isEmpty == true { self.controlWaiters[id] = nil }
                c.resume()
            }
        }
        return true
    }

    // MARK: Approvals (island buttons)

    func resolveApproval(id: String, approved: Bool, via: String = "island") {
        guard let p = approvals.first(where: { $0.approval.id == id }) else { return }
        let ans = p.approval.isChoice
            ? Answer(status: approved ? .answered : .dismissed, values: approved && !p.approval.primary.isEmpty ? [p.approval.primary] : [])
            : Answer(status: approved ? .approved : .denied)
        finish(id, ans, via: via)
    }

    /// Choice card answered from the island. Values are filtered to the card's options.
    func answerChoice(id: String, values: [String], text: String?, via: String = "island") {
        guard let p = approvals.first(where: { $0.approval.id == id }) else { return }
        let allowed = Set(p.approval.options.map(\.value))
        var v = values.filter { allowed.contains($0) }
        if !p.approval.multi { v = Array(v.prefix(1)) }
        let t = p.approval.allow_text ? text.map { String($0.prefix(2000)) }.flatMap { $0.isEmpty ? nil : $0 } : nil
        guard !v.isEmpty || t != nil else { return }
        finish(id, Answer(status: .answered, values: v, text: t), via: via)
    }

    func dismissChoice(id: String) {
        guard approvals.contains(where: { $0.approval.id == id }) else { return }
        finish(id, Answer(status: .dismissed), via: "island-x")
    }

    /// Agent list seen: drop these timed-out notices.
    func ackExpired(ids: [String]) {
        let before = expiredNotices.count
        expiredNotices.removeAll { ids.contains($0.id) }
        guard expiredNotices.count != before else { return }
        persist()
        changed()
    }

    private func finish(_ id: String, _ ans: Answer, via: String = "island") {
        if let p = approvals.first(where: { $0.approval.id == id }) {
            IslandEventLog.log("answered", id: id, source: p.source.cliName, kind: p.approval.kind,
                               ["via": via, "status": ans.status.rawValue, "values": ans.values.count, "has_text": ans.text != nil])
        }
        approvals.removeAll { $0.approval.id == id }
        record(id, ans)
        persist()
        SamanthaActivityLog.startup(phase: "agent_approval_answer", status: "ok",
                                    detail: "\(id.prefix(60)) \(ans.status.rawValue)")
        changed()
    }

    private func record(_ id: String, _ ans: Answer) {
        decisions[id] = ans
        decisionOrder.removeAll { $0 == id }
        decisionOrder.append(id)
        while decisionOrder.count > 200 { decisions[decisionOrder.removeFirst()] = nil }
        wake(id)
    }

    func decision(_ id: String) -> Decision {
        if let d = decisions[id] { return d.status }
        return approvals.contains { $0.approval.id == id } ? .pending : .unknown
    }

    func answer(_ id: String) -> Answer? { decisions[id] }

    private func answerResponse(_ id: String) -> Response {
        var r = Response.ok(["status": decision(id).rawValue])
        if let a = decisions[id], a.status == .answered { r.values = a.values; r.text = a.text }
        return r
    }

    // MARK: Long-poll (no busy polling: the request parks until answered, cleared, expired or timed out)

    /// `GET /api/agent/approval?wait=N` (N ≤ 600 per request; island-post loops to 3600).
    func handleAsync(_ r: Request, port: UInt16) async -> Response {
        if r.method == "GET", r.path == "/api/agent/control", let w = r.query["wait"].flatMap(Double.init), w > 0,
           let source = AgentSource.parse(r.query["source"] ?? ""), r.headers["origin"] == nil {
            let id = Self.key(source, r.query["id"] ?? "")
            if pendingControls[id] == nil, entries[id] != nil {
                // Auth is checked by handle(); park only after a cheap pre-check passes.
                let pre = handle(Request(method: "GET", path: "/api/island/state", query: r.query, headers: r.headers,
                                         body: r.body, peerVerified: r.peerVerified), port: port)
                guard pre.status == 200 else { return pre }
                guard await waitForControl(id, timeout: min(w, 600)) else { return .err(503, "too many waiters; retry later") }
            }
            return handle(r, port: port)
        }
        let first = handle(r, port: port)
        guard r.method == "GET", r.path == "/api/agent/approval", first.status == 200,
              first.body["status"] == Decision.pending.rawValue,
              let w = r.query["wait"].flatMap(Double.init), w > 0,
              let source = AgentSource.parse(r.query["source"] ?? "") else { return first }
        let id = Self.key(source, r.query["id"] ?? "")
        await waitForChange(id, timeout: min(w, 600))
        return answerResponse(id)
    }

    func waitForChange(_ id: String, timeout: TimeInterval) async {
        guard decision(id) == .pending else { return }
        // Bounded fan-out: a same-uid client can't park unlimited requests.
        guard waiterCount < Self.maxWaiters, (waiters[id]?.count ?? 0) < Self.maxWaitersPerCard else { return }
        let token = UUID()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            waiters[id, default: [:]][token] = c
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                self?.resumeWaiter(id, token)
            }
        }
    }

    private func resumeWaiter(_ id: String, _ token: UUID) {
        guard let c = waiters[id]?.removeValue(forKey: token) else { return }
        if waiters[id]?.isEmpty == true { waiters[id] = nil }
        c.resume()
    }

    private func wake(_ id: String) {
        let ws = waiters.removeValue(forKey: id) ?? [:]
        for c in ws.values { c.resume() }
    }

    var waiterCount: Int { waiters.values.reduce(0) { $0 + $1.count } }
    nonisolated static let maxWaiters = 64
    nonisolated static let maxWaitersPerCard = 4

    // MARK: Persistence

    private struct Persisted: Codable {
        var pending: [Pending]
        var answers: [String: Answer]
        var order: [String]
        var expired: [IslandExpired]? = nil
    }

    private func persist() {
        guard let url = persistURL else { return }
        let state = Persisted(pending: approvals, answers: decisions, order: decisionOrder, expired: expiredNotices)
        guard let data = try? JSONEncoder().encode(state) else {
            SamanthaActivityLog.startup(phase: "island_pending", status: "fail", detail: "encode")
            return
        }
        let tmp = url.appendingPathExtension("tmp")
        guard FileManager.default.createFile(atPath: tmp.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
            SamanthaActivityLog.startup(phase: "island_pending", status: "fail", detail: "write")
            return
        }
        // rename(2) is atomic and keeps the 0600 temp file's mode.
        if rename(tmp.path, url.path) != 0 {
            unlink(tmp.path)
            SamanthaActivityLog.startup(phase: "island_pending", status: "fail", detail: "rename errno \(errno)")
        }
    }

    private func loadPersisted(now: Date = Date()) {
        guard let url = persistURL, let data = try? Data(contentsOf: url),
              let st = try? JSONDecoder().decode(Persisted.self, from: data) else { return }
        approvals = st.pending.filter { $0.expires > now && store.mayRaiseApproval($0.source) }
        let fresh = st.answers.filter { now.timeIntervalSince($0.value.at) < 3600 }
        decisions = fresh
        decisionOrder = st.order.filter { fresh[$0] != nil }
        // Cards that expired while CUAService was down still get their notice + ring.
        var notices = st.expired ?? []
        for p in st.pending where p.expires <= now && store.mayRaiseApproval(p.source) {
            decisions[p.approval.id] = Answer(status: .expired, at: now)
            notices.append(IslandExpired(id: p.approval.id, agent: p.source.title, app: p.approval.app,
                                         title: p.approval.title, at: p.expires.timeIntervalSince1970))
        }
        expiredNotices = Array(notices.filter { now.timeIntervalSince1970 - $0.at < 86_400 }.suffix(Self.maxNotices))
        changed()
    }

    /// Source switched off / lost approval rights in Settings: drop its rows and cards at once.
    func applyGating() {
        let before = (entries.count, approvals.count)
        entries = entries.filter { store.sourceEnabled($0.value.source) }
        let gone = approvals.filter { !store.mayRaiseApproval($0.source) }
        for g in gone {
            record(g.approval.id, Answer(status: .dismissed))
            IslandEventLog.log("answered", id: g.approval.id, source: g.source.cliName, kind: g.approval.kind, ["via": "gated"])
        }
        approvals.removeAll { !store.mayRaiseApproval($0.source) }
        if !gone.isEmpty { persist() }
        if before != (entries.count, approvals.count) { changed() }
    }
}

// MARK: - HTTP transport (Network.framework, loopback only)

/// Minimal HTTP/1.1: one request per connection, Content-Length bodies ≤ 64 KB, Connection: close.
final class IslandHubServer: @unchecked Sendable {
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "island-hub")
    private let handler: @Sendable (IslandHub.Request) async -> IslandHub.Response
    private(set) var port: UInt16 = 0

    init(handler: @escaping @Sendable (IslandHub.Request) async -> IslandHub.Response) {
        self.handler = handler
    }

    /// Bind 127.0.0.1:`port` (0 = ephemeral, tests). Returns the bound port, or nil.
    func start(port: UInt16) -> UInt16? {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port) ?? .any)
        guard let l = try? NWListener(using: params) else { return nil }
        let ready = DispatchSemaphore(value: 0)
        var bound: UInt16?
        l.stateUpdateHandler = { state in
            switch state {
            case .ready: bound = l.port?.rawValue; ready.signal()
            case .failed: ready.signal()
            default: break
            }
        }
        l.newConnectionHandler = { [weak self] c in self?.accept(c) }
        l.start(queue: queue)
        _ = ready.wait(timeout: .now() + 1)  // loopback bind resolves in ms
        guard let b = bound else { l.cancel(); return nil }
        listener = l
        self.port = b
        return b
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    private func accept(_ c: NWConnection) {
        c.start(queue: queue)
        let parsed = Flag()
        receive(c, buffer: Data(), parsed: parsed)
        // Slowloris guard: the request must arrive within 10 s (a parked long-poll is fine after that).
        queue.asyncAfter(deadline: .now() + 10) { if !parsed.value { c.cancel() } }
    }

    private final class Flag: @unchecked Sendable { var value = false }

    private func receive(_ c: NWConnection, buffer: Data, parsed: Flag) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, err in
            guard let self else { return }
            var buf = buffer
            if let data { buf.append(data) }
            if buf.count > 70_000 { self.reply(c, .err(413, "too large")); return }
            switch Self.parse(buf) {
            case .complete(var req):
                parsed.value = true
                req.localPort = self.port
                let h = self.handler
                Task { let r = await h(req); self.reply(c, r) }
            case .incomplete:
                if done || err != nil { c.cancel() } else { self.receive(c, buffer: buf, parsed: parsed) }
            case .bad:
                self.reply(c, .err(400, "bad request"))
            }
        }
    }

    enum Parse { case complete(IslandHub.Request), incomplete, bad }

    static func parse(_ buf: Data) -> Parse {
        guard let sep = buf.range(of: Data("\r\n\r\n".utf8)) else { return buf.count > 16_384 ? .bad : .incomplete }
        guard let head = String(data: buf[..<sep.lowerBound], encoding: .utf8) else { return .bad }
        var lines = head.components(separatedBy: "\r\n")
        let start = lines.removeFirst().split(separator: " ")
        guard start.count >= 2 else { return .bad }
        var headers: [String: String] = [:]
        for l in lines {
            guard let i = l.firstIndex(of: ":") else { continue }
            headers[l[..<i].lowercased()] = l[l.index(after: i)...].trimmingCharacters(in: .whitespaces)
        }
        let len = Int(headers["content-length"] ?? "0") ?? -1
        guard len >= 0, len <= 65536 else { return .bad }
        let bodyStart = sep.upperBound
        guard buf.count - bodyStart >= len else { return .incomplete }
        let target = String(start[1])
        let comps = URLComponents(string: target)
        var q: [String: String] = [:]
        for item in comps?.queryItems ?? [] { q[item.name] = item.value ?? "" }
        return .complete(.init(method: String(start[0]).uppercased(), path: comps?.path ?? target, query: q,
                               headers: headers, body: buf.subdata(in: bodyStart..<(bodyStart + len))))
    }

    private func reply(_ c: NWConnection, _ r: IslandHub.Response) {
        let body = (try? JSONSerialization.data(withJSONObject: r.json, options: [.sortedKeys])) ?? Data("{}".utf8)
        let reason = [200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Too Large"][r.status] ?? "Error"
        var head = "HTTP/1.1 \(r.status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        if r.status == 401 { head = head.replacingOccurrences(of: "Connection: close", with: "WWW-Authenticate: Bearer\r\nConnection: close") }
        c.send(content: Data(head.utf8) + body, completion: .contentProcessed { _ in c.cancel() })
    }
}
