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
}

struct IslandSettingsRoot: View {
    @ObservedObject var store: IslandSettingsStore
    @State var tab: SettingsTab = .general
    var rendering = false
    var hasBattery = IslandSettingsSchema.hasInternalBattery
    var onVoiceSettings: () -> Void = {}
    var onShowLog: () -> Void = {}

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Rectangle().fill(Color.black.opacity(0.35)).frame(width: 1)
            content
        }
        .frame(width: SettingsStyle.size.width, height: rendering ? nil : SettingsStyle.size.height, alignment: .topLeading)
        .frame(minHeight: SettingsStyle.size.height, alignment: .top)
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
        }
        .frame(width: SettingsStyle.sidebarWidth, alignment: .topLeading)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(SettingsStyle.sidebarBG)
    }

    private var sidebarRows: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(IslandSettingsSchema_sidebar(hasBattery: hasBattery).enumerated()), id: \.offset) { _, group in
                if let title = group.0 {
                    Text(title)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.white.opacity(0.55))
                        .padding(.leading, 20).padding(.top, 10).padding(.bottom, 4)
                }
                ForEach(group.1) { t in sidebarRow(t) }
            }
        }
    }

    private func sidebarRow(_ t: SettingsTab) -> some View {
        let selected = t == tab
        return HStack(spacing: 8) {
            IconTile(symbol: t.symbol, color: Color(nsColor: t.tint), size: 20)
            Text(t.title).font(.system(size: 13)).foregroundStyle(Color.white.opacity(t == .fileTray ? 0.45 : 0.92))
            Spacer(minLength: 0)
            if let b = t.badge { Badge(text: b) }
        }
        .padding(.horizontal, 8)
        .frame(width: 200, height: 36)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(selected ? Color.white.opacity(0.10) : .clear))
        .contentShape(Rectangle())
        .onTapGesture { tab = t }
        .padding(.leading, 12)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                IconTile(symbol: tab.symbol, color: Color(nsColor: tab.tint), size: 18)
                Text(tab.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(.white)
                if let b = tab.badge { Badge(text: b) }
            }
            .padding(.leading, 12).padding(.top, rendering ? 14 : 14).frame(height: 50, alignment: .leading)
            if rendering {
                page.padding(.horizontal, 12).padding(.bottom, 16)
            } else {
                ScrollView { page.padding(.horizontal, 12).padding(.bottom, 16) }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var page: some View {
        let defs = IslandSettingsSchema.defs(for: tab).filter { d in d.visibleWhen.map { store.bool($0) } ?? true }
        let groups = Self.group(defs)
        return VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(groups.enumerated()), id: \.offset) { _, g in
                VStack(alignment: .leading, spacing: 6) {
                    if let s = g.section {
                        Text(s).font(.system(size: 12, weight: .semibold)).foregroundStyle(Color.white.opacity(0.55)).padding(.leading, 4)
                    }
                    SettingsCard {
                        ForEach(Array(g.rows.enumerated()), id: \.element.key) { i, d in
                            if i > 0 { Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10) }
                            SettingRow(def: d, store: store, rendering: rendering,
                                       onButton: { key in
                                           if key.hasSuffix("voiceSettings") { onVoiceSettings() }
                                           else if key == IslandSettingsSchema.calendarAccess { IslandSettingsSchema.calendarAccessAction?() }
                                           else if key == IslandSettingsSchema.focusAccess { IslandSettingsSchema.focusAccessAction?() }
                                           else { onShowLog() }
                                       })
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
                Text("Samantha Island").font(.system(size: 13, weight: .semibold)).foregroundStyle(.white)
                Spacer()
                Text(v).font(.system(size: 12, weight: .medium).monospacedDigit()).foregroundStyle(Color.white.opacity(0.55))
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
            Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10)
            Text("Alcove-style island for Samantha and your AI agents. Rows marked Soon are saved now and take effect when that feature ships.")
                .font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.7))
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
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

/// Sidebar groups (indirection keeps the schema type free of SwiftUI).
func IslandSettingsSchema_sidebar(hasBattery: Bool) -> [(String?, [SettingsTab])] { SettingsTab.sidebar(hasBattery: hasBattery) }

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

struct Badge: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 10, weight: .medium)).foregroundStyle(Color.white.opacity(0.55))
            .padding(.horizontal, 6).padding(.vertical, 1)
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.3), lineWidth: 1))
    }
}

// MARK: - Rows

struct SettingRow: View {
    let def: SettingDef
    @ObservedObject var store: IslandSettingsStore
    var rendering = false
    var onButton: (String) -> Void = { _ in }

