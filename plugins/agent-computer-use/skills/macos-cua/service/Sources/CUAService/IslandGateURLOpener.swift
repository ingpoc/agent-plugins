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
        guard url.scheme?.lowercased() == "https", let host = url.host, !host.isEmpty,
              url.user == nil, url.password == nil, url.port == nil else { throw IslandGateURLError.unsafeURL }
        let value = (host + url.path).lowercased()
        guard !value.contains("placeholder"), !value.contains("replace") else { throw IslandGateURLError.unsafeURL }
    }
    static func validate(_ config: IslandGateConfig) throws {
        try validate(config.x.intent_url)
        if let follow = config.x.follow_url { try validate(follow) }
        try validate(config.linkedin.share_url)
        if let follow = config.linkedin.follow_url { try validate(follow) }
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
