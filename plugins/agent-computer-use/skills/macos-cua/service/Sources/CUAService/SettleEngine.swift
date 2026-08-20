import AppKit
import Foundation
import Logging

/// Notification-driven UI settle engine.
/// Watches AXValueChanged / AXLayoutChanged / AXUIElementDestroyed
/// and returns once the UI is quiescent (no notification for `minQuiet`)
/// or `timeout` is reached.
final class SettleEngine: @unchecked Sendable {
    private let logger: Logger

    init(logger: Logger) { self.logger = logger }

    func waitForQuiescence(
        app: AXUIElement,
        pid: pid_t,
        timeout: TimeInterval = 5.0,
        minQuiet: TimeInterval = 0.3
    ) async -> SettleResult {
        let startTime = ProcessInfo.processInfo.systemUptime
        let notifications: [String] = [
            kAXValueChangedNotification,
            kAXLayoutChangedNotification,
            kAXUIElementDestroyedNotification,
            kAXFocusedUIElementChangedNotification,
            kAXWindowCreatedNotification,
        ]

        let stream = AXNotificationStream(
            element: app,
            pid: pid,
            notifications: notifications
        )

        var lastActivity = ProcessInfo.processInfo.systemUptime
        var notificationCount = 0

        // Poll loop with short intervals — AXObserver notifications
        // drive activity tracking through the stream
        while true {
            let now = ProcessInfo.processInfo.systemUptime
            if now - startTime >= timeout {
                return SettleResult(
                    settled: false,
                    reason: "timeout",
                    elapsed: now - startTime,
                    notifications: notificationCount
                )
            }

            // Drain any pending notifications
            while let _ = stream.poll() {
                lastActivity = ProcessInfo.processInfo.systemUptime
                notificationCount += 1
            }

            if now - lastActivity >= minQuiet {
                return SettleResult(
                    settled: true,
                    reason: "quiescent",
                    elapsed: now - startTime,
                    notifications: notificationCount
                )
            }

            try? await Task.sleep(nanoseconds: 50_000_000) // 50ms
        }
    }
}

struct SettleResult: Sendable {
    let settled: Bool
    let reason: String
    let elapsed: TimeInterval
    let notifications: Int

    var dict: [String: Any] {
        [
            "settled": settled,
            "reason": reason,
            "elapsed_ms": Int(elapsed * 1000),
            "notifications": notifications,
        ]
    }
}

/// Wraps AXObserver to deliver notifications as a pollable stream.
private final class AXNotificationStream: @unchecked Sendable {
    private var observer: AXObserver?
    private var pending: [String] = []
    private let lock = NSLock()

    init(element: AXUIElement, pid: pid_t, notifications: [String]) {
        var obs: AXObserver?
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        let callback: AXObserverCallback = { _, _, notification, refcon in
            guard let refcon else { return }
            let stream = Unmanaged<AXNotificationStream>.fromOpaque(refcon)
                .takeUnretainedValue()
            let name = notification as String
            stream.lock.lock()
            stream.pending.append(name)
            stream.lock.unlock()
        }

        AXObserverCreate(pid, callback, &obs)
        if let obs {
            for name in notifications {
                AXObserverAddNotification(obs, element, name as CFString, selfPtr)
            }
            CFRunLoopAddSource(
                CFRunLoopGetMain(),
                AXObserverGetRunLoopSource(obs),
                .defaultMode
            )
            self.observer = obs
        }
    }

    deinit {
        if let observer {
            CFRunLoopRemoveSource(
                CFRunLoopGetMain(),
                AXObserverGetRunLoopSource(observer),
                .defaultMode
            )
        }
    }

    func poll() -> String? {
        lock.lock()
        defer { lock.unlock() }
        return pending.isEmpty ? nil : pending.removeFirst()
    }
}
