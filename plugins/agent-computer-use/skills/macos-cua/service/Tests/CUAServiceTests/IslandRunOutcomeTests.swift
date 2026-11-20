import XCTest
@testable import CUAService

/// ACU-16: a run that ended without a final answer is Stopped with a short reason, never Done.
/// Fixtures are the real record shapes from the three harnesses (secrets removed).
final class IslandRunOutcomeTests: XCTestCase {
    // MARK: - Fixtures

    private let codexTaskCompleteUsageLimit = """
    {"timestamp":"2026-11-20T04:06:15.922Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"fixture","last_agent_message":null,"error":{"message":"You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage to purchase more credits or try again at Nov 22nd, 2026 12:28 PM."}}}
    """

    private let codexRateLimit100 = """
    {"timestamp":"2026-11-20T04:03:05.989Z","type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":47.0,"window_minutes":300,"resets_at":1795159029},"secondary":{"used_percent":100.0,"window_minutes":10080,"resets_at":1795330707},"rate_limit_reached_type":null}}}
    """

    private let codexTaskCompleteOK = """
    {"timestamp":"2026-11-20T03:30:00.000Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"fixture","last_agent_message":"Done.","error":null}}
    """

    private let claudeApiErrorAssistant = """
    {"type":"assistant","isApiErrorMessage":true,"error":"authentication_failed","timestamp":"2026-11-03T10:02:42.712Z","message":{"role":"assistant","stop_reason":"stop_sequence","content":[{"type":"text","text":"(text removed)"}]}}
    """

    private let claudeSystemApiError429 = """
    {"type":"system","subtype":"api_error","error":{"status":429,"message":"429 {\\"type\\":\\"error\\",\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"code\\":\\"1113\\",\\"message\\":\\"[1113][Insufficient balance or no resour"},"timestamp":"2026-10-31T19:07:58.431Z","message":{"role":null,"stop_reason":null,"content":[{"type":"text","text":"(text removed)"}]}}
    """

    private let claudeEndTurn = """
    {"type":"assistant","timestamp":"2026-05-14T19:57:30.503Z","message":{"role":"assistant","stop_reason":"end_turn","content":[{"type":"text","text":"(text removed)"}]}}
    """

    private let openCodeAborted = """
    {"role":"assistant","finish":null,"error":{"name":"MessageAbortedError","data":{"message":"Aborted"}}}
    """

    private let openCodeAPIError = """
    {"role":"assistant","finish":null,"error":{"name":"APIError","data":{"message":"OpenCode 1.18.0 or newer is required to use the free tier"}}}
    """

    private let openCodeContextOverflow = """
    {"role":"assistant","finish":"error","error":{"name":"ContextOverflowError","data":{"message":"Conversation history too large to compact - exceeds model context limit"}}}
    """

    private let openCodeStopOK = """
    {"role":"assistant","finish":"stop"}
    """

    // MARK: - Helpers

    private func object(_ json: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
    }

    private func payload(_ json: String) throws -> [String: Any] {
        try XCTUnwrap(try object(json)["payload"] as? [String: Any])
    }

    /// The test's "today" in Asia/Kolkata: 20 Nov 2026, so a 22 Nov reset needs its date.
    private func testNow() throws -> Date {
        try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-11-20T04:06:15Z"))
    }

    // MARK: - Codex

    func testCodexUsageLimitCutoffIsQuotaExhaustedWithReset() throws {
        let now = try testNow()
        let taskComplete = try payload(codexTaskCompleteUsageLimit)
        let rateLimits = try XCTUnwrap(try payload(codexRateLimit100)["rate_limits"] as? [String: Any])
        XCTAssertEqual(IslandRunOutcome.codex(taskComplete: taskComplete, rateLimits: rateLimits, now: now),
                       .stopped(reason: "Quota exhausted, resets 22 Nov 12:28 IST"))
        // The 100% window alone marks a cutoff, even when the error text says nothing about quota.
        let unknownError: [String: Any] = ["error": ["message": "stream disconnected"], "last_agent_message": NSNull()]
        XCTAssertEqual(IslandRunOutcome.codex(taskComplete: unknownError, rateLimits: rateLimits, now: now),
                       .stopped(reason: "Quota exhausted, resets 22 Nov 12:28 IST"))
        // Other failures stay short, with no invented reset.
        let apiError: [String: Any] = ["error": ["message": "stream disconnected"], "last_agent_message": NSNull()]
        XCTAssertEqual(IslandRunOutcome.codex(taskComplete: apiError, rateLimits: nil, now: now),
                       .stopped(reason: "API error"))
    }