    var body: some View {
        switch def.kind {
        case .master(let symbol):
            HStack(spacing: 8) {
                Image(systemName: symbol).font(.system(size: 12, weight: .semibold)).foregroundStyle(Color(nsColor: def.tab.tint).opacity(0.95))
                label
                Spacer(minLength: 0)
                SettingsSwitch(isOn: boolBinding)
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .toggle:
            HStack(spacing: 6) {
                label
                if let i = def.info { InfoDot(text: i) }
                Spacer(minLength: 0)
                SettingsSwitch(isOn: boolBinding)
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .slider(let lo, let hi, let step, let unit):
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    label
                    if let i = def.info { InfoDot(text: i) }
                    Spacer()
                    ValuePill(text: Self.format(store.double(def.key), step: step, unit: unit))
                }
                ValueSlider(value: doubleBinding, range: lo...hi, step: step)
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
        case .tiles(let options):
            VStack(alignment: .leading, spacing: 6) {
                if def.label != "" && !(def.tab == .general && def.key.hasSuffix("idleActivity")) {
                    HStack { label; if let i = def.info { InfoDot(text: i) }; Spacer() }
                }
                TilePicker(options: options, selection: stringBinding)
            }
            .padding(10)
        case .popup(let options):
            HStack(spacing: 6) {
                label
                if let i = def.info { InfoDot(text: i) }
                Spacer(minLength: 0)
                SettingsPopup(options: options, selection: stringBinding, rendering: rendering)
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        case .token:
            TokenRow(def: def, store: store, rendering: rendering)
        case .button(let title):
            HStack {
                label
                Spacer()
                Button(title) { onButton(def.key) }
                    .buttonStyle(PillButtonStyle())
            }
            .padding(.horizontal, 12).frame(height: SettingsStyle.rowHeight)
        }
    }

    private var label: some View {
        HStack(spacing: 6) {
            Text(def.label).font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.92))
            if !def.wired { Badge(text: "Soon") }
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
    var body: some View {
        ZStack(alignment: isOn ? .trailing : .leading) {
            Capsule().fill(isOn ? SettingsStyle.accent : Color.white.opacity(0.18)).frame(width: 38, height: 20)
            Circle().fill(Color.white).frame(width: 16, height: 16).padding(.horizontal, 2)
                .shadow(color: .black.opacity(0.25), radius: 1, y: 0.5)
        }
        .frame(width: 44, height: 20)
        .contentShape(Rectangle())
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
    var body: some View {
        HStack(spacing: 8) {
            ForEach(options, id: \.value) { o in
                let on = o.value == selection
                VStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? Color.black : Color.white.opacity(0.08))
                        .frame(height: 44)
                        .overlay(Image(systemName: o.symbol).font(.system(size: 15, weight: .semibold)).foregroundStyle(Color.white.opacity(on ? 1 : 0.6)))
                        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(on ? SettingsStyle.accent : .clear, lineWidth: 2))
                    Text(o.label).font(.system(size: 11, weight: on ? .bold : .regular)).foregroundStyle(Color.white.opacity(on ? 0.95 : 0.55))
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
    let options: [(value: String, label: String)]
    @Binding var selection: String
    var rendering = false
    var body: some View {
        let current = options.first { $0.value == selection }?.label ?? selection
        let lbl = HStack(spacing: 4) {
            Text(current).font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.85))
            Image(systemName: "chevron.up.chevron.down").font(.system(size: 9, weight: .semibold)).foregroundStyle(Color.white.opacity(0.6))
        }
        if rendering {
            lbl
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
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 12, weight: .medium)).foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(Color.white.opacity(configuration.isPressed ? 0.2 : 0.1)))
    }
}

/// Keychain token: only ever shows "••••" + last 3. Rotate replaces it at once; Copy puts it on the clipboard.
struct TokenRow: View {
    let def: SettingDef
    @ObservedObject var store: IslandSettingsStore
    var rendering = false
    @State private var error: String?

    private var source: AgentSource? { def.tab.agentSource }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "key.fill").font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.6))
            VStack(alignment: .leading, spacing: 1) {
                Text(source.map { store.maskedToken($0) } ?? "–")
                    .font(.system(size: 13, weight: .medium).monospaced()).foregroundStyle(.white)
                Text(error ?? def.info ?? "").font(.system(size: 10)).foregroundStyle(Color.white.opacity(0.45))
            }
            Spacer()
            Button("Copy") { copy() }.buttonStyle(PillButtonStyle()).disabled(rendering)
            Button("Rotate") { rotate() }.buttonStyle(PillButtonStyle()).disabled(rendering)
        }
        .padding(.horizontal, 12).frame(height: 48)
    }

    private func rotate() {
        guard let s = source else { return }
        do { try store.rotateToken(s); error = nil } catch { self.error = "Keychain error" }
    }
    private func copy() {
        guard let s = source else { return }
        error = store.copyToken(s) ? "Copied — clears from the clipboard in 60 s" : "Not set — Rotate first"
    }
}
