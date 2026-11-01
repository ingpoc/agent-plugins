import AppKit
import Foundation

protocol IslandGateURLOpening: AnyObject {
    func open(_ url: URL)
}

enum IslandGateSafety {
    static var restricted: Bool {
        NSClassFromString("XCTestCase") != nil || ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil || ProcessInfo.processInfo.environment["ACU_GATE_NO_OPEN"] == "1"
    }
}

final class IslandGateSystemURLOpener: IslandGateURLOpening {
    static var refused: [URL] = []
    func open(_ url: URL) {
        guard !IslandGateSafety.restricted else { Self.refused.append(url); return }
        NSWorkspace.shared.open(url)
    }
}

final class IslandGateRecordingURLOpener: IslandGateURLOpening {
    private(set) var opened: [URL] = []
    func open(_ url: URL) { opened.append(url) }
}

enum IslandGateURLError: Error { case unsafeURL }

final class IslandGateValidatedURLOpener: IslandGateURLOpening {
    private let system = IslandGateSystemURLOpener()
    static func validate(_ url: URL) throws {
        guard url.scheme?.lowercased() == "https", let host = url.host?.lowercased(),
              url.user == nil, url.password == nil, url.port == nil else { throw IslandGateURLError.unsafeURL }
        let path = url.path
        if host == "x.com" {
            let profile = path.range(of: #"^/[A-Za-z0-9_]{1,15}/?$"#, options: .regularExpression) != nil
            let status = path.range(of: #"^/i/status/[0-9]+$"#, options: .regularExpression) != nil
            let repost = path == "/intent/retweet" && URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: { $0.name == "tweet_id" && $0.value?.range(of: #"^[0-9]+$"#, options: .regularExpression) != nil }) == true
            guard profile || status || repost else { throw IslandGateURLError.unsafeURL }
            return
        }
        let linkedInHost = host == "linkedin.com" || host == "www.linkedin.com" || host.range(of: #"^[a-z]{2}\.linkedin\.com$"#, options: .regularExpression) != nil
        guard linkedInHost else { throw IslandGateURLError.unsafeURL }
        let profile = path.range(of: #"^/in/[A-Za-z0-9-]{3,100}/?$"#, options: .regularExpression) != nil
        let post = path.range(of: #"^/posts/[^/]+/?$"#, options: .regularExpression) != nil ||
            path.range(of: #"^/feed/update/urn:li:(?:activity|share|ugcPost):[0-9]+/?$"#, options: .regularExpression) != nil
        guard profile || (host == "www.linkedin.com" && post) else { throw IslandGateURLError.unsafeURL }
    }
    static func validate(_ config: IslandGateConfig) throws {
        try validate(config.x.profile_url)
        if let follow = config.x.follow_url { try validate(follow) }
        if let repost = config.x.repost_url { try validate(repost) }
        if let follow = config.linkedin.follow_url { try validate(follow) }
        if let launch = config.linkedin.launch_post_url { try validate(launch) }
        if let repost = config.linkedin.repost_url { try validate(repost) }
    }
    func open(_ url: URL) {
        do { try Self.validate(url); system.open(url) }
        catch { FileHandle.standardError.write(Data("Refused unsafe gate URL\n".utf8)) }
    }
}

enum IslandGateURLOpenerDefault {
    static func make(fixtureState: IslandGateState?) -> IslandGateURLOpening {
        if IslandGateSafety.restricted || fixtureState != nil { return IslandGateRecordingURLOpener() }
        return IslandGateValidatedURLOpener()
    }
}

protocol IslandGatePasteboard: AnyObject {
    func write(_ text: String)
}

final class IslandGateSystemPasteboard: IslandGatePasteboard {
    func write(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

final class IslandGateRecordingPasteboard: IslandGatePasteboard {
    private(set) var written: [String] = []
    func write(_ text: String) { written.append(text) }
}
