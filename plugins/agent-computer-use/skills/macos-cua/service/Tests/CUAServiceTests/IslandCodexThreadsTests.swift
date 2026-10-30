import XCTest
import SQLite3
@testable import CUAService

final class IslandCodexThreadsTests: XCTestCase {
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
        let a = IslandCodexThreads.ThreadRecord(id: "a", rolloutPath: "/a", title: nil, firstUserMessage: "First line\nsecret", model: "model", effort: "high", updatedAt: now.addingTimeInterval(-89), originator: "codex_exec")
        var b = a
        b.id = "b"; b.rolloutPath = "/b"; b.updatedAt = now.addingTimeInterval(-90); b.originator = "Codex Desktop"
        var old = a
        old.id = "old"; old.rolloutPath = "/old"; old.updatedAt = now.addingTimeInterval(-4000)
        let hosts = [
            IslandCodexThreads.Host(pid: 10, executable: "/Users/x/.local/bin/codex", rollouts: ["/a"]),
            IslandCodexThreads.Host(pid: 5495, executable: "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", rollouts: []),
            IslandCodexThreads.Host(pid: 5822, executable: "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", rollouts: []),
        ]
        let rows = IslandCodexThreads.rows(records: [a, b, old], hosts: hosts, now: now)
        XCTAssertEqual(rows.count, 3)
        XCTAssertEqual(rows[0].name, "Codex host")
        XCTAssertEqual(rows[0].title, "ChatGPT app · PID 5495, 5822")
        XCTAssertEqual(rows[1].name, "Codex · terminal")
        XCTAssertEqual(rows[1].title, "First line")
        XCTAssertEqual(rows[1].local_detail, "model · high · working · 1m ago · PID 10")
        XCTAssertEqual(rows[2].name, "Codex · ChatGPT app")
        XCTAssertEqual(rows[2].local_detail, "model · high · idle · 1m ago")
        XCTAssertFalse(rows.contains { $0.title.contains("PID 10") })
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
    }
}
