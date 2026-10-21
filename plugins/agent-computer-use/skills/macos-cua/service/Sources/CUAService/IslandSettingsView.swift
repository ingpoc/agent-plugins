import AppKit
import SwiftUI

// Alcove-replica Settings window: 620×687, sidebar 213 pt (rows 200×38, coloured icon tiles,
// section headers), grouped cards with 42 pt rows, tile pickers, value-pill sliders, master
// toggles. Single accent #64D2FF. All controls are drawn in SwiftUI (no AppKit-backed
// controls) so the window renders identically offscreen via ImageRenderer.

enum SettingsStyle {
    static let accent = Color(red: 0x64 / 255, green: 0xD2 / 255, blue: 0xFF / 255)
    static let windowBG = Color(red: 0.19, green: 0.19, blue: 0.19)
    static let sidebarBG = Color(red: 0.24, green: 0.24, blue: 0.24)
    static let card = Color.white.opacity(0.06)
    static let divider = Color.white.opacity(0.08)
    static let size = CGSize(width: 620, height: 687)
    static let sidebarWidth: CGFloat = 213
    static let rowHeight: CGFloat = 42
    static let tokenRowHeight: CGFloat = 42
    /// Card row width: window − sidebar − divider − 2 × 12 pt page padding.
    static let rowWidth: CGFloat = size.width - sidebarWidth - 1 - 24
}

/// Keyboard focus ring for a settings control (2 pt accent, 3 pt outside the control).
struct SettingsFocusRing: ViewModifier {
    let on: Bool
    let radius: CGFloat
    let key: String
    /// Extra horizontal room for borderless controls (popup text) so the ring doesn't touch the glyphs.
    var hOutset: CGFloat = 3
    func body(content: Content) -> some View {
        content.overlay {
            if on {
                RoundedRectangle(cornerRadius: radius + 3, style: .continuous)
                    .strokeBorder(SettingsStyle.accent, lineWidth: 2)
                    .padding(.horizontal, -hOutset).padding(.vertical, -3)
                    .layoutProbe("focus:\(key)", .ring)
                    .allowsHitTesting(false)
            }
        }
    }
}

/// Selected pane + keyboard focus (a row key). Shared with the window's key monitor.
@MainActor
final class SettingsNav: ObservableObject {
    @Published var tab: SettingsTab { didSet { if tab != oldValue { focus = nil } } }
    @Published var focus: String?
    init(tab: SettingsTab = .general, focus: String? = nil) { self.tab = tab; self.focus = focus }
}

struct IslandSettingsRoot: View {
    @ObservedObject var store: IslandSettingsStore
    @ObservedObject var nav: SettingsNav
    var rendering = false
    /// Permission state for `.permission` rows (tests inject granted / missing).
    var permissions: (String) -> Bool = IslandSettingsSchema.permissionGranted
    /// Row source (tests inject worst-case labels).
    var defs: (SettingsTab) -> [SettingDef] = IslandSettingsSchema.defs(for:)
    var onShowLog: () -> Void = {}

    init(store: IslandSettingsStore, tab: SettingsTab = .general, nav: SettingsNav? = nil, rendering: Bool = false,
         permissions: @escaping (String) -> Bool = IslandSettingsSchema.permissionGranted,
         defs: @escaping (SettingsTab) -> [SettingDef] = IslandSettingsSchema.defs(for:),
         onShowLog: @escaping () -> Void = {}) {
        self.store = store
        self.nav = nav ?? SettingsNav(tab: tab)
        self.rendering = rendering
        self.permissions = permissions
        self.defs = defs
        self.onShowLog = onShowLog
    }

    private var tab: SettingsTab { nav.tab }

    /// Rows shown for a pane (hidden `visibleWhen` rows dropped) — the view and the keyboard use the same list.
    static func visibleDefs(_ tab: SettingsTab, store: IslandSettingsStore,
                            defs: (SettingsTab) -> [SettingDef] = IslandSettingsSchema.defs(for:)) -> [SettingDef] {
        defs(tab).filter { d in d.visibleWhen.map { visible($0, store) } ?? true }
    }

