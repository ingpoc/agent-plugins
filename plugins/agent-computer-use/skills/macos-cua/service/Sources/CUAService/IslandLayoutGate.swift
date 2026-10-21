import AppKit
import SwiftUI

// Deterministic layout gate (tests + check.py). Probes read real frames from the views; they exist in the
// view tree only when `IslandLayoutRecorder.enabled` is set (tests), so production pays nothing.

enum IslandLayoutKind: String { case text, glyph, control, ring }

struct IslandLayoutItem: Equatable {
    var id: String
    var kind: IslandLayoutKind
    var frame: CGRect          // laid-out frame, island window coordinates
    var ideal: CGSize          // unconstrained (fixedSize) size: what the glyphs need
    var truncates: Bool        // ellipsis truncation is intentional for this text
    var container: String
    /// What actually gets drawn: a non-truncating text wider than its frame draws past it.
    var drawn: CGRect {
        guard kind == .text, !truncates else { return frame }
        return CGRect(x: frame.minX, y: frame.minY, width: max(frame.width, ideal.width), height: max(frame.height, ideal.height))
    }
}

struct IslandLayoutContainer: Equatable {
    var name: String
    var rect: CGRect
    var inset: CGSize          // minimum inset for content (rings only need to stay inside `rect`)
}

final class IslandLayoutRecorder: @unchecked Sendable {
    static let shared = IslandLayoutRecorder()
    static let space = "islandWindow"
    /// Read when views build their body; set before rendering.
    nonisolated(unsafe) static var enabled = false
    private let lock = NSLock()
    private var items: [String: IslandLayoutItem] = [:]
    private var containers: [String: IslandLayoutContainer] = [:]

    func note(_ i: IslandLayoutItem) { lock.lock(); items[i.container + "/" + i.id] = i; lock.unlock() }
    func container(_ c: IslandLayoutContainer) { lock.lock(); containers[c.name] = c; lock.unlock() }
    func reset() { lock.lock(); items.removeAll(); containers.removeAll(); lock.unlock(); Self.idealStore.clear() }
    /// Items with their ideal (fixedSize) sizes merged in; probes without a measured ideal keep their frame size.
    func snapshot() -> ([IslandLayoutItem], [String: IslandLayoutContainer]) {
        lock.lock(); defer { lock.unlock() }
        let merged = items.map { k, v -> IslandLayoutItem in
            var x = v; x.ideal = v.ideal == .zero ? (Self.idealStore.get(k) ?? v.frame.size) : v.ideal; return x
        }
        return (merged.sorted { $0.container + $0.id < $1.container + $1.id }, containers)
    }
}

private struct IslandLayoutContainerKey: EnvironmentKey { static let defaultValue = "shape" }
extension EnvironmentValues {
    /// Container that probes without an explicit `in:` report into (set by `layoutContainer`).
    var islandLayoutContainer: String {
        get { self[IslandLayoutContainerKey.self] }
        set { self[IslandLayoutContainerKey.self] = newValue }
    }
}

private struct IslandLayoutContainerProbe: ViewModifier {
    let name: String, inset: CGSize
    func body(content: Content) -> some View {
        content
            .environment(\.islandLayoutContainer, name)
            .background(GeometryReader { g in
                let _ = IslandLayoutRecorder.shared.container(.init(name: name, rect: g.frame(in: .named(IslandLayoutRecorder.space)), inset: inset))
                Color.clear
            })
    }
}

private struct IslandLayoutProbe: ViewModifier {
    let id: String, kind: IslandLayoutKind, truncates: Bool, explicitContainer: String?
    @Environment(\.islandLayoutContainer) private var envContainer
    private var container: String { explicitContainer ?? envContainer }
    func body(content: Content) -> some View {
        content
            .background(alignment: .topLeading) {
                // Same view, unconstrained: its size is what the glyphs need.
                content.fixedSize().hidden().background(GeometryReader { g in
                    let _ = IslandLayoutRecorder.shared.idealSizes.set(container + "/" + id, g.size)
                    Color.clear
                })
            }
            .background(GeometryReader { g in
                let f = g.frame(in: .named(IslandLayoutRecorder.space))
                let _ = IslandLayoutRecorder.shared.note(.init(id: id, kind: kind, frame: f, ideal: .zero, truncates: truncates, container: container))
                Color.clear
            })
    }
}

