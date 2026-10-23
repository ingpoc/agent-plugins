import AppKit
import QuickLookThumbnailing
import SwiftUI

// Views for phases A2–A9 + the agent list (polish bar: SF Pro 15/13/11, monospacedDigit, white ladder
// 100/70/45/15/8, continuous radii, nested radius = outer − padding, ≥ 28 / 44 pt targets).

enum IslandType {
    static let title = Font.system(size: 15, weight: .semibold)
    static let body = Font.system(size: 13)
    static let bodyStrong = Font.system(size: 13, weight: .semibold)
    static let meta = Font.system(size: 11, weight: .medium).monospacedDigit()
    static let value = Font.system(size: 28, weight: .semibold).monospacedDigit()
}

// MARK: - A2 HUD

/// Leading HUD glyph: SF Symbol with variableValue (speaker waves / sun rays fill with the level).
struct IslandHUDGlyph: View {
    let state: IslandHUDState
    var size: CGFloat = 16
    var body: some View {
        Image(systemName: state.symbol, variableValue: state.kind == .volume ? state.fraction : max(0.05, state.fraction))
            .font(.system(size: size * 0.82, weight: .semibold))
            .foregroundStyle(Color.white)
            .symbolRenderingMode(.hierarchical)
            .frame(width: size, height: size)
            .accessibilityLabel(state.spoken)
    }
}

/// 80×4 level bar, white on white 15 %. Only the fill animates (.smooth 0.15 by default).
struct IslandHUDBar: View {
    let fraction: Double
    var width: CGFloat = IslandBarLayout.hudBarWidth
    var animation: Animation? = .smooth(duration: 0.15)
    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(IslandPalette.track)
            Capsule().fill(Color.white)
                .frame(width: fraction <= 0 ? 0 : max(4, width * CGFloat(min(1, fraction))))
        }
        .frame(width: width, height: 4)
        .animation(animation, value: fraction)
        .accessibilityHidden(true)
    }
}

// MARK: - A3 Quick Peek (content width, capped at 244 pt)

