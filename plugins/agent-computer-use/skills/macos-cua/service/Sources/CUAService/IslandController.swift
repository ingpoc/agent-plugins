import AppKit
import DynamicNotchKit
import Foundation
import SwiftUI

struct IslandSnapshot: Codable, Equatable {
    var kind: String = "idle"
    var title: String = ""
    var detail: String = ""
    var app: String = ""
    var step: String = ""
    var confirm_id: String = ""
    var confirm_prompt: String = ""
}

/// Notch / Dynamic Island UI — streams gateway SSE when voice is supervised by CUAService.
@MainActor
final class IslandController {
    private var gateway: URL
    private var last: IslandSnapshot?
    private var notch: DynamicNotchInfo?
    private var pendingConfirmId: String = ""
    private var streamTask: Task<Void, Never>?

    init(gatewayPort: Int = 8765) {
        gateway = URL(string: "http://127.0.0.1:\(gatewayPort)")!
    }

    func updateGateway(port: Int) {
        gateway = URL(string: "http://127.0.0.1:\(port)")!
    }

    func prepareNotch() {
        guard notch == nil else { return }
        let notch = DynamicNotchInfo(
            icon: .init(systemName: "waveform", color: .cyan),
            title: "Voice CUA",
            description: "Voice off",
            compactLeading: .init(systemName: "waveform", color: .secondary),
            style: .auto
        )
        self.notch = notch
    }

    func startStreaming() {
        streamTask?.cancel()
        prepareNotch()
        streamTask = Task { await stream() }
    }

    func stopStreaming() {
        streamTask?.cancel()
        streamTask = nil
        Task { await notch?.hide() }
        last = nil
        pendingConfirmId = ""
    }

    func stream() async {
        while !Task.isCancelled {
            guard let url = URL(string: "/api/island/stream", relativeTo: gateway) else { return }
            do {
                let (bytes, _) = try await URLSession.shared.bytes(from: url)
                var buffer = ""
                for try await chunk in bytes {
                    if Task.isCancelled { return }
                    buffer.append(String(decoding: [chunk], as: UTF8.self))
                    while let range = buffer.range(of: "\n\n") {
                        let block = String(buffer[..<range.lowerBound])
                        buffer.removeSubrange(..<range.upperBound)
                        guard block.hasPrefix("data: ") else { continue }
                        let json = String(block.dropFirst(6))
                        guard let data = json.data(using: .utf8) else { continue }
                        let snap = try JSONDecoder().decode(IslandSnapshot.self, from: data)
                        if snap != last {
                            last = snap
                            pendingConfirmId = snap.confirm_id
                            await apply(snap)
                        }
                    }
                }
            } catch {
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
    }

    func resolveConfirm(approved: Bool) async {
        guard !pendingConfirmId.isEmpty else { return }
        guard let url = URL(string: "/api/confirm", relativeTo: gateway) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Any] = ["confirm_id": pendingConfirmId, "approved": approved]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: req)
    }

    func postListening() async {
        guard let url = URL(string: "/api/session/set-listening", relativeTo: gateway) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: req)
    }

    private func apply(_ snap: IslandSnapshot) async {
        guard let notch else { return }
        let iconName: String
        let color: Color
        switch snap.kind {
        case "speaking":
            iconName = "speaker.wave.2.fill"; color = .blue
        case "acting":
            iconName = "hand.point.up.left.fill"; color = .cyan
        case "listening":
            iconName = "mic.fill"; color = .green
        case "thinking":
            iconName = "brain"; color = .orange
        case "driving":
            iconName = "hand.point.up.left.fill"; color = .cyan
        case "confirm":
            iconName = "exclamationmark.triangle.fill"; color = .yellow
        case "secrets":
            iconName = "key.fill"; color = .purple
        case "done":
            iconName = "checkmark.circle.fill"; color = .green
        case "error":
            iconName = "xmark.octagon.fill"; color = .red
        default:
            iconName = "waveform"; color = .secondary
        }
        let title: String
        if !snap.title.isEmpty {
            title = snap.title
        } else if snap.kind == "driving" {
            title = snap.app.isEmpty ? "Driving" : snap.app
        } else {
            title = snap.kind.capitalized
        }
        let detail: String
        if snap.kind == "driving" {
            detail = [snap.app, snap.step].filter { !$0.isEmpty }.joined(separator: " · ")
        } else if snap.kind == "confirm" {
            detail = snap.confirm_prompt.isEmpty ? snap.detail : snap.confirm_prompt
        } else {
            detail = snap.detail
        }
        notch.icon = .init(systemName: iconName, color: color)
        notch.compactLeading = .init(systemName: iconName, color: color)
        notch.title = LocalizedStringKey(title)
        notch.description = detail.isEmpty ? nil : LocalizedStringKey(detail)

        switch snap.kind {
        case "idle":
            await notch.hide()
        case "confirm", "secrets", "error":
            await notch.expand()
        default:
            await notch.compact()
        }
    }
}