    /// `visibleWhen`: a bool key, "key=value" (the GPT-Live-1 pickers) or "key^=prefix" (Realtime-only rows).
    static func visible(_ cond: String, _ store: IslandSettingsStore) -> Bool {
        if cond.hasPrefix(IslandSettingsSchema.installedPrefix) {
            let p = ACUPermission.allCases.first { $0.bundleID == String(cond.dropFirst(IslandSettingsSchema.installedPrefix.count)) }
            return p.map { PermissionCenter.state($0) != .notInstalled } ?? true
        }
        if let r = cond.range(of: "^=") {
            return store.string(String(cond[..<r.lowerBound])).hasPrefix(String(cond[r.upperBound...]))
        }
        guard let eq = cond.firstIndex(of: "=") else { return store.bool(cond) }
        return store.string(String(cond[..<eq])) == String(cond[cond.index(after: eq)...])
    }

    /// Button-row actions (mouse and keyboard).
    static func perform(_ key: String, onShowLog: () -> Void) {
        if key == IslandSettingsSchema.calendarAccess { IslandSettingsSchema.calendarAccessAction?() }
        else if key == IslandSettingsSchema.focusAccess { IslandSettingsSchema.focusAccessAction?() }
        else if let p = ACUPermission.from(key: key) { PermissionCenter.shared.request(p) }
        else if key == IslandSettingsSchema.accessibilityDisplay || key == IslandSettingsSchema.accessibilitySound {
            // Opens the Accessibility list in System Settings (the user adds the app there).
            if let u = ACUPermission.accessibility.paneURL { NSWorkspace.shared.open(u) }
        }
        else { onShowLog() }
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Rectangle().fill(Color.black.opacity(0.35)).frame(width: 1)
            content
        }
        .frame(width: SettingsStyle.size.width, height: rendering ? nil : SettingsStyle.size.height, alignment: .topLeading)
        .frame(minHeight: SettingsStyle.size.height, alignment: .top)
        .coordinateSpace(name: IslandLayoutRecorder.space)
        .background(SettingsStyle.windowBG)
        .environment(\.colorScheme, .dark)
        .tint(SettingsStyle.accent)
    }

    // MARK: Sidebar

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            if rendering {
                // Window chrome is drawn by AppKit live; drawn here only for offscreen renders.
                HStack(spacing: 8) {
                    ForEach([Color(red: 1, green: 0.37, blue: 0.34), Color(white: 0.45), Color(white: 0.45)], id: \.self) { c in
                        Circle().fill(c).frame(width: 12, height: 12)
                    }
                }
                .padding(.leading, 18).padding(.top, 18)
            }
            Spacer().frame(height: rendering ? 21 : 51)
            if rendering { sidebarRows } else { ScrollView(showsIndicators: false) { sidebarRows } }
            Spacer(minLength: 0)
            sidebarRow(SettingsTab.pinned).padding(.bottom, 12)   // About: pinned, no header
        }
        .frame(width: SettingsStyle.sidebarWidth, alignment: .topLeading)
        .frame(maxHeight: .infinity, alignment: .top)
        .layoutContainer("sidebar")
        .background(SettingsStyle.sidebarBG)
    }

    private var sidebarRows: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(SettingsTab.sidebar.enumerated()), id: \.offset) { _, group in
                if let title = group.0 {
                    Text(title)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.white.opacity(0.55))
                        .layoutProbe("sidebar-header:\(title)")
                        .padding(.leading, 20).padding(.top, 10).padding(.bottom, 4)
                }
                ForEach(group.1) { t in sidebarRow(t) }
            }
        }
    }

    private func sidebarRow(_ t: SettingsTab) -> some View {
        let selected = t == tab
        return HStack(spacing: 8) {
            IconTile(symbol: t.symbol, color: Color(nsColor: t.tint), size: 20).layoutProbe("side-icon:\(t.rawValue)", .glyph)
            Text(t.title).font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.92))
                .layoutProbe("side-title:\(t.rawValue)")
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 8)
        .frame(width: 200, height: 36)
        .layoutContainer("side|\(t.rawValue)")
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(selected ? Color.white.opacity(0.10) : .clear))
        .contentShape(Rectangle())
        .onTapGesture { nav.tab = t }
        .padding(.leading, 12)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                IconTile(symbol: tab.symbol, color: Color(nsColor: tab.tint), size: 18).layoutProbe("pane-icon", .glyph)
                Text(tab.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(.white).layoutProbe("pane-title")
            }
            .padding(.leading, 12).padding(.top, rendering ? 14 : 14).frame(height: 50, alignment: .leading)
            if rendering {
                page.padding(.horizontal, 12).padding(.bottom, 16)
            } else {
                ScrollView { page.padding(.horizontal, 12).padding(.bottom, 16) }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .layoutContainer("content")
    }

    private var page: some View {
        let groups = Self.group(Self.visibleDefs(tab, store: store, defs: defs))
        return VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(groups.enumerated()), id: \.offset) { gi, g in
                VStack(alignment: .leading, spacing: 6) {
                    if let s = g.section {
                        Text(s).font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.white.opacity(0.55))
                            .lineLimit(1).truncationMode(.tail).help(s)
                            .layoutProbe("section:\(gi)", truncates: true).padding(.leading, 4)
                    }
                    SettingsCard {
                        ForEach(Array(g.rows.enumerated()), id: \.element.key) { i, d in
                            if i > 0 { Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10) }
                            SettingRow(def: d, store: store, rendering: rendering, card: "c\(gi)", focused: nav.focus == d.key,
                                       granted: { if case .permission = d.kind { return permissions(d.key) }; return false }(),
                                       onButton: { key in Self.perform(key, onShowLog: onShowLog) })
                        }
                    }
                }
            }
            if tab == .about { aboutFooter }
        }
    }

    private var aboutFooter: some View {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
        return SettingsCard {
            HStack {
                Text("Samantha Island").font(.system(size: 13, weight: .semibold)).foregroundStyle(.white).layoutProbe("about-name")
                Spacer()
                Text(v).font(.system(size: 12, weight: .medium).monospacedDigit()).foregroundStyle(Color.white.opacity(0.55)).layoutProbe("about-version")
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
            .layoutContainer("about|version")
            Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10)
            Text("Alcove-style island for Samantha and your AI agents.")
                .font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.7))
                .fixedSize(horizontal: false, vertical: true)
                .layoutProbe("about-text", truncates: true)   // wraps by design; frame = drawn text
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .layoutContainer("about|text")
        }
    }

    static func group(_ defs: [SettingDef]) -> [(section: String?, rows: [SettingDef])] {
        var out: [(String?, [SettingDef])] = []
        for d in defs {
            if let last = out.last, last.0 == d.section { out[out.count - 1].1.append(d) } else { out.append((d.section, [d])) }
        }
        return out.map { (section: $0.0, rows: $0.1) }
    }
}