struct IslandPeekCompact: View {
    let item: IslandPeekItem
    var body: some View {
        // Ticks on the peek's own minute boundaries: the time appears exactly at 1 minute.
        TimelineView(.periodic(from: item.at, by: 60)) { ctx in
            HStack(spacing: 8) {
                // 16 pt in the 8 pt lead slot, like every compact lead (gate: ≥ 8 pt inset).
                IslandPeekIcon(item: item, size: 16).layoutProbe("peek-icon", .glyph)
                Text(item.title)
                    .font(IslandType.bodyStrong)
                    .foregroundStyle(Color.white)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .layoutProbe("peek-title", truncates: true)
                if item.count > 1 {
                    // 16 pt disc at white 15 %; 11 semibold monospaced numeral (capsule past one digit).
                    Text("\(item.count)")
                        .font(.system(size: 11, weight: .semibold).monospacedDigit())
                        .foregroundStyle(Color.white)
                        .padding(.horizontal, item.count > 9 ? 4 : 0)
                        .frame(minWidth: 16, minHeight: 16)
                        .background(Capsule().fill(Color.white.opacity(0.15)))
                        .contentTransition(.numericText())
                        .layoutProbe("peek-badge", .glyph)
                }
                // No "now": the time appears only once the peek is at least a minute old.
                if let age = IslandPeekRules.ageLabel(item.at, now: ctx.date) {
                    Text(age)
                        .font(IslandType.meta)
                        .foregroundStyle(Color.white.opacity(0.45))
                        .layoutProbe("peek-age")
                }
            }
            .padding(.leading, IslandBarLayout.slotInset)
            .padding(.trailing, 10)
            .frame(height: IslandBarLayout.compactHeight)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(item.spoken)
    }
}

struct IslandPeekIcon: View {
    let item: IslandPeekItem
    var size: CGFloat = 20
    var body: some View {
        if !item.app.isEmpty {
            SamanthaAppIcon(appName: item.app, size: size, radius: size * 0.25)
        } else {
            Image(systemName: item.symbol.isEmpty ? "bell.fill" : item.symbol)
                .font(.system(size: size * 0.62, weight: .semibold))
                .foregroundStyle(IslandPeekRules.isSuccess(item.symbol) ? IslandPalette.green : Color.white)
                .frame(width: size, height: size)
                .background(Circle().fill(Color.white.opacity(0.15)))
        }
    }
}

// MARK: - A4 Calendar

/// Calendar glyph with the 2 pt countdown ring (accent; red in the last 5 minutes). Drawn relative to `size`.
struct IslandCalendarGlyph: View {
    let event: IslandCalendarEvent?
    var size: CGFloat = 16
    var lead: TimeInterval = 600
    var body: some View {
        TimelineView(.periodic(from: .now, by: 5)) { ctx in
            let now = ctx.date
            let f = event.map { CalendarLogic.ringFraction($0, now: now, lead: lead) } ?? 0
            let urgent = event.map { CalendarLogic.isUrgent($0, now: now) } ?? false
            let tint = urgent ? IslandPalette.red : IslandPalette.accent
            let line = size / 8
            ZStack {
                Circle().stroke(IslandPalette.track, lineWidth: line)
                Circle().trim(from: 0, to: max(0.001, f))
                    .stroke(tint, style: StrokeStyle(lineWidth: line, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                    .animation(.smooth(duration: 0.35), value: f)
                Image(systemName: "calendar")
                    .font(.system(size: size * 0.42, weight: .semibold))
                    .foregroundStyle(Color.white)
            }
            .padding(line / 2)
            .frame(width: size, height: size)
        }
        .accessibilityLabel(event.map { "\($0.title), \(CalendarLogic.countdown($0, now: Date()))" } ?? "Calendar")
    }
}

struct IslandCalendarDot: View {
    let event: IslandCalendarEvent?
    var useCalendarColor = true
    var countdown = false          // compact trail only
    var showDot = true             // Duo: no trailing dot
    var body: some View {
        let c = useCalendarColor ? event.flatMap { CalendarLogic.color(hex: $0.colorHex) }.map(Color.init(nsColor:)) : nil
        HStack(spacing: 5) {
            if countdown, let e = event {
                TimelineView(.periodic(from: .now, by: 15)) { ctx in
                    Text(CalendarLogic.shortCountdown(e, now: ctx.date))
                        .font(.system(size: 11, weight: .semibold).monospacedDigit())
                        .foregroundStyle(Color.white.opacity(0.7))
                        .contentTransition(.numericText())
                }
            }
            if showDot { Circle().fill(c ?? IslandPalette.accent).frame(width: 6, height: 6) }
        }
        .fixedSize()
        .frame(height: 16)
        .accessibilityHidden(true)
    }
}

/// 340×176 card: start time (28 mono, fitted) + date | dot · title, place, countdown | Join (meeting links only) + next rows.
struct CalendarCardBody: View {
    @ObservedObject var model: IslandModel
    var body: some View {
        TimelineView(.periodic(from: .now, by: 15)) { ctx in
            let now = ctx.date
            let agenda = model.calendarAgenda(now: now)
            let p = IslandBarLayout.cardPadding
            ZStack(alignment: .topLeading) {
                if let e = agenda.first {
                    // Time column sized to fit (never truncated); event column takes the rest, ≥ 12 pt away.
                    HStack(alignment: .top, spacing: Self.columnGap) {
                        VStack(alignment: .leading, spacing: 2) {
                            Self.timeText(e.start)
                                .foregroundStyle(Color.white)
                                .contentTransition(.numericText())
                                .lineLimit(1)
                            Text(Self.day.string(from: e.start))
                                .font(IslandType.meta)
                                .foregroundStyle(Color.white.opacity(0.45))
                        }
                        .fixedSize()
                        .layoutProbe("cal-time-column")
                        // Dot inline before the title (6 pt); room and relative lines align to the title's text edge.
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            IslandCalendarDot(event: e, useCalendarColor: model.useCalendarColor)
                                .frame(width: 6, height: 6)
                                .layoutProbe("cal-dot", .glyph)
                                .alignmentGuide(.firstTextBaseline) { _ in 7.5 }   // dot centred on the x-height
                            VStack(alignment: .leading, spacing: 2) {
                                Text(e.title.isEmpty ? "Event" : e.title)
                                    .font(IslandType.bodyStrong).foregroundStyle(Color.white)
                                    .lineLimit(3).truncationMode(.tail)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .layoutProbe("cal-title", truncates: true)
                                if !e.location.isEmpty || !e.calendar.isEmpty {
                                    Text(e.location.isEmpty ? e.calendar : e.location)
                                        .font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.6)).lineLimit(2)
                                        .layoutProbe("cal-room", truncates: true)
                                }
                                Text(CalendarLogic.countdown(e, now: now))
                                    .font(.system(size: 11).monospacedDigit())
                                    .foregroundStyle(CalendarLogic.isUrgent(e, now: now) ? IslandPalette.red : Color.white.opacity(0.45))
                                    .contentTransition(.numericText())
                                    .layoutProbe("cal-relative")
                            }
                        }
                        .padding(.top, 6)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                    }
                    .frame(width: IslandBarLayout.cardSize.width - 2 * p, alignment: .topLeading)
                    .offset(x: p, y: p - 4)
                    HStack(spacing: 8) {
                        Button("View event") { model.onOpenEvent?(e, false) }
                            .font(IslandType.bodyStrong).foregroundStyle(Color.white)
                            .buttonStyle(.plain)
                            .frame(height: 44).contentShape(Rectangle())
                            .layoutProbe("cal-details", .control)
                            .accessibilityLabel("View event: \(e.title)")
                        Spacer(minLength: 0)
                        if let url = e.meetingURL {
                            Button { model.onOpenEvent?(e, true); _ = url } label: {
                                Text("Join").font(IslandType.bodyStrong).foregroundStyle(Color.black)
                                    .frame(width: 88, height: 32)
                                    .layoutProbe("cal-join", .control)
                                    .background(Capsule().fill(Color.white))
                                    .frame(height: 44).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Join \(e.title)")
                        }
                    }
                    .frame(width: IslandBarLayout.cardSize.width - 2 * p, height: 44)
                    .offset(x: p, y: IslandBarLayout.cardSize.height - p - 38)
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("No more events today").font(IslandType.title).foregroundStyle(Color.white)
                        Text(Self.day.string(from: now)).font(IslandType.body).foregroundStyle(Color.white.opacity(0.7))
                    }
                    .offset(x: p, y: p)
                }
            }
            .frame(width: IslandBarLayout.cardSize.width, height: IslandBarLayout.cardSize.height, alignment: .topLeading)
            .contentShape(Rectangle())
            .onTapGesture { if let e = agenda.first { model.onOpenEvent?(e, false) } }
        }
    }