final class IslandIdealSizes: @unchecked Sendable {
    private let lock = NSLock(); private var m: [String: CGSize] = [:]
    func set(_ k: String, _ s: CGSize) { lock.lock(); m[k] = s; lock.unlock() }
    func get(_ k: String) -> CGSize? { lock.lock(); defer { lock.unlock() }; return m[k] }
    func clear() { lock.lock(); m.removeAll(); lock.unlock() }
}

extension IslandLayoutRecorder {
    static let idealStore = IslandIdealSizes()
    var idealSizes: IslandIdealSizes { Self.idealStore }
}

extension View {
    /// Register this text / glyph / control with the layout gate (no-op outside tests).
    @ViewBuilder
    func layoutProbe(_ id: String, _ kind: IslandLayoutKind = .text, truncates: Bool = false, in container: String? = nil) -> some View {
        if IslandLayoutRecorder.enabled {
            modifier(IslandLayoutProbe(id: id, kind: kind, truncates: truncates, explicitContainer: container))
        } else {
            self
        }
    }

    /// Register this view's frame as a layout container (probes inside default to it).
    @ViewBuilder
    func layoutContainer(_ name: String, inset: CGSize = CGSize(width: IslandLayoutGate.minInset, height: IslandLayoutGate.minInset)) -> some View {
        if IslandLayoutRecorder.enabled {
            modifier(IslandLayoutContainerProbe(name: name, inset: inset))
        } else {
            self
        }
    }

    /// Register a rect computed from layout constants (views drawn through a scale effect, whose
    /// geometry proxies report the unscaled box).
    func layoutRect(_ id: String, _ kind: IslandLayoutKind, _ r: CGRect, in container: String = "shape") -> some View {
        if IslandLayoutRecorder.enabled {
            IslandLayoutRecorder.shared.note(.init(id: id, kind: kind, frame: r, ideal: r.size, truncates: false, container: container))
        }
        return self
    }
}

enum IslandLayoutGate {
    static let minInset: CGFloat = 8
    static let tolerance: CGFloat = 0.5

    /// Every problem in one rendered state: overlap between sibling texts/glyphs, content clipped by its
    /// own frame or by the pill/card, and content closer than the minimum inset to the container edge.
    static func issues(state: String, items: [IslandLayoutItem], containers: [String: IslandLayoutContainer]) -> [String] {
        var out: [String] = []
        func f(_ r: CGRect) -> String { String(format: "(%.1f,%.1f %.1f×%.1f)", r.minX, r.minY, r.width, r.height) }
        for i in items {
            guard let c = containers[i.container] else { out.append("\(state): \(i.id) has no container '\(i.container)'"); continue }
            if i.kind == .text, !i.truncates, i.ideal.width > i.frame.width + tolerance {
                out.append("\(state): \(i.id) text is cut off: needs \(String(format: "%.1f", i.ideal.width)) pt, frame \(String(format: "%.1f", i.frame.width)) pt")
            }
            if !c.rect.insetBy(dx: -tolerance, dy: -tolerance).contains(i.drawn) {
                out.append("\(state): \(i.id) \(f(i.drawn)) is clipped by \(c.name) \(f(c.rect))")
            } else if i.kind != .ring, !c.rect.insetBy(dx: c.inset.width - tolerance, dy: c.inset.height - tolerance).contains(i.drawn) {
                out.append("\(state): \(i.id) \(f(i.drawn)) is under the \(Int(c.inset.width))/\(Int(c.inset.height)) pt inset of \(c.name) \(f(c.rect))")
            }
        }
        let solid = items.filter { $0.kind != .ring }
        for a in solid.indices {
            for b in solid.indices where b > a && solid[a].container == solid[b].container {
                // A control's own label (popup text inside the popup) is nesting, not overlap.
                let (x, y) = (solid[a], solid[b])
                if (x.kind == .control && x.frame.insetBy(dx: -tolerance, dy: -tolerance).contains(y.drawn))
                    || (y.kind == .control && y.frame.insetBy(dx: -tolerance, dy: -tolerance).contains(x.drawn)) { continue }
                let r = solid[a].drawn.intersection(solid[b].drawn)
                if !r.isNull, r.width > tolerance, r.height > tolerance {
                    out.append("\(state): \(solid[a].id) \(f(solid[a].drawn)) overlaps \(solid[b].id) \(f(solid[b].drawn))")
                }
            }
        }
        return out
    }

