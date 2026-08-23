import AppKit
import Foundation
import SwiftUI

/// Menu-bar-center island — streams gateway SSE when voice is supervised by CUAService.
@MainActor
final class IslandController {
    private var gateway: URL
    private var last: IslandSnapshot?
    private let model = IslandModel()
    private let menuBarPanel = SamanthaMenuBarIslandPanel()
    private var pendingConfirmId: String = ""
    private var streamTask: Task<Void, Never>?
    private var voiceSessionActive = false
    private var loggedListeningReady = false
    private weak var cursorOverlay: CursorOverlay?

    init(gatewayPort: Int = 8765) {
        gateway = URL(string: "http://127.0.0.1:\(gatewayPort)")!
        model.resolveConfirm = { [weak self] approved in
            Task { await self?.resolveConfirm(approved: approved) }
        }
        menuBarPanel.attach(model: model)
    }

    func updateGateway(port: Int) {
        gateway = URL(string: "http://127.0.0.1:\(port)")!
    }

    func setCursorOverlay(_ overlay: CursorOverlay) {
        cursorOverlay = overlay
    }

    func prepareNotch() {
        // Panel is created lazily on first show.
    }

    func startStreaming() {
        streamTask?.cancel()
        voiceSessionActive = true
        last = nil
        model.apply(IslandSnapshot(kind: "listening", title: "Connecting", detail: ""))
        menuBarPanel.show(on: SamanthaMenuBarIslandPanel.menuBarScreen())
        SamanthaActivityLog.startup(phase: "island_stream_begin", status: "ok", detail: "Connecting pill")
        streamTask = Task { await stream() }
    }

    func stopStreaming() {
        streamTask?.cancel()
        streamTask = nil
        voiceSessionActive = false
        loggedListeningReady = false
        hideAgentCursor()
        menuBarPanel.hide()
        last = nil
        pendingConfirmId = ""
        model.apply(IslandSnapshot(kind: "idle"))
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
                SamanthaActivityLog.startup(
                    phase: "island_stream_error",
                    status: "fail",
                    detail: String(describing: error).prefix(120).description
                )
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
        var effective = snap
        if voiceSessionActive && snap.kind == "idle" {
            effective = IslandSnapshot(kind: "listening", title: "Connecting", detail: "")
        }
        model.apply(effective)
        if effective.kind == "listening", effective.title == "Listening", !loggedListeningReady {
            loggedListeningReady = true
            SamanthaActivityLog.startup(phase: "island_listening", status: "ok")
        }
        let screen = SamanthaMenuBarIslandPanel.menuBarScreen()
        switch effective.kind {
        case "idle":
            hideAgentCursor()
            menuBarPanel.hide()
        default:
            menuBarPanel.show(on: screen)
            menuBarPanel.reposition(on: screen)
        }
    }

    /// Session end / disconnect — not between tasks while Listening.
    private func hideAgentCursor() {
        cursorOverlay?.hide()
    }
}