    static let columnGap: CGFloat = 12

    /// "10:29" in 28 pt semibold + thin space + "AM" in 15 medium at 70 % (period first where the locale puts it first).
    static func timeText(_ d: Date) -> Text {
        let (t, period, leading) = timeParts(d)
        let big = Text(t).font(IslandType.value)
        guard let period else { return big }
        let small = Text(period).font(.system(size: 15, weight: .medium)).foregroundColor(Color.white.opacity(0.7))
        return leading ? small + Text("\u{2009}") + big : big + Text("\u{2009}").font(IslandType.value) + small
    }

    static func timeParts(_ d: Date) -> (String, String?, Bool) {
        let s = time.string(from: d)
        let trim = CharacterSet.whitespaces.union(CharacterSet(charactersIn: "\u{202F}\u{00A0}\u{2009}"))
        for sym in [time.amSymbol, time.pmSymbol].compactMap({ $0 }) where !sym.isEmpty {
            if let r = s.range(of: sym) {
                let rest = s.replacingCharacters(in: r, with: "").trimmingCharacters(in: trim)
                return (rest, sym, r.lowerBound == s.startIndex)
            }
        }
        return (s, nil, false)
    }

    static let time: DateFormatter = {
        let f = DateFormatter()
        f.locale = .current
        f.setLocalizedDateFormatFromTemplate("jmm")
        return f
    }()