struct SettingsCard<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(spacing: 0, content: content)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(SettingsStyle.card))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.white.opacity(0.05), lineWidth: 1))
    }
}

struct IconTile: View {
    let symbol: String
    let color: Color
    var size: CGFloat = 20
    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.26, style: .continuous).fill(color)
            .frame(width: size, height: size)
            .overlay(Image(systemName: symbol).font(.system(size: size * 0.55, weight: .semibold)).foregroundStyle(.white))
    }
}

// MARK: - Rows

struct SettingRow: View {
    let def: SettingDef
    @ObservedObject var store: IslandSettingsStore
    var rendering = false
    var card = "c0"
    var focused = false
    /// `.permission` rows: the real (or injected) macOS permission state.
    var granted = false
    var onButton: (String) -> Void = { _ in }

    static func kindTag(_ k: SettingKind) -> String {
        switch k {
        case .master: return "master"
        case .toggle: return "toggle"
        case .slider: return "slider"
        case .tiles: return "tiles"
        case .popup: return "popup"
        case .token: return "token"
        case .button: return "button"
        case .permission: return "permission"
        }
    }

    private var key: String { def.key }

    var body: some View {
        if let c = def.caption {
            // Captioned rows: the explanation sits under the row (11 pt, 45 %) instead of an "i".
            VStack(alignment: .leading, spacing: 0) {
                rowBody
                Text(c).font(.system(size: 11)).foregroundStyle(Color.white.opacity(0.45))
                    .fixedSize(horizontal: false, vertical: true)
                    .layoutProbe("caption:\(key)", truncates: true)   // wraps by design
                    .padding(.horizontal, 12).padding(.top, -4).padding(.bottom, 12)
            }
            .layoutContainer("row|\(card)|captioned|\(def.key)")
        } else {
            rowBody.layoutContainer("row|\(card)|\(Self.kindTag(def.kind))|\(def.key)")
        }
    }

    /// Action buttons never take more than half the row; the label keeps priority and truncates.
    static let buttonCap: CGFloat = SettingsStyle.rowWidth / 2

