import XCTest
@testable import CUAService

/// Real Claude desktop 4.4212 AX dumps (free tier, 2026-11-01), redacted: other chats' titles are
/// "Chat N", message text is dropped. A plain chat's sidebar row stays "Idle <title>" while Claude
/// replies; the only working signals are the composer's "Stop response" button (depth 32 from the
/// app element) and the "Currently streaming message" article.
final class IslandClaudeDesktopLiveFixtureTests: XCTestCase {
    typealias Activity = IslandClaudeDesktopActivity
    typealias E = Activity.Element

    private func fixture(_ name: String) throws -> any Activity.AXElement {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Fixtures/claude-desktop-4.4212-\(name).jsonl")
        let lines = try String(contentsOf: url, encoding: .utf8).split(separator: "\n")
        // Depth-first dump with a depth per node (window = 0) → tree under an AXApplication root.
        final class Node { var e: E; var kids: [Node] = []; init(_ e: E) { self.e = e } }
        let root = Node(E(role: "AXApplication"))
        var stack: [(Int, Node)] = [(-1, root)]
        for line in lines {
            let o = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
            let depth = try XCTUnwrap(o["d"] as? Int)
            let node = Node(E(role: o["role"] as? String ?? "", subrole: o["subrole"] as? String,
                              title: o["title"] as? String, description: o["description"] as? String,
                              selected: (o["selected"] as? Bool ?? false) || o["value"] as? String == "1"))
            while let last = stack.last, last.0 >= depth { stack.removeLast() }
            stack.last!.1.kids.append(node)
            stack.append((depth, node))
        }
        func build(_ n: Node) -> E { var e = n.e; e.children = n.kids.map(build); return e }
        return build(root)
    }

    func testStreamingChatIsWorkingUnderItsHeaderTitle() throws {
        let snap = Activity.read(try fixture("streaming"))
        XCTAssertTrue(snap.stopButtonVisible, "Stop response sits at depth 32: below the old 30 cap")
        XCTAssertTrue(snap.streamingMessageVisible)
        XCTAssertTrue(snap.chatTabActive)
        XCTAssertEqual(snap.openChatTitle, "History of lighthouses essay")
        XCTAssertEqual(snap.rows.filter { $0.status == .working }.map(\.title), ["History of lighthouses essay"])
        XCTAssertEqual(snap.agents.map(\.title), ["History of lighthouses essay"])
        XCTAssertEqual(snap.agents.first?.status, "running")
        XCTAssertEqual(snap.agents.first?.local_detail, "Sonnet 5.5 · medium")
    }

    func testNewChatBeforeRetitleMatchesTruncatedHeader() throws {
        let snap = Activity.read(try fixture("started"))
        XCTAssertTrue(snap.responding)
        XCTAssertEqual(snap.agents.map(\.title), ["ACU island test: write a detai..."])
    }

    func testFinishedReplyIsNotWorkingAndHoldsDone() throws {
        let streaming = Activity.read(try fixture("streaming"))
        let done = Activity.read(try fixture("done"))
        XCTAssertFalse(done.responding)
        XCTAssertTrue(done.rows.allSatisfy { $0.status != .working })
        XCTAssertTrue(done.agents.isEmpty)
        var finished: [String: Date] = [:]
        let t0 = Date(timeIntervalSince1970: 1_000)
        let held = Activity.retainFinishedRows(done.rows, previous: streaming.rows, finishedAt: &finished, now: t0)
        XCTAssertEqual(Activity.Snapshot(rows: held).agents.map(\.title), ["History of lighthouses essay"])
        XCTAssertEqual(Activity.Snapshot(rows: held).agents.map(\.status), ["done"])
    }

    func testRetitleMidReplyDoesNotFlashDone() throws {
        let started = Activity.read(try fixture("started"))
        let streaming = Activity.read(try fixture("streaming"))
        var finished: [String: Date] = [:]
        let rows = Activity.retainFinishedRows(streaming.rows, previous: started.rows, finishedAt: &finished,
                                               now: Date(timeIntervalSince1970: 1_000))
        XCTAssertEqual(Activity.Snapshot(rows: rows).agents.map(\.status), ["running"])
        XCTAssertTrue(finished.isEmpty)
    }

    func testSameTitleHandlesTruncation() {
        XCTAssertTrue(Activity.sameTitle("ACU island test: write a detai...", "ACU island test: write a detailed essay"))
        XCTAssertFalse(Activity.sameTitle("Ab...", "Abc"))
        XCTAssertFalse(Activity.sameTitle("Alpha", "Beta"))
    }
}
