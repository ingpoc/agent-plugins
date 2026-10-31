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
        case .locked(let message):
            heading("Bring your Mac to life.", "A quiet home for your agents, right at the top of your screen. Unlock it with a follow and a post.")
            if let message { notice(message) }
            Button { gate.signIn("x") } label: {
                HStack(spacing: 9) { IslandGateXMark().stroke(.black, lineWidth: 1.5).frame(width: 15, height: 15); Text("Sign in with X") }.frame(maxWidth: .infinity)
            }.buttonStyle(IslandGatePrimaryButtonStyle(xBrand: true)).keyboardShortcut(.defaultAction)
                .accessibilityLabel("Sign in with X to unlock Dynamic Island")
            Button("No X account? Use LinkedIn") { gate.signIn("linkedin") }.buttonStyle(.link).font(.system(size: 12)).frame(maxWidth: .infinity)
        case .signingIn(let provider):
            heading("Continue in your browser", "Sign in with \(provider == "x" ? "X" : "LinkedIn"). We'll bring you back here when you're ready.")
            HStack { ProgressView().controlSize(.small); Text("Waiting for sign-in…").font(.system(size: 13)) }
            Button("Cancel") { gate.cancelSignIn() }.buttonStyle(.link)
        case .needsVerify(let provider, let follow, let post):
            heading("Make it yours.", provider == "x" ? "Two small steps to unlock your Dynamic Island." : "Share a public post, then paste its link for review.")
            if provider == "x" {
                checklist("Follow @gurusharan", button: "Follow", done: follow, required: true, action: gate.openFollow)
                checklist("Post about ACU", button: "Post", done: post, required: true, action: gate.openShare)
                if !follow || !post { notice(!follow && !post ? "Still needed: follow @gurusharan and publish your ACU post." : (!follow ? "Still needed: follow @gurusharan." : "Still needed: publish your ACU post.")) }
            } else {
                checklist("Follow Gurusharan on LinkedIn", button: "Follow", done: false, required: false, action: gate.openFollow)
                checklist("Share on LinkedIn", button: "Share", done: false, required: true, action: gate.openShare)
                if gate.copied { Text("Text copied - paste with ⌘V").font(.system(size: 12)).foregroundStyle(.mint).accessibilityAddTraits(.updatesFrequently) }
                Text("Paste your post link").font(.system(size: 12, weight: .medium))
                TextField("https://www.linkedin.com/posts/…", text: $gate.postURL).textFieldStyle(.roundedBorder).accessibilityLabel("Paste your post link")
            }
            wallConsent(unlocked: false)
            if let message = gate.verificationMessage { notice(message) }
            primary("Verify", action: gate.verify)
            Button("Use a different account") { gate.signOut() }.buttonStyle(.link).font(.system(size: 12))
        case .verifying:
            heading("Checking your island access", "This may take a moment.")
            ProgressView().controlSize(.small)
        case .pendingReview:
            heading("Your post is in review.", "Thanks for sharing ACU. We'll check every minute while this window is open. You can close it and come back later.")
            Label("Waiting for approval", systemImage: "clock").font(.system(size: 13)).foregroundStyle(.orange)
            primary("Done", action: close)
        case .conflict(let device, let boundAt, let available):
            heading("Bring your island here?", "Island is on \(device)\(boundAt.map { " since \(Self.date($0))" } ?? ""). Move it to this Mac?")
            Label(device, systemImage: "laptopcomputer").font(.system(size: 14, weight: .medium))
            if let available { notice("You can move it on \(Self.date(available)).") }
            primary("Move to this Mac", action: gate.rebind).disabled(Self.inCooldown(available))
            Button("Cancel", action: close).buttonStyle(.link)
        case .unlocked(let account, let verified, let exp):
            heading("Your island is ready.", "Your agents now have a home at the top of this Mac.")
            HStack {
                Label(account.handle, systemImage: "person.crop.circle")
                if verified { Image(systemName: "checkmark.seal.fill").foregroundStyle(.blue).accessibilityLabel("Verified account") }
            }.font(.system(size: 14, weight: .semibold))
            Text("\(account.provider == "x" ? "X" : "LinkedIn") · Bound to this Mac").font(.system(size: 12)).foregroundStyle(.secondary)
            Text("Offline access until \(exp.formatted(date: .abbreviated, time: .omitted))").font(.system(size: 11)).foregroundStyle(.secondary)
            wallConsent(unlocked: true)
            primary("Enjoy your island", action: close)
        case .error(_, let message):
            heading("Let's try that again.", message)
            primary("Retry", action: gate.retry)
            Button("Start over") { gate.signOut() }.buttonStyle(.link)
            Button("Use LinkedIn instead") { gate.signIn("linkedin") }.buttonStyle(.link)
        }
    }
    private func wallConsent(unlocked: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(gate.wallConsentTitle, isOn: Binding(get: { gate.showOnWall }, set: { show in
                if unlocked { gate.setWallVisibility(show) } else { gate.setVerificationWallConsent(show) }
            }))
                .toggleStyle(.checkbox).font(.system(size: 12))
                .disabled(gate.wallUpdating)
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
        Button(action: action) { Text(title).frame(maxWidth: .infinity) }.buttonStyle(IslandGatePrimaryButtonStyle()).keyboardShortcut(.defaultAction)
    }
    private func notice(_ text: String) -> some View {
        Text(text).font(.system(size: 12)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
    }
    private func checklist(_ title: String, button: String, done: Bool, required: Bool, action: @escaping () -> Void) -> some View {
        HStack(alignment: .center, spacing: 9) {
            Image(systemName: done ? "checkmark.circle.fill" : "circle").foregroundColor(done ? .mint : .secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 12, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                Text(done ? "Complete" : (required ? "Required" : "Optional · not verified")).font(.system(size: 10)).foregroundStyle(done ? .mint : .secondary)
            }
            Spacer(minLength: 4)
            Button(button, action: action).buttonStyle(.link).controlSize(.small).accessibilityLabel(title)
        }.padding(12).background(.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
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
    // macOS 13.0–13.2 predates NSHostingView.safeAreaRegions.
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
        // A full-size content view includes the title bar; set the frame without adding it again.
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
                Button("Move to this Mac") { gate.show(); gate.signIn(account.provider) }.buttonStyle(.link)
            } else {
                Button("Unlock Dynamic Island…") { gate.show() }
                Button("Move to this Mac") { gate.show(); gate.signIn(gate.provider) }.buttonStyle(.link)
            }
        }.font(.system(size: 12)).padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }
}
