import XCTest
import SQLite3
@testable import CUAService

private extension IslandAgent {
    func renamed(_ id: String) -> IslandAgent { var a = self; a.id = id; return a }
}

final class IslandCodexThreadsTests: XCTestCase {
    func testRolloutStateAndTimeouts() {
        let lines = """
        {"type":"response_item","payload":{"type":"task_complete"}}
        {"type":"turn_context","payload":{"model":"gpt-6-luna","effort":"low"}}
        {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"total_tokens":3100000},"last_token_usage":{"total_tokens":620},"model_context_window":1000}}}
        {"type":"event_msg","payload":{"type":"task_started"}}
        """
        let data = Data(lines.utf8)
        let result = IslandCodexThreads.rolloutState(data, age: 600)
        XCTAssertEqual(result.state, "working")
        XCTAssertEqual(result.tokens, 3_100_000)
        XCTAssertEqual(result.contextPercent, 62)
        XCTAssertEqual(IslandCodexThreads.productModelName(result.model), "Luna")
        XCTAssertEqual(IslandCodexThreads.rolloutState(data, age: 601).state, "working", "liveState decides idle vs stopped")
        XCTAssertEqual(IslandCodexThreads.rolloutState(data, age: 1801).state, "idle")
        let done = Data("{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"}}".utf8)
        XCTAssertEqual(IslandCodexThreads.rolloutState(done, age: 180).state, "done")
        XCTAssertEqual(IslandCodexThreads.rolloutState(done, age: 181).state, "idle")
        let aborted = Data("{\"type\":\"event_msg\",\"payload\":{\"type\":\"turn_aborted\",\"reason\":\"interrupted\"}}".utf8)
        XCTAssertEqual(IslandCodexThreads.rolloutState(aborted, age: 0).state, "idle")
    }
    func testInitiatorAndStoppedContracts() throws {
        XCTAssertNil(IslandInitiator.parse(originator: "codex_exec"))
        XCTAssertEqual(IslandInitiator.parse(originator: "acu_exec")?.agent, "ACU")
        let initiator = try XCTUnwrap(IslandInitiator.parse(originator: "acu_exec.grokbot.samantha"))
        XCTAssertEqual(initiator.chain, ["Grok Bot", "Samantha"])
        XCTAssertEqual(initiator.description(app: "Codex"), "Started by ACU (Grok Bot, then Samantha) · running in Codex")
        XCTAssertEqual(IslandInitiator.parse(originator: "acu_exec.abcdefghijklmnopq")?.bot, "abcdefghijklmno…")
        var agent = IslandAgent(name: "Codex", app: "Codex", title: "Task", status: "running")
        agent.initiator = initiator
        XCTAssertEqual(try JSONDecoder().decode(IslandAgent.self, from: JSONEncoder().encode(agent)), agent)
        XCTAssertEqual(IslandAgentRows.metaCandidates("Sol · light · 4m", initiator: initiator, redacted: false), ["via ACU · Sol · light · 4m", "via ACU · Sol · light", "via ACU · Sol", "via ACU", ""])
        XCTAssertEqual(IslandAgentRows.metaCandidates("Sol · light · 4m", initiator: initiator, redacted: true).first, "Sol · light · 4m")
        XCTAssertEqual(IslandAgentRows.voiceOverLabel(agent, title: "Task", meta: "Sol · light · 4m", parent: nil), "Task, Codex, started by ACU, Sol, light, working.")
        let post = try XCTUnwrap(IslandInitiator.fromPost(["agent": "ACU\n", "bot": "Grok Bot\u{0007}"]))
        XCTAssertEqual(post.bot, "Grok Bot")
        XCTAssertEqual(IslandInitiator.fromPost("ACU/Grok Bot"), post)
        let hook = Data("""
        {"type":"event_msg","payload":{"type":"task_complete"}}
        {"type":"event_msg","payload":{"type":"hook_completed","status":"failed"}}
        {"type":"event_msg","payload":{"type":"error","message":"hook: Stop Failed"}}
        """.utf8)
        XCTAssertEqual(IslandCodexThreads.rolloutState(hook, age: 0).state, "done")
        let failure = Data("{\"type\":\"event_msg\",\"payload\":{\"type\":\"error\",\"message\":\"real failure\"}}".utf8)
        XCTAssertEqual(IslandCodexThreads.rolloutState(failure, age: 0).error, "real failure")
        let now = Date()
        var thread = IslandCodexThreads.ThreadRecord(id: UUID().uuidString, rolloutPath: "/task", title: "Task", firstUserMessage: nil, model: nil, effort: nil, updatedAt: now.addingTimeInterval(-30))
        thread.originator = "acu_exec.grokbot"; thread.cwd = "/tmp/worktree"; thread.rollout.state = "working"
        let host = IslandCodexThreads.Host(pid: 1, executable: "codex", rollouts: ["/task"])
        XCTAssertEqual(IslandCodexThreads.rows(records: [thread], hosts: [host], now: now).first?.initiator?.bot, "Grok Bot")
        let stopped = try XCTUnwrap(IslandCodexThreads.rows(records: [thread], hosts: [], now: now).first)
        XCTAssertEqual(stopped.status, "stopped")
        XCTAssertEqual(IslandAgentRows.statusText(stopped), "Stopped")
        XCTAssertEqual(IslandAgentRows.stateWord(stopped), "stopped")
        XCTAssertFalse(IslandAgentRows.voiceOverLabel(stopped, title: "Task", meta: "Stopped", parent: nil).lowercased().contains("failed"))
    }
    func testTailAndLsofFilter() {
        let input = """
        {"type":"turn_context","payload":{"model":"old","effort":"low"}}
        invalid
        {"type":"other","payload":{"model":"ignored"}}
        {"type":"turn_context","payload":{"model":"new","effort":"high"}}
        {bad
        """
        let context = IslandCodexThreads.turnContext(Data(input.utf8))
        XCTAssertEqual(context?.model, "new")
        XCTAssertEqual(context?.effort, "high")
        XCTAssertEqual(IslandCodexThreads.rolloutPaths("p123\nn/private/sessions/x/rollout-a.jsonl\nn/private/auth.json\nn/private/sessions/x/other.jsonl\n"), ["/private/sessions/x/rollout-a.jsonl"])
    }

    func testRowsAndBoundary() {
        let now = Date(timeIntervalSince1970: 10_000)
        var thread = IslandCodexThreads.ThreadRecord(id: "a", rolloutPath: "/a", title: "A task", firstUserMessage: "different prompt", model: "gpt-6-luna", effort: "HIGH", updatedAt: now.addingTimeInterval(-89))
        XCTAssertTrue(IslandCodexThreads.rows(records: [thread], hosts: [], now: now).isEmpty)
        thread.rollout.state = "working"
        let host = IslandCodexThreads.Host(pid: 7, executable: "/opt/homebrew/bin/codex", rollouts: ["/a"])
        let rows = IslandCodexThreads.rows(records: [thread], hosts: [host], now: now)
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.status, "running")
        XCTAssertEqual(rows.first?.local_detail, "Luna · high · 1m")
        thread.cwd = "/tmp/empty-probe"
        XCTAssertTrue(IslandCodexThreads.rows(records: [thread], hosts: [host], now: now).isEmpty)
    }

    func testCodexProcessesFromPsComm() {
        let ps = """
          PID COMM
         5495 /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex
        31610 /Users/x/.local/bin/codex
         8275 /Applications/Cursor.app/Contents/MacOS/Cursor
        """
        let processes = IslandLocalHarnessScanner.codexProcesses(processList: ps)
        XCTAssertEqual(processes.map(\.pid), [5495, 31610])
        XCTAssertTrue(IslandCodexThreads.Host(pid: 5495, executable: processes[0].executable, rollouts: []).isDesktopApp)
        XCTAssertFalse(IslandCodexThreads.Host(pid: 31610, executable: processes[1].executable, rollouts: []).isDesktopApp)
    }

    func testReadOnlyDatabase() throws {
        let dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".build/island-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let path = dir.appendingPathComponent("state.sqlite").path
        var db: OpaquePointer?
        XCTAssertEqual(sqlite3_open(path, &db), SQLITE_OK)
        let sql = "CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, first_user_message TEXT, model TEXT, reasoning_effort TEXT, updated_at_ms INTEGER, archived INTEGER, source TEXT, cwd TEXT, originator TEXT); INSERT INTO threads VALUES ('id', '/a', NULL, 'hello', 'm', 'high', 1000, 0, 'cli', '/', 'codex_exec');"
        XCTAssertEqual(sqlite3_exec(db, sql, nil, nil, nil), SQLITE_OK)
        sqlite3_close(db)
        let readOnly = try XCTUnwrap(IslandCodexThreads.openReadOnly(path))
        XCTAssertEqual(sqlite3_exec(readOnly, "DELETE FROM threads", nil, nil, nil), SQLITE_READONLY)
        sqlite3_close(readOnly)
        XCTAssertEqual(IslandCodexThreads.readDatabase(path).first?.firstUserMessage, "hello")
        XCTAssertEqual(IslandCodexThreads.readDatabase(path).first?.originator, "codex_exec")

        // While a writer holds the lock (as Codex does), the immutable read-only fallback still reads.
        var writer: OpaquePointer?
        XCTAssertEqual(sqlite3_open(path, &writer), SQLITE_OK)
        defer { sqlite3_close(writer) }
        XCTAssertEqual(sqlite3_exec(writer, "PRAGMA locking_mode=EXCLUSIVE; BEGIN EXCLUSIVE;", nil, nil, nil), SQLITE_OK)
        XCTAssertNil(IslandCodexThreads.readDatabase(path, immutable: false))
        XCTAssertEqual(IslandCodexThreads.readDatabase(path).first?.model, "m")
        sqlite3_exec(writer, "ROLLBACK", nil, nil, nil)
    }

    func testDesktopThreadUsesTheAppsEffortLabels() {
        let now = Date(timeIntervalSince1970: 10_000)
        var hi = IslandCodexThreads.ThreadRecord(id: "hi", rolloutPath: "/hi", title: "hi", firstUserMessage: nil,
                                                 model: "gpt-6.1-sol", effort: "low", updatedAt: now.addingTimeInterval(-120))
        hi.rollout.state = "done"; hi.rollout.since = now.addingTimeInterval(-120); hi.originator = "codex_work_desktop"
        var cli = hi
        cli.id = "cli"; cli.originator = "codex_exec"
        let rows = IslandCodexThreads.rows(records: [hi, cli], hosts: [], now: now)
        XCTAssertEqual(rows.first { $0.id.hasSuffix(":hi") }?.local_detail, "Sol · light · done 2m")
        XCTAssertEqual(rows.first { $0.id.hasSuffix(":cli") }?.local_detail, "Sol · low · done 2m")
        XCTAssertEqual(IslandCodexThreads.effortLabel("xhigh", desktop: true), "extra high")
        XCTAssertNil(IslandCodexThreads.effortLabel(nil, desktop: true))
    }

    func testRosterSpecRows() {
        let now = Date(timeIntervalSince1970: 10_000)
        var done = IslandCodexThreads.ThreadRecord(id: "d", rolloutPath: "/d", title: "Ship notes", firstUserMessage: nil,
                                                   model: "gpt-6.1-terra", effort: "medium", updatedAt: now.addingTimeInterval(-120))
        done.rollout.state = "done"; done.rollout.since = now.addingTimeInterval(-120)
        done.rollout.tokens = 3_100_000; done.rollout.contextPercent = 62; done.cwd = "/x/agent-plugins"; done.branch = "codex/a"
        var failed = done
        failed.id = "e"; failed.rollout.state = "error"; failed.rollout.error = "stream disconnected"
        var helper = done
        helper.id = "h"; helper.parentID = "w"
        var working = done
        working.id = "w"; working.rollout.state = "working"; working.rollout.since = now.addingTimeInterval(-240)
        working.model = nil; working.rollout.model = nil
        var probe = working
        probe.id = "p"; probe.title = "Call only the agent-computer-use MCP tool"
        let live = IslandCodexThreads.Host(pid: 42, executable: "/opt/homebrew/bin/codex", rollouts: ["/d"])
        let rows = IslandCodexThreads.rows(records: [done, failed, helper, working, probe], hosts: [live], now: now)
        XCTAssertEqual(rows.map(\.id), ["local:codex-thread:d", "local:codex-thread:e", "local:codex-thread:w"], "helpers fold, probes hide")
        XCTAssertEqual(rows[0].local_detail, "Terra · medium · done 2m", "a done row keeps its model meta")
        XCTAssertEqual(rows[1].local_detail, "Connection lost", "human words on the row")
        XCTAssertTrue(rows[1].tooltip?.hasPrefix("stream disconnected") == true, "technical detail in the tooltip")
        XCTAssertEqual(rows[2].local_detail, "medium · +1 helpers · 4m", "unknown model omitted, never 'unknown'")
        XCTAssertEqual(rows[0].link, "codex://threads/d")
        XCTAssertEqual(rows[0].tooltip, "agent-plugins · codex/a\n3.1M tokens · 62% of context")
        XCTAssertFalse(rows.contains { ($0.local_detail ?? "").contains("unknown") || $0.title.contains("PID") })
        // Its process exits mid-turn: the row turns into error "Stopped", never working.
        let after = IslandCodexThreads.rows(records: [working], hosts: [], now: now)
        XCTAssertEqual(after.map(\.status), ["stopped"])
        XCTAssertEqual(after.first?.local_detail, "Stopped")
        // A stopped thread remains visible even if the process exited before observation.
        var ghost = working; ghost.id = "ghost"; ghost.rolloutPath = "/ghost"
        XCTAssertEqual(IslandCodexThreads.rows(records: [ghost], hosts: [], now: now).first?.status, "stopped")
    }

    func testShortTitles() {
        XCTAssertEqual(IslandCodexThreads.shortTitle("Audit release notes: run the shell command sleep 420, then reply done."), "Audit release notes")
        XCTAssertEqual(IslandCodexThreads.shortTitle("Summarise release notes. Reply with ok."), "Summarise release notes")
        XCTAssertEqual(IslandCodexThreads.shortTitle("Ship v0.2.53 to https://x.dev now"), "Ship v0.2.53 to https://x.dev now")
        XCTAssertEqual(IslandCodexThreads.shortTitle("Delete branch acu/demo?"), "Delete branch acu/demo?")
        let long = IslandCodexThreads.shortTitle("Refactor the island roster layout and the session discovery code paths everywhere")
        XCTAssertTrue(long.hasSuffix("…") && long.count <= 49, long)
        XCTAssertEqual(long, "Refactor the island roster layout and the…")
    }

    @MainActor
    func testRosterOrderCapAndLabels() {
        let ask1 = IslandAgent(id: "pending:a", name: "Grok Bot", app: "Grok", title: "Approve: delete branch acu/x",
                               status: "needs_you", needs_you: true, elapsed_s: 30, request_kind: "approval", destructive: true)
        var ask2 = ask1; ask2.id = "pending:b"; ask2.elapsed_s = 90
        let err = IslandAgent(id: "e", name: "Codex", app: "Codex", title: "Build", status: "error", last_seen_at: 5)
        let w1 = IslandAgent(id: "w1", name: "Codex", app: "Codex", title: "Old", status: "running", last_seen_at: 1)
        let w2 = IslandAgent(id: "w2", name: "Codex", app: "Codex", title: "New", status: "running", last_seen_at: 9)
        let d = IslandAgent(id: "d", name: "Codex", app: "Codex", title: "Done", status: "done", last_seen_at: 20)
        let ordered = IslandAgentRows.hierarchical([d, w1, err, w2, ask1, ask2]).map(\.agent.id)
        XCTAssertEqual(ordered, ["pending:b", "pending:a", "e", "w2", "w1", "d"], "oldest ask, error, newest working, done")
        let model = IslandModel()
        _ = model.apply(IslandSnapshot(kind: "idle", agents: [ask1, ask2, err, w1, w2, d]))
        XCTAssertEqual(model.rosterVisibleRows, 3)
        _ = model.apply(IslandSnapshot(kind: "idle", agents: [ask1, ask2, ask1.renamed("pending:c"), ask1.renamed("pending:d"), w1]))
        XCTAssertEqual(model.rosterVisibleRows, 4, "needs-you rows are never pushed off by the cap")
        var row = w2; row.local_detail = "Luna · low · 4m"
        XCTAssertEqual(IslandAgentRows.voiceOverLabel(ask1, title: ask1.title, meta: "Luna · low · 4m", parent: nil),
                       "Grok Bot, needs approval, Approve: delete branch acu/x. Luna, low, 4 minutes.")
        XCTAssertEqual(IslandAgentRows.redactedTitle(row), "Codex · working")
        XCTAssertEqual(IslandAgentRows.redactedTitle(ask1), "Grok Bot · needs you")
        XCTAssertEqual(IslandAgentRows.statusText(ask2), "Grok Bot · 1m", "needs-you meta: source and age")
    }

    /// A long instruction preamble pushes task_started out of the 256 KB tail; the tail widens.
    func testTailWidensPastLongPreamble() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("tail-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let path = dir.appendingPathComponent("rollout.jsonl").path
        var text = #"{"timestamp":"2026-10-30T13:59:45.419Z","type":"event_msg","payload":{"type":"task_started"}}"# + "\n"
        let filler = #"{"type":"response_item","payload":{"type":"message","text":"\#(String(repeating: "x", count: 1000))"}}"#
        for _ in 0..<300 { text += filler + "\n" }
        try text.write(toFile: path, atomically: true, encoding: .utf8)
        let tail = try XCTUnwrap(IslandCodexThreads.tail(path))
        XCTAssertEqual(IslandCodexThreads.rolloutState(tail, age: 30).state, "working")
    }

    @MainActor
    func testMoreButtonExpandsAndOpenedErrorsClear() {
        let rows = (0..<5).map { IslandAgent(id: "w\($0)", name: "Codex", app: "Codex", title: "T\($0)", status: "running") }
        let err = IslandAgent(id: "e", name: "Codex", app: "Codex", title: "Run tests", status: "error")
        let model = IslandModel()
        _ = model.apply(IslandSnapshot(kind: "idle", agents: rows + [err]))
        model.openAgentList()
        XCTAssertTrue(model.rosterHasMore)
        let collapsed = model.cardHeight
        model.expandRoster()
        XCTAssertFalse(model.rosterHasMore, "no footer once grown")
        XCTAssertEqual(model.rosterVisibleRows, 6)
        XCTAssertGreaterThan(model.cardHeight, collapsed)
        IslandModel.maxCardHeight = { 200 }
        defer { IslandModel.maxCardHeight = { max(240, (NSScreen.main?.visibleFrame.height ?? 800) - 48) } }
        XCTAssertEqual(model.cardHeight, 200, "past the screen's height the list scrolls")
        model.openRow(err)
        XCTAssertFalse(model.agents.contains { $0.id == "e" }, "an error row clears when opened")
        _ = model.apply(IslandSnapshot(kind: "idle", agents: rows + [err]))
        XCTAssertFalse(model.agents.contains { $0.id == "e" }, "and stays cleared while unchanged")
        var again = err; again.status = "running"
        _ = model.apply(IslandSnapshot(kind: "idle", agents: [again]))
        XCTAssertTrue(model.agents.contains { $0.id == "e" }, "a new state brings it back")
    }
}