    /// Settings form rules. Row containers are named `row|<card>|<kind>|<key>`; inside, `label:<key>` /
    /// `symbol:<key>` start the row and `control:<key>` ends it. Per card: every row starts on one leading
    /// column and ends on one trailing column; single-line rows are `rowHeight` tall (token rows
    /// `tokenRowHeight`) and their label and control share a vertical centre.
    static func formIssues(state: String, items: [IslandLayoutItem], containers: [String: IslandLayoutContainer],
                           rowHeight: CGFloat, tokenRowHeight: CGFloat) -> [String] {
        var out: [String] = []
        let singleLine: Set<String> = ["toggle", "popup", "button", "master"]
        struct Row { var card: String; var kind: String; var key: String; var rect: CGRect }
        let rows = containers.values.compactMap { c -> Row? in
            let p = c.name.split(separator: "|", maxSplits: 3).map(String.init)
            guard p.count == 4, p[0] == "row" else { return nil }
            return Row(card: p[1], kind: p[2], key: p[3], rect: c.rect)
        }.sorted { $0.rect.minY < $1.rect.minY }
        func item(_ prefix: String, _ r: Row) -> IslandLayoutItem? {
            items.first { $0.container == "row|\(r.card)|\(r.kind)|\(r.key)" && $0.id == prefix + ":" + r.key }
        }
        func n(_ v: CGFloat) -> String { String(format: "%.1f", v) }
        for card in Set(rows.map(\.card)).sorted() {
            let cr = rows.filter { $0.card == card }
            let leads = cr.compactMap { r in (item("symbol", r) ?? item("label", r)).map { (r, $0.frame.minX) } }
            if let ref = leads.first {
                for (r, x) in leads.dropFirst() where abs(x - ref.1) > tolerance {
                    out.append("\(state): \(r.key) starts at x \(n(x)), off the card's leading column \(n(ref.1)) (\(ref.0.key))")
                }
            }
            let trails = cr.compactMap { r in item("control", r).map { (r, $0.frame.maxX) } }
            if let ref = trails.first {
                for (r, x) in trails.dropFirst() where abs(x - ref.1) > tolerance {
                    out.append("\(state): \(r.key) control ends at x \(n(x)), off the card's trailing column \(n(ref.1)) (\(ref.0.key))")
                }
            }
            for r in cr {
                if singleLine.contains(r.kind), abs(r.rect.height - rowHeight) > tolerance {
                    out.append("\(state): \(r.key) row is \(n(r.rect.height)) pt tall, form rows are \(n(rowHeight))")
                }
                if r.kind == "token", abs(r.rect.height - tokenRowHeight) > tolerance {
                    out.append("\(state): \(r.key) token row is \(n(r.rect.height)) pt tall, expected \(n(tokenRowHeight))")
                }
                if singleLine.contains(r.kind), let l = item("label", r), let c = item("control", r), abs(l.frame.midY - c.frame.midY) > 1 {
                    out.append("\(state): \(r.key) label centre y \(n(l.frame.midY)) vs control \(n(c.frame.midY))")
                }
            }
        }
        return out
    }
}
