import AppKit
import Foundation

enum IslandGateDeepLink {
    enum Route: Equatable {
        case unlock
    }

    static func route(_ url: URL) -> Route? {
        guard url.scheme?.lowercased() == "acu-island", url.host?.lowercased() == "unlock" else { return nil }
        let path = url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard path.isEmpty else { return nil }
        return .unlock
    }

    @MainActor
    static func handle(_ url: URL, gate: IslandGate = .shared) {
        guard route(url) == .unlock else { return }
        gate.openFromDeepLink()
    }
}

@MainActor
final class IslandGateURLHandler: NSObject {
    private weak var gate: IslandGate?

    init(gate: IslandGate) {
        self.gate = gate
        super.init()
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURL(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    @objc private func handleGetURL(_ event: NSAppleEventDescriptor, withReplyEvent reply: NSAppleEventDescriptor) {
        guard let raw = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              let url = URL(string: raw) else { return }
        Task { @MainActor in
            guard let gate else { return }
            IslandGateDeepLink.handle(url, gate: gate)
        }
    }
}