    static let day: DateFormatter = {
        let f = DateFormatter()
        f.locale = .current
        f.setLocalizedDateFormatFromTemplate("EEEdMMM")
        return f
    }()
}

// MARK: - A9 File tray

/// QuickLook thumbnail (icon fallback), cached per path.
@MainActor
final class IslandThumbCache: ObservableObject {
    static let shared = IslandThumbCache()
    @Published private(set) var images: [String: NSImage] = [:]
    @Published private(set) var failed = Set<String>()
    private var inflight = Set<String>()

    /// The QuickLook thumbnail; the system file icon ONLY when QuickLook failed; nil while loading.
    func image(for item: IslandTrayItem, side: CGFloat) -> NSImage? {
        if let i = images[item.path] { return i }
        if failed.contains(item.path) { return NSWorkspace.shared.icon(forFile: item.path) }
        load(item, side: side)
        return nil
    }

    private func load(_ item: IslandTrayItem, side: CGFloat) {
        guard !inflight.contains(item.path) else { return }
        inflight.insert(item.path)
        let req = QLThumbnailGenerator.Request(fileAt: item.url, size: CGSize(width: side, height: side), scale: 2,
                                               representationTypes: .thumbnail)
        let path = item.path
        QLThumbnailGenerator.shared.generateBestRepresentation(for: req) { rep, _ in
            // Mostly white / empty thumbnails (text pages, blank sheets) fall back to the system icon.
            let img = rep.flatMap { IslandTrayRules.isMostlyBlank($0.cgImage) ? nil : $0.nsImage }
            Task { @MainActor in
                IslandThumbCache.shared.inflight.remove(path)
                if let img { IslandThumbCache.shared.images[path] = img } else { IslandThumbCache.shared.failed.insert(path) }
            }
        }
    }
}

struct IslandTrayThumb: View {
    let item: IslandTrayItem
    var side: CGFloat = 48
    @ObservedObject private var cache = IslandThumbCache.shared
    var body: some View {
        // 48 pt white 8 % tile (radius 10, no border); the thumbnail or icon aspect-fits inside 4 pt padding.
        // While QuickLook is generating, the tile shows alone.
        ZStack {
            RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.white.opacity(0.08))
            if let img = cache.image(for: item, side: side) {
                Image(nsImage: img).resizable().interpolation(.high).aspectRatio(contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    .padding(IslandTrayRules.tileInset)
            }
        }
        .frame(width: side, height: side)
    }
}

