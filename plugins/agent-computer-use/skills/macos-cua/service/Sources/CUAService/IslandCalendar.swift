import AppKit
import EventKit
import Foundation

/// A4: read-only EventKit feed (today + tomorrow). Never writes. Access is requested only from the
/// explicit Settings button — never at launch.
@MainActor
final class IslandCalendarController {
    private let store = EKEventStore()
    private var observer: NSObjectProtocol?
    private var timer: Timer?
    var onChange: (([IslandCalendarEvent]) -> Void)?
    var excludedCalendarIDs: Set<String> = []
    var enabled = true { didSet { if enabled != oldValue { reload() } } }

    nonisolated static var status: String {
        let s = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) {
            switch s {
            case .fullAccess: return "granted"
            case .writeOnly: return "write-only"
            case .denied, .restricted: return "denied"
            case .notDetermined: return "not-determined"
            default: return "unknown"
            }
        }
        return s == .authorized ? "granted" : s == .notDetermined ? "not-determined" : "denied"
    }

    nonisolated static var authorized: Bool { status == "granted" }

    func start() {
        if observer == nil {
            observer = NotificationCenter.default.addObserver(forName: .EKEventStoreChanged, object: store, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.reload() }
            }
        }
        if timer == nil {
            let t = Timer(timeInterval: 60, repeats: true) { [weak self] _ in Task { @MainActor in self?.reload() } }
            RunLoop.main.add(t, forMode: .common)
            timer = t
        }
        reload()
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        timer?.invalidate()
        timer = nil
    }

    // Asking for access lives in PermissionBroker (onboarding / Settings Grant only).

    func calendars() -> [(id: String, title: String)] {
        guard Self.authorized else { return [] }
        return store.calendars(for: .event).map { ($0.calendarIdentifier, $0.title) }
    }

    func reload(now: Date = Date()) {
        guard enabled, Self.authorized else { onChange?([]); return }
        let cal = Calendar.current
        let start = cal.startOfDay(for: now)
        guard let end = cal.date(byAdding: .day, value: 2, to: start) else { return }
        let calendars = store.calendars(for: .event).filter { !excludedCalendarIDs.contains($0.calendarIdentifier) }
        guard !calendars.isEmpty else { onChange?([]); return }
        let pred = store.predicateForEvents(withStart: start, end: end, calendars: calendars)
        let events = store.events(matching: pred).prefix(40).map(Self.map)
        onChange?(Array(events))
    }

    nonisolated static func map(_ e: EKEvent) -> IslandCalendarEvent {
        let hex: String = {
            guard let c = e.calendar?.color?.usingColorSpace(.sRGB) else { return "" }
            return String(format: "#%02X%02X%02X", Int(c.redComponent * 255), Int(c.greenComponent * 255), Int(c.blueComponent * 255))
        }()
        let url = CalendarLogic.meetingURL(in: [e.url?.absoluteString ?? "", e.location ?? "", e.notes ?? ""])
        return IslandCalendarEvent(id: e.eventIdentifier ?? UUID().uuidString, title: e.title ?? "", start: e.startDate, end: e.endDate,
                                   location: e.location ?? "", calendar: e.calendar?.title ?? "", colorHex: hex,
                                   meetingURL: url, allDay: e.isAllDay)
    }

    /// Open Calendar.app at the event's day (read-only: we never edit).
    static func open(_ e: IslandCalendarEvent) {
        let t = Int(e.start.timeIntervalSinceReferenceDate)
        if let u = URL(string: "ical://ekevent/\(e.id)?method=show&options=more") ?? URL(string: "calshow:\(t)") {
            if !NSWorkspace.shared.open(u), let u2 = URL(string: "calshow:\(t)") { NSWorkspace.shared.open(u2) }
        }
    }
}