    @ViewBuilder
    private var rowBody: some View {
        switch def.kind {
        case .master(let symbol):
            HStack(spacing: 8) {
                Image(systemName: symbol).font(.system(size: 12, weight: .semibold)).foregroundStyle(Color(nsColor: def.tab.tint).opacity(0.95))
                    .layoutProbe("symbol:\(key)", .glyph)
                label
                Spacer(minLength: 0)
                SettingsSwitch(isOn: boolBinding, probe: "control:\(key)").modifier(SettingsFocusRing(on: focused, radius: 10, key: key))
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .toggle:
            HStack(spacing: 6) {
                label
                if let i = def.info { InfoDot(text: i).fixedSize().layoutProbe("info:\(key)", .glyph) }
                Spacer(minLength: 0)
                SettingsSwitch(isOn: boolBinding, probe: "control:\(key)").modifier(SettingsFocusRing(on: focused, radius: 10, key: key))
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .slider(let lo, let hi, let step, let unit):
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    label
                    if let i = def.info { InfoDot(text: i).fixedSize().layoutProbe("info:\(key)", .glyph) }
                    Spacer()
                    ValuePill(text: Self.format(store.double(def.key), step: step, unit: unit)).fixedSize().layoutProbe("control:\(key)", .control)
                }
                ValueSlider(value: doubleBinding, range: lo...hi, step: step)
                    .layoutProbe("slider:\(key)", .control)
                    .modifier(SettingsFocusRing(on: focused, radius: 10, key: key))
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
        case .tiles(let options):
            VStack(alignment: .leading, spacing: 6) {
                if def.label != "" && !(def.tab == .general && def.key.hasSuffix("idleActivity")) {
                    HStack { label; if let i = def.info { InfoDot(text: i).fixedSize().layoutProbe("info:\(key)", .glyph) }; Spacer() }
                }
                TilePicker(options: options, selection: stringBinding, key: key, focused: focused)
            }
            .padding(.horizontal, 12).padding(.vertical, 10)   // same 12 pt leading column as every row
        case .popup(let options):
            HStack(spacing: 6) {
                label
                if let i = def.info { InfoDot(text: i).fixedSize().layoutProbe("info:\(key)", .glyph) }
                Spacer(minLength: 0)
                SettingsPopup(options: options, selection: stringBinding, rendering: rendering, key: key)
                    .layoutProbe("control:\(key)", .control)
                    .modifier(SettingsFocusRing(on: focused, radius: 6, key: key, hOutset: 7))
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .token:
            TokenRow(def: def, store: store, rendering: rendering)
        case .button(let title):
            HStack(spacing: 8) {
                label.layoutPriority(1)
                Spacer(minLength: 0)
                Button(title) { onButton(def.key) }
                    .buttonStyle(PillButtonStyle())
                    .frame(width: min(Self.buttonCap, PillButtonStyle.width(title)))
                    .layoutProbe("control:\(key)", .control)
                    .modifier(SettingsFocusRing(on: focused, radius: 11, key: key))
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .permission(let allow):
            HStack(spacing: 8) {
                label.layoutPriority(1)
                Spacer(minLength: 0)
                if granted {
                    HStack(spacing: 4) {
                        Image(systemName: "checkmark.circle.fill").font(.system(size: 13)).foregroundStyle(Color(nsColor: .systemGreen))
                        Text("Allowed").font(.system(size: 12, weight: .medium)).foregroundStyle(Color.white.opacity(0.85))
                    }
                    .fixedSize()
                    .accessibilityElement(children: .combine)
                    .layoutProbe("control:\(key)", .control)
                    .modifier(SettingsFocusRing(on: focused, radius: 6, key: key, hOutset: 7))
                } else {
                    Button(allow) { onButton(def.key) }
                        .buttonStyle(PillButtonStyle())
                        .frame(width: min(Self.buttonCap, PillButtonStyle.width(allow)))
                        .layoutProbe("control:\(key)", .control)
                        .modifier(SettingsFocusRing(on: focused, radius: 11, key: key))
                }
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        }
    }

    private var label: some View {
        HStack(spacing: 6) {
            Text(def.label).font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.92))
                .lineLimit(1).truncationMode(.tail).help(def.label)
                .layoutProbe("label:\(key)", truncates: true)
        }
    }

    private var boolBinding: Binding<Bool> {
        Binding(get: { store.bool(def.key) }, set: { store.set($0, for: def.key) })
    }
    private var doubleBinding: Binding<Double> {
        Binding(get: { store.double(def.key) }, set: { store.set($0, for: def.key) })
    }
    private var stringBinding: Binding<String> {
        Binding(get: { store.string(def.key) }, set: { store.set($0, for: def.key) })
    }

    static func format(_ v: Double, step: Double, unit: String) -> String {
        let n = step < 1 ? String(format: "%.1f", v) : String(format: "%.0f", v)
        return unit == "%" ? "\(n) %" : "\(n) \(unit)"
    }
}

struct InfoDot: View {
    let text: String
    var body: some View {
        Image(systemName: "info.circle.fill").font(.system(size: 11)).foregroundStyle(Color.white.opacity(0.4)).help(text)
            .accessibilityLabel(text)
    }
}

struct ValuePill: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 11, weight: .medium).monospacedDigit()).foregroundStyle(Color.white.opacity(0.85))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(Color.white.opacity(0.08)))
    }
}