    func testCodexCompleteWithFinalMessageIsDone() throws {
        let taskComplete = try payload(codexTaskCompleteOK)
        XCTAssertEqual(IslandRunOutcome.codex(taskComplete: taskComplete, rateLimits: nil, now: try testNow()), .done)
    }

    func testCodexEmptyFinalMessageIsNoFinalAnswer() throws {
        let silent = try payload("""
        {"type":"event_msg","payload":{"type":"task_complete","turn_id":"fixture","last_agent_message":null,"error":null}}
        """)
        XCTAssertEqual(IslandRunOutcome.codex(taskComplete: silent, rateLimits: nil, now: try testNow()),
                       .stopped(reason: "No final answer"))
        let blank: [String: Any] = ["last_agent_message": "   ", "error": NSNull()]
        XCTAssertEqual(IslandRunOutcome.codex(taskComplete: blank, rateLimits: nil, now: try testNow()),
                       .stopped(reason: "No final answer"))
    }

    // MARK: - Claude Code

    func testClaudeAuthenticationFailedIsNotLoggedIn() throws {
        XCTAssertEqual(IslandRunOutcome.claudeCode(line: try object(claudeApiErrorAssistant)),
                       .stopped(reason: "Not logged in"))
        XCTAssertNil(IslandRunOutcome.claudeCode(line: ["type": "assistant",
                                                       "message": ["stop_reason": "tool_use"]]), "mid-turn")
    }

    func testClaudeSystem429IsQuotaExhausted() throws {
        XCTAssertEqual(IslandRunOutcome.claudeCode(line: try object(claudeSystemApiError429)),
                       .stopped(reason: "Quota exhausted"))
    }

    func testClaudeEndTurnIsDone() throws {
        XCTAssertEqual(IslandRunOutcome.claudeCode(line: try object(claudeEndTurn)), .done)
        XCTAssertNil(IslandRunOutcome.claudeCode(line: ["type": "user", "message": ["role": "user"]]))
    }

    // MARK: - OpenCode

    func testOpenCodeAbortedApiErrorAndContextOverflow() throws {
        XCTAssertEqual(IslandRunOutcome.openCode(message: try object(openCodeAborted)),
                       .stopped(reason: "Aborted"))
        XCTAssertEqual(IslandRunOutcome.openCode(message: try object(openCodeAPIError)),
                       .stopped(reason: "API error"))
        XCTAssertEqual(IslandRunOutcome.openCode(message: try object(openCodeContextOverflow)),
                       .stopped(reason: "Context overflow"))
        let quota = try object("""
        {"role":"assistant","finish":null,"error":{"name":"APIError","data":{"message":"usage limit reached"}}}
        """)
        XCTAssertEqual(IslandRunOutcome.openCode(message: quota), .stopped(reason: "Quota exhausted"))
    }

    func testOpenCodeStopIsDoneAndToolCallsIsRunning() throws {
        XCTAssertEqual(IslandRunOutcome.openCode(message: try object(openCodeStopOK)), .done)
        XCTAssertNil(IslandRunOutcome.openCode(message: ["role": "assistant", "finish": "tool-calls"]),
                     "tool calls still running")
        XCTAssertEqual(IslandRunOutcome.openCode(message: ["role": "assistant", "finish": "length"]),
                       .stopped(reason: "Context overflow"))
        XCTAssertEqual(IslandRunOutcome.openCode(message: ["role": "assistant", "finish": "error"]),
                       .stopped(reason: "API error"))
        XCTAssertEqual(IslandRunOutcome.openCode(message: ["role": "assistant", "finish": "unknown"]),
                       .stopped(reason: "Aborted"))
    }
}
