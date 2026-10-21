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

private struct IslandLayoutProbe: ViewModifier {
    let id: String, kind: IslandLayoutKind, truncates: Bool, container: String
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
    func layoutProbe(_ id: String, _ kind: IslandLayoutKind = .text, truncates: Bool = false, in container: String = "shape") -> some View {
        if IslandLayoutRecorder.enabled {
            modifier(IslandLayoutProbe(id: id, kind: kind, truncates: truncates, container: container))
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
                let r = solid[a].drawn.intersection(solid[b].drawn)
                if !r.isNull, r.width > tolerance, r.height > tolerance {
                    out.append("\(state): \(solid[a].id) \(f(solid[a].drawn)) overlaps \(solid[b].id) \(f(solid[b].drawn))")
                }
            }
        }
        return out
    }
}
