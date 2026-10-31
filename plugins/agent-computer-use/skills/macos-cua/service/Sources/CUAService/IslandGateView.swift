import AppKit
import SwiftUI

struct IslandGateView: View {
    @ObservedObject var gate: IslandGate
    var close: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 22) {
            HStack(spacing: 10) {
                BrandMark(size: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Agent Computer Use").font(.system(size: 14, weight: .semibold))
                    Text("DYNAMIC ISLAND").font(.system(size: 10, weight: .medium)).tracking(1.5).foregroundStyle(.secondary)
                }
                Spacer()
            }
            preview
            VStack(alignment: .leading, spacing: 16) { content }
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack {
                Image(systemName: "lock.shield")
                Text("Free to unlock. One account, one Mac.")
            }.font(.system(size: 11)).foregroundStyle(.secondary)
            Button("Close", action: close).keyboardShortcut(.cancelAction).hidden().frame(height: 0)
        }
        .padding(28).frame(width: 440)
        .background {
            ZStack {
                Color(white: 0.075)
                IslandGateVibrancy()
                Color.black.opacity(0.74)
            }.ignoresSafeArea()
        }
        .ignoresSafeArea()
        .foregroundStyle(.white)
        .preferredColorScheme(.dark)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.15), value: gate.state)
    }
    private var preview: some View {
        HStack(spacing: 12) {
            BrandMark(size: 20)
            VStack(alignment: .leading, spacing: 3) {
                Text("Your agents, at a glance").font(.system(size: 12, weight: .semibold))
                Text("Live runs · approvals · Samantha").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            Image(systemName: "waveform").foregroundStyle(.mint)
        }.padding(.horizontal, 20).padding(.vertical, 17)
            .background(.black, in: Capsule())
            .clipShape(Capsule())
            .overlay(Capsule().strokeBorder(.white.opacity(0.12), lineWidth: 1))
            .compositingGroup()
            .shadow(color: .mint.opacity(0.12), radius: 6, y: 2)
            .padding(18)
            .clipped()
            .accessibilityElement(children: .combine)
    }
    @ViewBuilder private var content: some View {
        switch gate.state {
        case .locked(let linkedIn, let message):
            unlockSheet(linkedIn: linkedIn, message: message)
        case .submitting:
            heading("Sending your check", "Hang on a moment.")
            ProgressView().controlSize(.small)
        case .waking:
            heading("Waking up the unlock service…", "This can take up to a minute the first time.")
            ProgressView().controlSize(.small)
        case .checking(_, _, let follow, let post):
            checkingView(follow: follow, post: post)
        case .conflict(let device, let available):
            heading("Bring your island here?", "Island is on \(device ?? "another Mac"). Move it to this Mac with a fresh post?")
            if let device { Label(device, systemImage: "laptopcomputer").font(.system(size: 14, weight: .medium)) }
            if let available { notice("You can move it on \(Self.date(available)).") }
            primary("Try again", action: gate.tryMoveHere).disabled(Self.inCooldown(available))
            Button("Cancel", action: close).buttonStyle(.link)
        case .unlocked(let account, let verified, let exp):
            heading("Your island is ready.", "Your agents now have a home at the top of this Mac.")
            if let banner = gate.deepLinkBanner { notice(banner) }
            HStack {
                Label(account.handle, systemImage: "person.crop.circle")
                if verified { Image(systemName: "checkmark.seal.fill").foregroundStyle(.blue).accessibilityLabel("Verified account") }
            }.font(.system(size: 14, weight: .semibold))
            Text("\(account.provider == "x" ? "X" : "LinkedIn") · Bound to this Mac").font(.system(size: 12)).foregroundStyle(.secondary)
            Text("Offline access until \(exp.formatted(date: .abbreviated, time: .omitted))").font(.system(size: 11)).foregroundStyle(.secondary)
            wallConsent(unlocked: true)
            primary("Enjoy your island", action: close)
        case .rejected(let message):
            heading("Almost there.", message)
            primary("Try again", action: gate.retryFromRejected)
        case .error(_, let message):
            heading("Let's try that again.", message)
            primary("Retry", action: gate.retry)
            Button("Start over") { gate.signOut() }.buttonStyle(.link)
        }
    }
    @ViewBuilder private func unlockSheet(linkedIn: Bool, message: String?) -> some View {
        heading("Unlock the island", linkedIn ? "Follow on LinkedIn, share the preset, then tell us your profile." : "Follow @gurusharan, post about ACU, then tell us your handle.")
        if let message { notice(message) }
        if let banner = gate.deepLinkBanner, !gate.isUnlocked { notice(banner) }
        if linkedIn {
            linkedInSteps
        } else {
            xSteps
            Button("No X account? Use LinkedIn") { gate.useLinkedInRoute() }.buttonStyle(.link).font(.system(size: 12)).frame(maxWidth: .infinity)
        }
    }
    private var xSteps: some View {
        Group {
            stepRow("Follow @gurusharan", button: "Follow", action: gate.openFollow, xPrimary: true)
            stepRow("Post about ACU", button: "Post", action: gate.openShare, xPrimary: true)
            Text("Your X handle").font(.system(size: 12, weight: .medium))
            TextField("@handle", text: $gate.handleText).textFieldStyle(.roundedBorder).accessibilityLabel("Your X handle").accessibilityIdentifier("Your X handle")
            optionalPostLink
            wallConsent(unlocked: false)
            primary("Check", action: gate.submitCheck).disabled(!gate.isHandleValid)
        }
    }
    private var linkedInSteps: some View {
        Group {
            if gate.config?.linkedInFollowURL != nil {
                stepRow("Follow Gurusharan on LinkedIn", button: "Follow", action: gate.openFollow, xPrimary: false)
            }
            stepRow("Share on LinkedIn", button: "Share", action: gate.openShare, xPrimary: false)
            if gate.copied { Text("Text copied - paste with ⌘V").font(.system(size: 12)).foregroundStyle(.mint).accessibilityAddTraits(.updatesFrequently) }
            Text("Your LinkedIn profile URL").font(.system(size: 12, weight: .medium))
            TextField("https://www.linkedin.com/in/…", text: $gate.handleText).textFieldStyle(.roundedBorder).accessibilityLabel("Your LinkedIn profile URL")
            optionalPostLink
            wallConsent(unlocked: false)
            primary("Check", action: gate.submitCheck).disabled(!gate.isHandleValid)
            Button("Use X instead") { gate.useXRoute() }.buttonStyle(.link).font(.system(size: 12))
        }
    }
    private var optionalPostLink: some View {
        Group {
            Text("Post link (optional)").font(.system(size: 12, weight: .medium))
            TextField("https://…", text: $gate.postURL).textFieldStyle(.roundedBorder).accessibilityLabel("Post link").accessibilityIdentifier("Post link")
        }
    }
    private func checkingView(follow: IslandGateCheckStatus, post: IslandGateCheckStatus) -> some View {
        Group {
            heading("Checking… \(gate.checkETAText)", "You can close this window, the island turns on by itself.")
            ProgressView().controlSize(.regular)
            checkRow("Follow @gurusharan", status: follow)
            checkRow("Post about ACU", status: post)
        }
    }
    private func checkRow(_ title: String, status: IslandGateCheckStatus) -> some View {
        HStack(spacing: 9) {
            Image(systemName: status == .confirmed ? "checkmark.circle.fill" : (status == .failed ? "xmark.circle.fill" : "circle"))
                .foregroundColor(status == .confirmed ? .mint : (status == .failed ? .orange : .secondary))
            Text(title).font(.system(size: 12, weight: .medium))
            Spacer()
            Text(status == .confirmed ? "Confirmed" : (status == .failed ? "Pending" : "Pending")).font(.system(size: 11)).foregroundStyle(.secondary)
        }.padding(12).background(.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
    }
    private func stepRow(_ title: String, button: String, action: @escaping () -> Void, xPrimary: Bool) -> some View {
        HStack {
            Text(title).font(.system(size: 12, weight: .medium)).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if xPrimary {
                Button(button, action: action).accessibilityLabel(button).accessibilityIdentifier(button).buttonStyle(IslandGatePrimaryButtonStyle(xBrand: true)).controlSize(.small)
            } else {
                Button(button, action: action).accessibilityLabel(button).accessibilityIdentifier(button).buttonStyle(.link).controlSize(.small)
            }
        }.padding(12).background(.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
    }
    private func wallConsent(unlocked: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(gate.wallConsentTitle, isOn: Binding(get: { gate.showOnWall }, set: { show in
                if unlocked { gate.setWallVisibility(show) } else { gate.setLocalWallConsent(show) }
            }))
                .toggleStyle(.checkbox).font(.system(size: 12))
                .disabled(unlocked && gate.wallUpdating)
            Text("Your handle, avatar and profile link appear in the public 'Who's using it' list. Turn off any time.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let error = gate.wallError { notice(error) }
        }
    }
    private func heading(_ title: String, _ subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.system(size: 25, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
            Text(subtitle).font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
        }
    }
    private func primary(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Text(title).frame(maxWidth: .infinity) }.accessibilityLabel(title).accessibilityIdentifier(title).buttonStyle(IslandGatePrimaryButtonStyle()).keyboardShortcut(.defaultAction)
    }
    private func notice(_ text: String) -> some View {
        Text(text).font(.system(size: 12)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
    }
    static func date(_ raw: String) -> String {
        if let date = ISO8601DateFormatter().date(from: raw) ?? Double(raw).map({ Date(timeIntervalSince1970: $0) }) { return date.formatted(date: .abbreviated, time: .shortened) }
        return "the scheduled date"
    }
    static func inCooldown(_ raw: String?) -> Bool {
        guard let raw else { return false }
        let date = ISO8601DateFormatter().date(from: raw) ?? Double(raw).map { Date(timeIntervalSince1970: $0) }
        return date.map { $0 > Date() } ?? true
    }
}

private struct IslandGateXMark: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.minX, y: rect.minY)); p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        p.move(to: CGPoint(x: rect.maxX, y: rect.minY)); p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        return p
    }
}