struct TrayCardBody: View {
    @ObservedObject var model: IslandModel
    var onShare: (([URL]) -> Void)?
    var body: some View {
        let p = IslandBarLayout.cardPadding
        let vis = IslandTrayRules.visible(model.tray)
        ZStack(alignment: .topLeading) {
            HStack(spacing: 4) {
                Image(systemName: "tray.full.fill").font(.system(size: 11, weight: .semibold))
                Text("\(model.tray.count)").font(IslandType.meta).contentTransition(.numericText())
            }
            .layoutProbe("tray-header")
            .foregroundStyle(Color.white.opacity(0.45))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("File tray, \(model.tray.count) items")
            .offset(x: p, y: p)
            // Top-aligned: the 48 pt "+N" tile shares the thumbnails' centre line (names hang below).
            HStack(alignment: .top, spacing: 8) {
                ForEach(vis.shown) { item in
                    VStack(spacing: 4) {
                        IslandTrayThumb(item: item).layoutProbe("tray-thumb-\(item.id)", .glyph)
                        Text(item.name).font(.system(size: 11)).foregroundStyle(Color.white.opacity(0.7))
                            .lineLimit(1).truncationMode(.middle)
                            .layoutProbe("tray-name-\(item.id)", truncates: true)
                            .frame(width: 56, height: IslandTrayRules.nameHeight)
                    }
                    .frame(width: 56)
                    .contentShape(Rectangle())
                    .onTapGesture { NSWorkspace.shared.open(item.url) }
                    .onDrag { NSItemProvider(contentsOf: item.url) ?? NSItemProvider() }
                    .contextMenu {
                        Button("Open") { NSWorkspace.shared.open(item.url) }
                        Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([item.url]) }
                        Button("AirDrop…") { NSSharingService(named: .sendViaAirDrop)?.perform(withItems: [item.url]) }
                        Button("Share…") { onShare?([item.url]) }
                        Divider()
                        Button("Remove from Tray") { model.removeFromTray(item.id) }
                    }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(item.name)
                    .accessibilityAddTraits(.isButton)
                }
                if vis.extra > 0 {
                    Text("+\(vis.extra)").font(IslandType.bodyStrong).foregroundStyle(Color.white.opacity(0.7))
                        .frame(width: 48, height: 48)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.white.opacity(0.08)))
                        .layoutProbe("tray-more", .glyph)
                        .frame(width: 56)
                }
            }
            .offset(x: p, y: IslandTrayRules.thumbTop)
            HStack(spacing: 4) {
                Spacer(minLength: 0)
                Button { onShare?(model.tray.map(\.url)) } label: {
                    Image(systemName: "square.and.arrow.up").font(.system(size: 15, weight: .semibold)).foregroundStyle(Color.white)
                        .layoutProbe("tray-share", .glyph)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain).accessibilityLabel("Share all")
                Button { model.clearTray() } label: {
                    Text("Clear").font(IslandType.bodyStrong).foregroundStyle(Color.white.opacity(0.7))
                        .layoutProbe("tray-clear")
                        .frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain).accessibilityLabel("Clear tray")
            }
            .frame(width: IslandBarLayout.cardSize.width - 2 * p, height: IslandTrayRules.buttonRow)
            .offset(x: p, y: IslandTrayRules.buttonTop)   // 12 pt below the thumbnail row
        }
        .frame(width: IslandBarLayout.cardSize.width, height: IslandTrayRules.cardHeight, alignment: .topLeading)
    }
}

/// Drop target content: tray glyph only (the shape draws the 1 pt accent stroke). No dashes, no text.
struct IslandDropTarget: View {
    var body: some View {
        Image(systemName: "tray.and.arrow.down.fill")
            .font(.system(size: 24, weight: .semibold))
            .foregroundStyle(IslandPalette.accent)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityLabel("Drop files to add them to the tray")
    }
}

// MARK: - A5 Duo secondary pill (74×24)

struct IslandSecondaryPill: View {
    @ObservedObject var model: IslandModel
    let activity: IslandLiveActivity
    var reduceMotion = false
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: IslandBarLayout.compactRadius, style: .continuous).fill(Color.black)
            HStack(spacing: 0) {
                lead.frame(width: 16, height: 16).layoutProbe("duo-lead", .glyph, in: "secondary")
                if activity != .calendar {
                    Spacer(minLength: 0)
                    trail.layoutProbe("duo-trail", .glyph, in: "secondary").frame(width: 20, height: 16)
                }
            }
            .padding(.horizontal, IslandBarLayout.slotInset)
        }
        .frame(width: model.secondaryWidth, height: IslandBarLayout.compactHeight)
        .contentShape(RoundedRectangle(cornerRadius: IslandBarLayout.compactRadius, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityHint("Swaps with the main activity")
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder private var lead: some View {
        switch activity {
        case .media: NowPlayingArtwork(info: model.nowPlaying, size: 16, flip: false)
        case .calendar: IslandCalendarGlyph(event: model.calendarNext, size: 16, lead: model.calendarLead)
        case .tray: Image(systemName: "tray.full.fill").font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.white)
        }
    }

    @ViewBuilder private var trail: some View {
        switch activity {
        case .media:
            NowPlayingWaveform(playing: model.nowPlaying?.playing ?? false, color: NowPlayingTint.wave(model), reduceMotion: reduceMotion)
        case .calendar: Color.clear   // Duo: the ring glyph carries the countdown; no trailing dot
        case .tray:
            Text("\(model.tray.count)").font(.system(size: 11, weight: .semibold).monospacedDigit()).foregroundStyle(Color.white.opacity(0.7))
        }
    }

    private var label: String {
        switch activity {
        case .media: return "Now playing: " + [model.nowPlaying?.title ?? "", model.nowPlaying?.artist ?? ""].filter { !$0.isEmpty }.joined(separator: ", ")
        case .calendar: return model.calendarNext.map { "Next event: \($0.title), \(CalendarLogic.countdown($0, now: Date()))" } ?? "Calendar"
        case .tray: return "File tray, \(model.tray.count) items"
        }
    }
}

