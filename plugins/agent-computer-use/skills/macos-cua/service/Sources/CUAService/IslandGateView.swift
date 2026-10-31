import AppKit
import SwiftUI
import Combine

private enum GateColor {
    static let window = Color(red: 30/255, green: 30/255, blue: 30/255)
    static let card = Color(red: 42/255, green: 42/255, blue: 44/255)
    static let blue = Color(red: 10/255, green: 132/255, blue: 1)
    static let green = Color(red: 48/255, green: 209/255, blue: 88/255)
    static let red = Color(red: 1, green: 69/255, blue: 58/255)
    static let amber = Color(red: 1, green: 179/255, blue: 64/255)
    static let wave = Color(red: 100/255, green: 210/255, blue: 1)
}

struct IslandGateView: View {
    @ObservedObject var gate: IslandGate
    var close: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var inputFocused: Bool
    @State private var linkHover = false
    @State private var animateWave = false
    @State private var barMoving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            preview.frame(maxWidth: .infinity)
            gap(24)
            content
            gap(gate.state == .waking ? 20 : 16)
            HStack(spacing: 5) { Image(systemName: "lock.shield"); Text("Free to unlock. One account, one Mac.") }
                .font(.system(size: 11)).foregroundStyle(.white.opacity(0.5)).frame(maxWidth: .infinity)
            Button("Close", action: close).keyboardShortcut(.cancelAction).hidden().frame(height: 0)
        }
        .padding(.horizontal, 24).padding(.top, 48).padding(.bottom, 20)
        .frame(width: 440).fixedSize(horizontal: false, vertical: true)
        .background(GateColor.window).foregroundStyle(.white).preferredColorScheme(.dark)
        .onAppear { if gate.focusInput { inputFocused = true; gate.focusInput = false } }
        .onChange(of: gate.focusInput) { value in if value { inputFocused = true; gate.focusInput = false } }
    }
    private func gap(_ n: CGFloat) -> some View { Color.clear.frame(height: n) }
    private var preview: some View {
        HStack(spacing: 12) {
            BrandMark(size: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text("Your agents, at a glance").font(.system(size: 13, weight: .semibold))
                HStack(spacing: 4) {
                    if gate.isUnlocked { Circle().fill(GateColor.green).frame(width: 5, height: 5) }
                    Text("Live runs · approvals · Samantha").font(.system(size: 11)).foregroundStyle(.white.opacity(0.6))
                }
            }
            Spacer(minLength: 0)
            HStack(spacing: 2) {
                ForEach(0..<10) { i in
                    Capsule().fill(GateColor.wave).frame(width: 2, height: CGFloat([6, 10, 15, 20, 14, 19, 12, 16, 10, 6][i]) * (gate.isUnlocked && animateWave ? 0.65 : 1))
                }
            }.onAppear {
                if gate.isUnlocked && !reduceMotion { withAnimation(.easeInOut(duration: 0.65).repeatForever(autoreverses: true)) { animateWave = true } }
            }.onChange(of: gate.isUnlocked) { live in
                if live && !reduceMotion { withAnimation(.easeInOut(duration: 0.65).repeatForever(autoreverses: true)) { animateWave = true } }
            }
        }.padding(.horizontal, 18).frame(width: 340, height: 56)
            .background(.black, in: Capsule()).overlay(Capsule().strokeBorder(.white.opacity(0.08)))
            .accessibilityElement(children: .combine)
    }
    @ViewBuilder private var content: some View {
        switch gate.state {
        case .locked(let linkedIn, let message): form(linkedIn: linkedIn, message: message)
        case .submitting: heading("Checking your post", "Hang on a moment."); gap(20); ProgressView().controlSize(.mini)
        case .waking:
            heading("Starting the unlock service", "This takes up to a minute the first time."); gap(20)
            wakingBar
        case .checking(_, _, let follow, let post): checking(follow: follow, post: post, notFound: post == .failed)
        case .unlocked(let account, _, let exp): unlocked(account, exp: exp)
        case .error(let code, let message):
            if ["post_not_found", "post_author_mismatch", "post_missing_tag", "post_too_old"].contains(code) {
                checking(follow: .confirmed, post: .failed, notFound: true)
            } else if ["offline", "service_wake_timeout", "timeout", "network_error"].contains(code) {
                Image(systemName: "exclamationmark.triangle").font(.system(size: 28)).foregroundStyle(GateColor.red); gap(12)
                heading("Can’t reach the unlock service", "Check your connection and try again. Your steps are saved."); gap(20)
                primary("Try again", enabled: true, action: gate.retry); gap(12)
                secondary("Start over", action: gate.startOver)
            } else { heading("Let’s try that again.", message); gap(20); primary("Try again", enabled: true, action: gate.retry); gap(12); secondary("Start over", action: gate.startOver) }
        case .rejected(let message): heading("Almost there.", message); gap(20); primary("Try again", enabled: true, action: gate.retryFromRejected)
        case .conflict(let device, let available):
            heading("Bring your island here?", "Island is on \(device ?? "another Mac"). Move it to this Mac with a fresh post?"); gap(20)
            primary("Try again", enabled: !Self.inCooldown(available), action: gate.tryMoveHere); gap(12); secondary("Cancel", action: close)
        }
    }
    private var wakingBar: some View {
        Group {
            if reduceMotion { ProgressView().progressViewStyle(.linear).frame(height: 4) }
            else {
                // Fixed track width (440 - 2 x 24) and a scoped animation: a GeometryReader plus withAnimation(repeatForever)
                // made the whole window content animate during fitContent.
                let track: CGFloat = 392
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.1)).frame(width: track, height: 4)
                    Capsule().fill(GateColor.blue).frame(width: track * 0.38, height: 4)
                        .offset(x: barMoving ? track * 0.62 : 0)
                        .animation(gate.isFixture ? nil : .easeInOut(duration: 1.6).repeatForever(autoreverses: false), value: barMoving)
                }
                .frame(width: track, height: 4)
                .clipShape(Capsule())
                .onAppear { if !gate.isFixture { barMoving = true } }
            }
        }
    }
    private func heading(_ title: String, _ subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 22, weight: .bold)).fixedSize(horizontal: false, vertical: true)
            Text(subtitle).font(.system(size: 13)).foregroundStyle(.white.opacity(0.6)).lineLimit(2).fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
    private func form(linkedIn: Bool, message: String?) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Unlock the island", linkedIn ? "Follow, share a post, then paste its link. Takes about a minute." : "Follow, post, then tell us your handle. Takes about a minute.")
            if let message { gap(8); Text(message).font(.system(size: 11)).foregroundStyle(GateColor.amber) }
            gap(20)
            VStack(spacing: 0) {
                stepRow(1, title: linkedIn ? "Follow Gurusharan on LinkedIn" : "Follow @\(gate.config?.x.handle ?? "gurusharan")", caption: linkedIn ? "Opens LinkedIn in your browser" : "Opens X in your browser", button: "Follow", done: gate.followDone, action: gate.openFollow)
                separator
                stepRow(2, title: linkedIn ? "Share a post" : "Post about ACU", caption: linkedIn ? "Text is ready, paste it and post" : "Text is ready, just tap Post on X", button: "Post", done: gate.postDone, action: gate.openShare)
                separator
                HStack(spacing: 12) {
                    badge(3, done: gate.isHandleValid, active: gate.activeStep == 3)
                    TextField(linkedIn ? "LinkedIn post link" : "@handle or post link", text: $gate.handleText)
                        .textFieldStyle(.plain).font(.system(size: 13)).padding(.horizontal, 8).frame(height: 30)
                        .background(.black.opacity(0.3), in: RoundedRectangle(cornerRadius: 7))
                        .overlay(RoundedRectangle(cornerRadius: 7).stroke(inputFocused ? GateColor.blue : .white.opacity(0.12), lineWidth: 1))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(inputFocused ? GateColor.blue.opacity(0.5) : .clear, lineWidth: 3))
                        .focused($inputFocused).onSubmit { if gate.isHandleValid { gate.submitCheck() } }
                        .accessibilityLabel(linkedIn ? "Step 3 of 3, your LinkedIn post link" : "Step 3 of 3, your X handle or post link")
                }.padding(.horizontal, 16).frame(minHeight: 56)
            }.background(GateColor.card, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.06)))
            if !gate.handleText.isEmpty && !gate.isHandleValid {
                gap(6); Text(linkedIn ? "That doesn’t look like a LinkedIn post link." : "That doesn’t look like an X handle or post link.").font(.system(size: 11)).foregroundStyle(GateColor.red)
            }
            gap(16); wallConsent(unlocked: false, linkedIn: linkedIn); gap(16)
            primary("Unlock", enabled: gate.isHandleValid, isDefault: gate.activeStep == 3, action: gate.submitCheck)
            gap(12); secondary(linkedIn ? "Use X instead" : "No X account? Use LinkedIn", action: linkedIn ? gate.useXRoute : gate.useLinkedInRoute)
        }
    }
    private var separator: some View { Rectangle().fill(.white.opacity(0.08)).frame(height: 1).padding(.leading, 52) }
    private func badge(_ n: Int, done: Bool, active: Bool) -> some View {
        ZStack {
            Circle().fill(done ? GateColor.green : .clear)
            Circle().stroke(done ? .clear : (active ? GateColor.blue : .white.opacity(0.3)), lineWidth: 1.5)
            if done { Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)) }
            else { Text("\(n)").font(.system(size: 12, weight: .semibold)).foregroundStyle(active ? GateColor.blue : .white.opacity(0.7)) }
        }.frame(width: 24, height: 24)
    }
    private func stepRow(_ number: Int, title: String, caption: String, button: String, done: Bool, action: @escaping () -> Void) -> some View {
        let active = gate.activeStep == number
        return HStack(spacing: 12) {
            badge(number, done: done, active: active)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                Text(caption).font(.system(size: 11)).foregroundStyle(.white.opacity(0.55)).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            Group {
                if active && !done { stepButton(button, active: true, done: false, action: action).keyboardShortcut(.defaultAction) }
                else { stepButton(done ? "Open again" : button, active: false, done: done, action: action) }
            }
        }.padding(.horizontal, 16).frame(minHeight: 56)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Step \(number) of 3, \(title), \(done ? "done" : active ? "current step" : "not done")")
            .accessibilityAction(named: Text(done ? "Open again" : button), action)
    }
    private func stepButton(_ title: String, active: Bool, done: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { Text(title).font(.system(size: done ? 12 : 13, weight: done ? .regular : .semibold)).foregroundStyle(done ? .white.opacity(0.55) : .white).frame(width: 84, height: 28).background(done ? .clear : active ? GateColor.blue : .white.opacity(0.1), in: RoundedRectangle(cornerRadius: 7)) }
            .buttonStyle(.plain).gateFocusEffectDisabled()
    }
    private func primary(_ title: String, enabled: Bool, isDefault: Bool = true, action: @escaping () -> Void) -> some View {
        let button = Button(action: action) { Text(title).font(.system(size: 14, weight: .semibold)).foregroundStyle(enabled ? .white : .white.opacity(0.35)).frame(maxWidth: .infinity).frame(height: 36).background(enabled ? GateColor.blue : .white.opacity(0.08), in: RoundedRectangle(cornerRadius: 9)) }.buttonStyle(.plain).disabled(!enabled)
        return Group { if isDefault && enabled { button.keyboardShortcut(.defaultAction) } else { button } }
    }
    private func secondary(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Text(title).font(.system(size: 12)).foregroundStyle(.white.opacity(0.6)).underline(linkHover).frame(maxWidth: .infinity) }.buttonStyle(.plain).onHover { linkHover = $0 }
    }
    private func wallConsent(unlocked: Bool, linkedIn: Bool) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Toggle("Show me on the Who’s using it wall", isOn: Binding(get: { gate.showOnWall }, set: { if unlocked { gate.setWallVisibility($0) } else { gate.setLocalWallConsent($0) } })).toggleStyle(.checkbox).font(.system(size: 13)).disabled(unlocked && gate.wallUpdating)
            Text(linkedIn ? "Your name and photo on the Agent Plugins site. Turn off any time." : "Your handle and avatar on the Agent Plugins site. Turn off any time.").font(.system(size: 11)).foregroundStyle(.white.opacity(0.55)).padding(.leading, 22).fixedSize(horizontal: false, vertical: true)
            if let error = gate.wallError { Text(error).foregroundStyle(GateColor.amber).font(.system(size: 11)) }
        }
    }
    private func checking(follow: IslandGateCheckStatus, post: IslandGateCheckStatus, notFound: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if notFound { Image(systemName: "exclamationmark.magnifyingglass").font(.system(size: 28)).foregroundStyle(GateColor.amber); gap(12) }
            heading(notFound ? "We couldn’t find your post yet" : "Checking your post", notFound ? "Make sure it’s public and posted from \(gate.handleText)." : "Usually within the hour. You can close this window. The island turns on by itself.")
            gap(20)
            VStack(spacing: 0) {
                statusRow(1, title: "Following @\(gate.config?.x.handle ?? "gurusharan")", status: follow, notFound: false)
                separator
                statusRow(2, title: "Post about ACU", status: post, notFound: notFound)
            }.background(GateColor.card, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.06)))
            gap(12)
            HStack(spacing: 3) { Text(gate.handleText).foregroundStyle(.white.opacity(0.55)); Text("·").foregroundStyle(.white.opacity(0.55)); Button("Change", action: gate.changeInput).buttonStyle(.plain).foregroundStyle(GateColor.blue) }.font(.system(size: 12))
            gap(16)
            if notFound { primary("Check again", enabled: true, action: gate.retry) }
            else { Button(action: close) { Text("Close").font(.system(size: 14, weight: .semibold)).frame(maxWidth: .infinity).frame(height: 36).background(.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 9)) }.buttonStyle(.plain) }
        }
    }
    private func statusRow(_ n: Int, title: String, status: IslandGateCheckStatus, notFound: Bool) -> some View {
        HStack(spacing: 12) {
            if notFound { Image(systemName: "minus.circle").font(.system(size: 22)).foregroundStyle(GateColor.amber).frame(width: 24, height: 24) }
            else { badge(n, done: status == .confirmed, active: status != .confirmed) }
            Text(title).font(.system(size: 13, weight: .semibold)); Spacer(minLength: 0)
            if notFound { Text("Not found").font(.system(size: 12)).foregroundStyle(GateColor.amber); stepButton("Post again", active: false, done: false, action: gate.openShare) }
            else if status == .confirmed { Label("Found", systemImage: "checkmark").font(.system(size: 12)).foregroundStyle(GateColor.green) }
            else { ProgressView().controlSize(.mini); Text("Checking").font(.system(size: 12)).foregroundStyle(.white.opacity(0.55)) }
        }.padding(.horizontal, 16).frame(minHeight: 44)
    }
    private func unlocked(_ account: IslandGateAccount, exp: Date) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Your island is on", "Your agents now live at the top of this Mac."); gap(20)
            VStack(spacing: 0) {
                HStack(spacing: 12) { Text(account.provider == "x" ? "𝕏" : "in").font(.system(size: 15, weight: .semibold)).frame(width: 20, height: 20); VStack(alignment: .leading, spacing: 2) { Text(account.handle).font(.system(size: 13, weight: .semibold)); Text("Bound to this Mac").font(.system(size: 11)).foregroundStyle(.white.opacity(0.55)) }; Spacer() }.padding(.horizontal, 16).frame(minHeight: 56)
                separator
                HStack(spacing: 12) { Image(systemName: "calendar").font(.system(size: 14)).frame(width: 20, height: 20); Text("Works offline until \(exp.formatted(.dateTime.day().month(.abbreviated).year()))").font(.system(size: 13)); Spacer() }.padding(.horizontal, 16).frame(minHeight: 44)
            }.background(GateColor.card, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.06)))
            gap(16); wallConsent(unlocked: true, linkedIn: account.provider == "linkedin"); gap(16); primary("Done", enabled: true, action: close)
        }
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