struct IslandGateVibrancy: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView(); view.material = .hudWindow; view.blendingMode = .behindWindow; view.state = .active
        return view
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

@MainActor
private final class IslandGateHostingView: NSHostingView<IslandGateView> {
    override var safeAreaInsets: NSEdgeInsets { NSEdgeInsetsZero }
}

@MainActor
private final class IslandGateWindow: NSWindow {
    override func cancelOperation(_ sender: Any?) { performClose(sender) }
}

@MainActor
final class IslandGateWindowController: NSWindowController, NSWindowDelegate {
    private let gate: IslandGate
    init(gate: IslandGate) {
        self.gate = gate
        let window = IslandGateWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 500), styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Unlock Dynamic Island"
        window.titlebarAppearsTransparent = true; window.titleVisibility = .hidden
        window.titlebarSeparatorStyle = .none
        window.isOpaque = false; window.backgroundColor = NSColor(calibratedWhite: 0.075, alpha: 1)
        window.isMovableByWindowBackground = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false; window.delegate = self
        let hosting = IslandGateHostingView(rootView: IslandGateView(gate: gate, close: { [weak self] in self?.close() }))
        if #available(macOS 13.3, *) { hosting.safeAreaRegions = [] }
        window.contentView = hosting
        gate.onChange.append { [weak self] in
            DispatchQueue.main.async { self?.fitContent() }
        }
        fitContent()
        window.center()
    }
    func fitContent() {
        guard let window, let content = window.contentView else { return }
        content.layoutSubtreeIfNeeded()
        let height = max(1, content.fittingSize.height)
        window.setFrame(NSRect(x: window.frame.minX, y: window.frame.maxY - height, width: 440, height: height), display: window.isVisible)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func windowWillClose(_ notification: Notification) { gate.closed() }
}

struct IslandGateAccessRow: View {
    @ObservedObject var gate: IslandGate
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Island access").font(.system(size: 12, weight: .semibold))
            if case .unlocked(let account, let verified, _) = gate.state {
                HStack {
                    Text(gate.accessSummary)
                    if verified { Image(systemName: "checkmark.seal.fill").foregroundStyle(.blue).accessibilityLabel("Verified account") }
                    Spacer()
                    Button("Sign out") { gate.signOut() }
                }
                Toggle("Show me on the site", isOn: Binding(get: { gate.showOnWall }, set: { gate.setWallVisibility($0) }))
                    .toggleStyle(.checkbox).disabled(gate.wallUpdating)
                if let error = gate.wallError { Text(error).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true) }
                Label("Bound to this Mac", systemImage: "laptopcomputer").foregroundStyle(.secondary)
            } else {
                Button("Unlock Dynamic Island…") { gate.show() }
            }
        }.font(.system(size: 12)).padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }
}