// MARK: - 7 Agent list

struct IslandAgentListBody: View {
    @ObservedObject var model: IslandModel
    var reduceMotion = false
    var body: some View {
        let p = IslandBarLayout.cardPadding
        let all = model.listRows
        let rows = Array(all.prefix(IslandAgentRows.maxRows))
        let extra = all.count - rows.count
        let h = model.cardHeight
        VStack(alignment: .leading, spacing: 0) {
            // ≤ 4 fixed-height rows always fit: no scroll view (crisper, renders offscreen).
            // One 1 s timeline for the whole list, only while it is on screen AND a row is running.
            Group {
                let running = rows.filter { $0.status == "running" }.map(\.elapsed_s)
                if !running.isEmpty {
                    // Per second for the first minute, then per minute ("running 4m").
                    TimelineView(IslandAgentTicker(start: model.agentsAt, bases: running)) { ctx in
                        rowStack(rows, extra: ctx.date.timeIntervalSince(model.agentsAt))
                    }
                } else {
                    rowStack(rows, extra: 0)
                }
            }
            .frame(height: h - 2 * p - (extra > 0 ? 18 : 0), alignment: .top)
            if extra > 0 {
                Text("+\(extra) in app").font(IslandType.meta).foregroundStyle(Color.white.opacity(0.45))
                    .layoutProbe("agents-footer")
                    .frame(height: 18, alignment: .bottom)
            }
        }
        .padding(p)
        .frame(width: IslandBarLayout.cardSize.width, height: h, alignment: .topLeading)
    }

    private func rowStack(_ rows: [IslandAgent], extra: TimeInterval) -> some View {
        VStack(spacing: 0) {
            ForEach(rows, id: \.id) { a in row(a, extra: extra) }
        }
    }