private extension View {
    @ViewBuilder func gateFocusEffectDisabled() -> some View {
        if #available(macOS 14, *) { focusEffectDisabled() } else { self }
    }
}

@MainActor
private final class IslandGateHostingView: NSHostingView<IslandGateView> {
    override var safeAreaInsets: NSEdgeInsets { NSEdgeInsetsZero }
}

@MainActor
private final class IslandGateWindow: NSWindow {
    override func cancelOperation(_ sender: Any?) { performClose(sender) }
    /// AppKit selects the whole field when it gains focus; put the insertion point at the end instead (UI & UX fix 4).
    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        let accepted = super.makeFirstResponder(responder)
        if let editor = firstResponder as? NSTextView, editor.isFieldEditor {
            editor.setSelectedRange(NSRange(location: (editor.string as NSString).length, length: 0))
        }
        return accepted
    }
}

@MainActor
final class IslandGateWindowController: NSWindowController, NSWindowDelegate {
    private let gate: IslandGate
    private var changes: AnyCancellable?
    init(gate: IslandGate) {
        self.gate = gate
        let window = IslandGateWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 500), styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Unlock Dynamic Island"
        window.titlebarAppearsTransparent = true; window.titleVisibility = .hidden
        window.titlebarSeparatorStyle = .none
        window.isOpaque = false; window.backgroundColor = NSColor(calibratedWhite: 30.0 / 255.0, alpha: 1)
        window.isMovableByWindowBackground = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false; window.delegate = self
        let hosting = IslandGateHostingView(rootView: IslandGateView(gate: gate, close: { [weak self] in self?.close() }))
        if #available(macOS 13.3, *) { hosting.safeAreaRegions = [] }
        window.contentView = hosting
        gate.onChange.append { [weak self] in
            DispatchQueue.main.async { self?.fitContent() }
        }
        changes = gate.objectWillChange.sink { [weak self] _ in
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
