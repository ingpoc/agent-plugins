import XCTest
import AppKit
@testable import CUAService

@MainActor
final class IslandGateTrialCopyTests: XCTestCase {
    func testTrialCaptionsFitTwoLinesAt440Points() {
        let available: CGFloat = 440 - 2 * 24
        let captions = [
            "From @gurusharan. Keeps your island on for good.",
            "Opens LinkedIn in your browser",
            "Usually starts within a few minutes. You can close this window.",
            "Your trial ends today. Repost to keep your island for good.",
            "Your trial ended. Repost the launch post to turn your island back on.",
            "Follow again and repost to turn your island back on.",
            "Not following @gurusharan anymore",
            "Follow to start a free 3-day trial. Repost to keep it.",
            "Follow to start your free trial."
        ]
        for caption in captions {
            let text = NSAttributedString(string: caption, attributes: [.font: NSFont.systemFont(ofSize: 13)])
            let bounds = text.boundingRect(with: CGSize(width: available, height: 1000), options: [.usesLineFragmentOrigin, .usesFontLeading])
            XCTAssertLessThanOrEqual(bounds.height, 34, caption)
        }
        for caption in ["Trial · 3 days left", "Trial · Last day", "Trial · free while we launch"] {
            let width = NSAttributedString(string: caption, attributes: [.font: NSFont.systemFont(ofSize: 12)]).size().width
            XCTAssertLessThan(width, available / 2, caption)
        }
    }

    func testTrialWindowReloadsLaunchConfigAfterClosing() async throws {
        let gate = IslandGate(store: IslandGateMemoryStore(), client: IslandGateURLProtocol.client(), urlOpener: IslandGateRecordingURLOpener(), pasteboard: IslandGateRecordingPasteboard())
        let account = IslandGateAccount(id: "trial-account", provider: "x", handle: "friend")
        gate.transition(.trial(account: account, ends: Date().addingTimeInterval(86400)))
        var launch = "123"
        IslandGateURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/config")
            let body = """
            {"x":{"handle":"guru","profile_url":"https://x.com/guru","intent_url":"https://x.com/intent/post?text=legacy","preset_text":"legacy","launch_post_id":"\(launch)","repost_url":"https://x.com/intent/retweet?tweet_id=\(launch)"},"linkedin":{"share_url":"https://www.linkedin.com/feed/","preset_text":"legacy","follow_url":"https://www.linkedin.com/in/example/"}}
            """
            return (200, Data(body.utf8))
        }
        defer { gate.closed(); gate.stopPollingForTests(); IslandGateURLProtocol.handler = nil }
        let first = expectation(description: "trial fetches config")
        gate.onChange.append { if gate.config?.x.launch_post_id == "123" { first.fulfill() } }
        gate.opened()
        await fulfillment(of: [first], timeout: 2)
        gate.closed()
        launch = "456"
        let second = expectation(description: "reopened trial fetches updated launch")
        gate.onChange.append { if gate.config?.x.launch_post_id == "456" { second.fulfill() } }
        gate.opened()
        await fulfillment(of: [second], timeout: 2)
        XCTAssertEqual(gate.config?.x.launch_post_id, "456")
    }
}