    private func row(_ a: IslandAgent, extra: TimeInterval) -> some View {
        let pending = model.rowControls[a.id]
        let flashing = model.rowFlash.contains(a.id)
        return HStack(spacing: 10) {
            // 20 pt icon on a 28 pt tile (radius 7, white 8 %) on every row.
            SamanthaAppIcon(appName: a.app.isEmpty ? a.name : a.app, size: 20, radius: 5)
                .frame(width: 28, height: 28)
                .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Color.white.opacity(0.08)))
                .layoutProbe("row-\(a.id)-icon", .glyph)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(a.name.isEmpty ? a.app : a.name).font(IslandType.bodyStrong).foregroundStyle(Color.white).lineLimit(1)
                        .layoutProbe("row-\(a.id)-name", truncates: true)
                        .layoutPriority(1)
                    if !a.title.isEmpty {
                        Text(a.title).font(IslandType.body).foregroundStyle(Color.white.opacity(0.7)).lineLimit(1)
                            .layoutProbe("row-\(a.id)-title", truncates: true)
                    }
                }
                let status = pending.map { Self.pendingText($0.action) } ?? IslandAgentRows.statusText(a, elapsedExtra: extra)
                Text(status)
                    .font(IslandType.meta)
                    .foregroundStyle(a.status == "error" ? IslandPalette.red.opacity(0.9) : Color.white.opacity(0.45))
                    .contentTransition(reduceMotion ? .identity : .numericText())
                    .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: status)
                    .layoutProbe("row-\(a.id)-status")
            }
            Spacer(minLength: 4)
            control(a, pending: pending)
        }
        .frame(height: IslandAgentRows.rowHeight)
        .padding(.horizontal, 6)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill(flashing ? IslandPalette.red.opacity(0.25) : Color.clear))
        .animation(reduceMotion ? nil : .smooth(duration: 0.2), value: flashing)
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private func control(_ a: IslandAgent, pending: IslandRowControl?) -> some View {
        if pending != nil {
            SamanthaProgressRing(fraction: nil, reduceMotion: reduceMotion).frame(width: 28, height: 28)
                .accessibilityLabel("Sending")
        } else if let action = IslandAgentRows.control(for: a) {
            let armed = action == .stop && model.isStopArmed(a.id)
            Button { model.tapRowControl(a) } label: {
                Group {
                    if armed { Text("Confirm stop") } else { Image(systemName: Self.symbol(action, armed: false)) }
                }
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(action == .approve ? Color.black : Color.white)
                    .frame(width: armed ? 92 : 28, height: 28)
                    .background(Capsule().fill(armed ? IslandPalette.destructiveFill : action == .approve ? Color.white : Color.white.opacity(0.12)))
                    .layoutProbe("row-\(a.id)-control", .control)
                    .islandFocusRing(model.isNavFocused(.row(a.id)), shape: .capsule)
                    .frame(width: armed ? 100 : 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .animation(reduceMotion ? nil : .smooth(duration: 0.2), value: armed)
            .accessibilityLabel(Self.label(action, armed: armed, name: a.name))
        } else {
            statusGlyph(a).frame(width: 28, height: 28).layoutProbe("row-\(a.id)-glyph", .glyph)
        }
    }

    @ViewBuilder
    private func statusGlyph(_ a: IslandAgent) -> some View {
        if a.status == "expired" {
            IslandHollowRing(size: 12)
        } else if a.status == "error" {
            SamanthaSymbol(name: "xmark.circle.fill", color: IslandPalette.red)
        } else if a.status == "done" {
            SamanthaSymbol(name: "checkmark.circle.fill", color: IslandPalette.green)
        } else if a.needs_you || a.status == "needs_you" {
            SamanthaSymbol(name: "hand.raised.fill", color: IslandPalette.amber)
        } else {
            SamanthaProgressRing(fraction: nil, reduceMotion: reduceMotion).frame(width: 16, height: 16)
        }
    }

    static func symbol(_ a: IslandRowControl.Action, armed: Bool) -> String {
        switch a {
        case .stop: return "stop.fill"
        case .retry: return "arrow.clockwise"
        case .approve: return "checkmark"
        }
    }

    static func label(_ a: IslandRowControl.Action, armed: Bool, name: String) -> String {
        switch a {
        case .stop: return armed ? "Confirm stop \(name)" : "Stop \(name)"
        case .retry: return "Retry \(name)"
        case .approve: return "Approve \(name)"
        }
    }

    static func pendingText(_ a: IslandRowControl.Action) -> String {
        switch a {
        case .stop: return "stopping…"
        case .retry: return "retrying…"
        case .approve: return "approving…"
        }
    }
}

/// One schedule for the whole agent list: the next instant any running row's label changes —
/// every second while a row is under a minute, then on each row's minute (hour) boundary.
struct IslandAgentTicker: TimelineSchedule {
    let start: Date
    let bases: [Double]    // elapsed_s of each running row at `start`

    func entries(from date: Date, mode: TimelineScheduleMode) -> AnySequence<Date> {
        AnySequence(sequence(first: date) { Self.next(after: $0, start: start, bases: bases) })
    }

    static func next(after d: Date, start: Date, bases: [Double]) -> Date {
        let dt = max(0, d.timeIntervalSince(start))
        var wait = Double.infinity
        for b in bases {
            let e = b + dt
            let step: Double = e < 60 ? 1 : e < 3600 ? 60 : 3600
            wait = min(wait, (floor(e / step) + 1) * step - e)
        }
        return d.addingTimeInterval(wait.isFinite ? max(0.05, wait) : 60)
    }
}