struct SettingsSwitch: View {
    @Binding var isOn: Bool
    var probe = "switch"
    var body: some View {
        ZStack(alignment: isOn ? .trailing : .leading) {
            Capsule().fill(isOn ? SettingsStyle.accent : Color.white.opacity(0.18)).frame(width: 38, height: 20)
                .layoutProbe(probe, .control)   // the visible track, not the hit area
            Circle().fill(Color.white).frame(width: 16, height: 16).padding(.horizontal, 2)
                .shadow(color: .black.opacity(0.25), radius: 1, y: 0.5)
        }
        .frame(width: 38, height: 20)   // the visible track: ends on the card's trailing column like every control
        .contentShape(Rectangle().inset(by: -3))
        .onTapGesture { withAnimation(.spring(response: 0.25, dampingFraction: 0.8)) { isOn.toggle() } }
        .accessibilityElement()
        .accessibilityAddTraits(.isButton)
        .accessibilityValue(isOn ? "on" : "off")
    }
}

struct ValueSlider: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double
    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let f = (value - range.lowerBound) / max(0.0001, range.upperBound - range.lowerBound)
            let ticks = min(12, Int(((range.upperBound - range.lowerBound) / step).rounded()))
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.14)).frame(height: 4)
                Capsule().fill(SettingsStyle.accent).frame(width: max(4, w * f), height: 4)
                ForEach(0...max(1, ticks), id: \.self) { i in
                    Rectangle().fill(Color.white.opacity(0.25)).frame(width: 1, height: 4)
                        .offset(x: w * CGFloat(i) / CGFloat(max(1, ticks)) - 0.5, y: 7)
                }
                Circle().fill(Color.white).frame(width: 16, height: 16).shadow(color: .black.opacity(0.3), radius: 1)
                    .offset(x: max(0, min(w - 16, w * f - 8)))
            }
            .frame(height: 20)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { g in
                let raw = range.lowerBound + Double(max(0, min(1, g.location.x / w))) * (range.upperBound - range.lowerBound)
                value = (raw / step).rounded() * step
            })
        }
        .frame(height: 20)
        .accessibilityElement()
        .accessibilityValue(String(value))
        .accessibilityAdjustableAction { dir in
            value = min(range.upperBound, max(range.lowerBound, value + (dir == .increment ? step : -step)))
        }
    }
}

struct TilePicker: View {
    let options: [(value: String, label: String, symbol: String)]
    @Binding var selection: String
    var key = ""
    var focused = false
    var body: some View {
        HStack(alignment: .top, spacing: 8) {   // tiles stay on one line when a label wraps to two
            ForEach(options, id: \.value) { o in
                let on = o.value == selection
                VStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? Color.black : Color.white.opacity(0.08))
                        .frame(height: 44)
                        .overlay(Image(systemName: o.symbol).font(.system(size: 15, weight: .semibold)).foregroundStyle(Color.white.opacity(on ? 1 : 0.6)))
                        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(on ? SettingsStyle.accent : .clear, lineWidth: 2))
                        .layoutProbe("tile:\(key):\(o.value)", .control)
                        .modifier(SettingsFocusRing(on: focused && on, radius: 8, key: key))
                    Text(o.label).font(.system(size: 11, weight: on ? .bold : .regular)).foregroundStyle(Color.white.opacity(on ? 0.95 : 0.55))
                        .lineLimit(2).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true).help(o.label)
                        .layoutProbe("tile-label:\(key):\(o.value)", truncates: true)
                }
                .frame(maxWidth: .infinity)
                .contentShape(Rectangle())
                .onTapGesture { selection = o.value }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
            }
        }
    }
}

