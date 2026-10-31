import SwiftUI

struct IslandSourceTable: View {
    @ObservedObject var store: IslandSettingsStore
    var rendering = false
    var focus: String?
    private let sourceWidth: CGFloat = 112
    private let showWidth: CGFloat = 38
    private let alertsWidth: CGFloat = 136
    private let approveWidth: CGFloat = 48

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                header("Show agents from", key: "source", width: sourceWidth, alignment: .leading)
                header("Show", key: "show", width: showWidth)
                header("Alerts", key: "alerts", width: alertsWidth, alignment: .leading)
                header("Approve", key: "approve", width: approveWidth)
            }
            .font(.system(size: 10, weight: .semibold)).foregroundStyle(IslandInk.metaColor)
            .frame(height: SettingsStyle.rowHeight).padding(.horizontal, 12)
            .layoutContainer("row|sources-header|sourceHeader|header")
            ForEach(IslandSettingsSchema.tableSources, id: \.rawValue) { source in
                Rectangle().fill(SettingsStyle.divider).frame(height: 1).padding(.horizontal, 10)
                sourceRow(source)
            }
        }
    }

    private func header(_ title: String, key: String, width: CGFloat, alignment: Alignment = .center) -> some View {
        Text(title).lineLimit(1).layoutProbe("header:" + key).frame(width: width, alignment: alignment)
    }

    private func sourceRow(_ source: AgentSource) -> some View {
        let prefix = source.keyPrefix
        let enabled = prefix + "enabled", cap = prefix + "cap", approve = prefix + "canRaiseApprovals"
        let shown = store.sourceEnabled(source)
        let canApprove = store.sourceControlEnabled(approve)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    icon(source).frame(width: 20, height: 20).layoutProbe("symbol:\(prefix)", .glyph)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(source.title).font(.system(size: 12, weight: .medium))
                            .lineLimit(1).truncationMode(.tail).layoutProbe("label:\(prefix)", truncates: true)
                        if source == .grokBot {
                            Text("includes Comet").font(.system(size: 9)).foregroundStyle(IslandInk.metaColor)
                                .lineLimit(1).layoutProbe("subtitle:\(prefix)", truncates: true)
                        }
                    }
                    Spacer(minLength: 0)
                }.frame(width: sourceWidth)
                SettingsSwitch(isOn: binding(enabled), probe: "control:\(enabled)")
                    .modifier(SettingsFocusRing(on: focus == enabled, radius: 10, key: enabled))
                    .accessibilityLabel(source.title + " Show")
                    .frame(width: showWidth)
                SettingsPopup(options: IslandSettingsSchema.alertOptions,
                              selection: Binding(get: { store.cap(source).rawValue }, set: { store.set($0, for: cap) }),
                              rendering: rendering, key: cap, compactWidth: 116,
                              disabledValues: source == .samantha ? ["info"] : [])
                    .frame(width: alertsWidth, alignment: .leading)
                    .layoutProbe("control:\(cap)", .control)
                    .modifier(SettingsFocusRing(on: focus == cap, radius: 6, key: cap, hOutset: 2))
                    .accessibilityLabel(source.title + " Alerts")
                    .disabled(!shown).opacity(shown ? 1 : 0.4)
                SettingsSwitch(isOn: Binding(get: { source == .codex ? false : store.bool(approve) }, set: { store.set($0, for: approve) }),
                               probe: "control:\(approve)")
                    .modifier(SettingsFocusRing(on: focus == approve, radius: 10, key: approve))
                    .accessibilityLabel(source.title + " Approve")
                    .frame(width: approveWidth)
                    .disabled(!canApprove).opacity(canApprove ? 1 : 0.4)
            }
            .frame(height: SettingsStyle.rowHeight).padding(.horizontal, 12)
            if source == .codex {
                caption("Codex can't receive decisions yet.", key: prefix)
            } else if shown && store.cap(source) == .info {
                caption("List only sends no approval cards.", key: prefix)
            }
            if source == .samantha { caption("Voice turns always alert.", key: prefix + "voice") }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .layoutContainer("row|sources|source|\(source.rawValue)")
    }

    private func binding(_ key: String) -> Binding<Bool> {
        Binding(get: { store.bool(key) }, set: { store.set($0, for: key) })
    }

    private func caption(_ text: String, key: String) -> some View {
        Text(text).font(.system(size: 11)).foregroundStyle(IslandInk.metaColor)
            .fixedSize(horizontal: false, vertical: true).layoutProbe("caption:\(key)", truncates: true)
            .padding(.horizontal, 12).padding(.bottom, 8)
    }

    @ViewBuilder private func icon(_ source: AgentSource) -> some View {
        switch AppIconResolver.resolve(source.title) {
        case .image(let image): Image(nsImage: image).resizable().scaledToFit()
        case .symbol(let name): Image(systemName: name).foregroundStyle(IslandInk.secondaryColor)
        }
    }
}