struct SettingsPopup: View {
    static let maxTextWidth: CGFloat = 170
    /// Hug the selected title (so the focus ring hugs it too); long titles truncate at `maxTextWidth`.
    static func textWidth(_ s: String) -> CGFloat {
        min(maxTextWidth, ceil((s as NSString).size(withAttributes: [.font: NSFont.systemFont(ofSize: 12)]).width) + 1)
    }
    let options: [(value: String, label: String)]
    @Binding var selection: String
    var rendering = false
    var key = ""
    var body: some View {
        let current = options.first { $0.value == selection }?.label ?? selection
        let lbl = HStack(spacing: 4) {
            Text(current).font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.85))
                .lineLimit(1).truncationMode(.tail).frame(width: Self.textWidth(current))
                .layoutProbe("popup-text:\(key)", truncates: true)
            Image(systemName: "chevron.up.chevron.down").font(.system(size: 9, weight: .semibold)).foregroundStyle(Color.white.opacity(0.6))
        }
        if rendering {
            lbl.fixedSize(horizontal: false, vertical: true)
        } else {
            Menu {
                ForEach(options, id: \.value) { o in Button(o.label) { selection = o.value } }
            } label: { lbl }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
    }
}

struct PillButtonStyle: ButtonStyle {
    /// Primary = the one accent-filled action of a row (Create token).
    var primary = false
    static let font = NSFont.systemFont(ofSize: 12, weight: .medium)
    /// Hugging width for a title (text + 2×10 pt padding); callers cap it.
    static func width(_ title: String) -> CGFloat {
        ceil((title as NSString).size(withAttributes: [.font: font]).width) + 21
    }
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 12, weight: .medium)).foregroundStyle(primary ? Color.black : .white)
            .lineLimit(1).truncationMode(.tail)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(primary ? SettingsStyle.accent.opacity(configuration.isPressed ? 0.8 : 1)
                                               : Color.white.opacity(configuration.isPressed ? 0.2 : 0.1)))
    }
}

/// Keychain token. No token: one primary Create token button. Token: "••••" + last 4 in SF Mono, Copy, Rotate.
/// The setup command is the row's tooltip; nothing about the Keychain is shown as text.
struct TokenRow: View {
    let def: SettingDef
    @ObservedObject var store: IslandSettingsStore
    var rendering = false
    @State private var error: String?
    @State private var copied = false

    private var source: AgentSource? { def.tab.agentSource }

    var body: some View {
        let masked = source.flatMap { store.maskedToken($0) }
        HStack(spacing: 8) {
            Image(systemName: "key.fill").font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.6))
                .frame(width: 14).layoutProbe("symbol:\(def.key)", .glyph)
            if let error {
                Text(error).font(.system(size: 12)).foregroundStyle(Color(nsColor: .systemRed))
                    .lineLimit(1).layoutProbe("token:\(def.key)", truncates: true)
            } else if let masked {
                Text(masked).font(.system(size: 13, weight: .medium, design: .monospaced)).foregroundStyle(.white)
                    .lineLimit(1).layoutProbe("token:\(def.key)")
            } else {
                Text("No token").font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.6))
                    .lineLimit(1).layoutProbe("token:\(def.key)")
            }
            Spacer(minLength: 8)
            if masked != nil {
                Button(copied ? "Copied" : "Copy") { copy() }.buttonStyle(PillButtonStyle()).disabled(rendering).fixedSize()
                    .layoutProbe("copy:\(def.key)", .control)
                Button("Rotate") { rotate() }.buttonStyle(PillButtonStyle()).disabled(rendering).fixedSize()
                    .layoutProbe("control:\(def.key)", .control)
            } else {
                Button("Create token") { rotate() }.buttonStyle(PillButtonStyle(primary: true)).disabled(rendering).fixedSize()
                    .layoutProbe("control:\(def.key)", .control)
            }
        }
        .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        .contentShape(Rectangle())
        .help(def.info.map { "Setup: \($0)" } ?? "")
    }

    private func rotate() {
        guard let s = source else { return }
        do { try store.rotateToken(s); error = nil } catch { self.error = "Keychain error" }
    }
    private func copy() {
        guard let s = source else { return }
        if store.copyToken(s) {
            error = nil
            copied = true   // clears from the clipboard in 60 s
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { copied = false }
        } else {
            error = "No token"
        }
    }
}
