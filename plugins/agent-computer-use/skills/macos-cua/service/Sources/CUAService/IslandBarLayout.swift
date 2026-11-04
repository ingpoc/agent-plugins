import AppKit

/// Geometry for the Alcove-style island (spec: island-alcove-language.md §1).
///
/// The panel is a FIXED 640×320 transparent, non-activating window pinned to
/// the top centre of the menu-bar display; only the black shape morphs inside
/// it (content-sized compact ×24 r12 ↔ card 340×136…176 r36), so nothing is ever
/// clipped by a resizing window. All rects here are in window coordinates
/// with a top-left origin (SwiftUI), so views and hit-testing share one source.
enum IslandBarLayout {
    // Window
    static let windowSize = CGSize(width: 640, height: 320)
    static let screenMargin: CGFloat = 8

    // Compact
    static let topInset: CGFloat = 3
    static let compactHeight: CGFloat = 24
    static let compactRadius: CGFloat = 12
    static let idleWidth: CGFloat = 74
    static let standardWidth: CGFloat = 136
    static let wideWidth: CGFloat = 200
    static let peekWidth: CGFloat = 244
    /// Duo: detached secondary pill (74) 3 pt right of the primary.
    static let secondaryWidth: CGFloat = 74
    static let duoGap: CGFloat = 3
    /// HUD trailing bar (polish bar §1: 80×4).
    static let hudBarWidth: CGFloat = 80
    static var hudWidth: CGFloat { 2 * slotInset + leadingSlot.width + 8 + hudBarWidth }
    static func hudTrailWidth(shapeWidth: CGFloat) -> CGFloat {
        min(hudBarWidth, max(0, shapeWidth - 2 * slotInset - leadingSlot.width - 8))
    }
    /// File-tray drop target (340×96, 1 pt accent stroke).
    static let dropTargetSize = CGSize(width: 340, height: 96)
    static let slotInset: CGFloat = 8
    static let leadingSlot = CGSize(width: 16, height: 16)
    static let trailingSlot = CGSize(width: 20, height: 16)

    // Card
    static let cardSize = CGSize(width: 340, height: 176)
    static let cardRadius: CGFloat = 36
    static let cardPadding: CGFloat = 16
    static let cardArt: CGFloat = 56
    static let cardArtRadius: CGFloat = 12
    static let cardTextX: CGFloat = 84          // art + 12
    static let cardTrailOrigin = CGPoint(x: 304, y: 16)
    static let cardMetaY: CGFloat = 88
    static let cardControlsY: CGFloat = 128
    static let cardControlsHeight: CGFloat = 32
    static let decisionButton = CGSize(width: 148, height: 32)  // 2×148 + 12 gap = full 308 inner width
    static let decisionGap: CGFloat = 12
    /// Card art block (§5 r2): 56×56 tile, continuous radius 14, white 8%, 30 pt glyph.
    static let artTileRadius: CGFloat = 14
    static let artGlyph: CGFloat = 30
    static let agentRowHeight: CGFloat = 40

    // Circles: plugin marks on the right, harnesses on the left (app icons are never outside)
    static let circleDiameter: CGFloat = 24
    static let circleIcon: CGFloat = 16
    static let circleGap: CGFloat = 6
    static let circleSpacing: CGFloat = 4
    static let maxCircles = 3

    static func gap(hasNotch: Bool) -> CGFloat { hasNotch ? 0 : topInset }

    // MARK: Shapes

    static func shapeSize(compactWidth: CGFloat, card: Bool, cardHeight: CGFloat = cardSize.height) -> CGSize {
        card ? CGSize(width: cardSize.width, height: cardHeight) : CGSize(width: compactWidth, height: compactHeight)
    }

    /// The black shape, horizontally centred, `gap` below the window top.
    static func shapeRect(compactWidth: CGFloat, card: Bool, gap: CGFloat = topInset,
                          cardHeight: CGFloat = cardSize.height) -> CGRect {
        let s = shapeSize(compactWidth: compactWidth, card: card, cardHeight: cardHeight)
        return CGRect(x: (windowSize.width - s.width) / 2, y: gap, width: s.width, height: s.height)
    }

    /// Visible circle slots: up to 3 apps plus one "+N" circle.
    static func circleSlots(count: Int) -> Int {
        count <= maxCircles ? max(count, 0) : maxCircles + 1
    }

    static func overflowCount(count: Int) -> Int { max(0, count - maxCircles) }

    /// Duo secondary pill, detached right of the primary shape (compact only).
    static func secondaryRect(shape: CGRect, width: CGFloat = secondaryWidth) -> CGRect {
        CGRect(x: shape.maxX + duoGap, y: shape.minY, width: width, height: compactHeight)
    }

    /// Plugin-mark circles right of the shape, top-aligned with it (y = gap) in both states.
    static func circleRects(shape: CGRect, count: Int) -> [CGRect] {
        (0..<circleSlots(count: count)).map { i in
            CGRect(x: shape.maxX + circleGap + CGFloat(i) * (circleDiameter + circleSpacing),
                   y: shape.minY, width: circleDiameter, height: circleDiameter)
        }
    }

    /// The single "other agents" circle, mirrored on the left.
    static func agentRect(shape: CGRect) -> CGRect {
        CGRect(x: shape.minX - circleGap - circleDiameter, y: shape.minY,
               width: circleDiameter, height: circleDiameter)
    }

    /// Source circles, right to left from the shape's left edge (one per source, max 3 + "+N").
    /// Compact status dot (Guru, 2026-11-01 19:19 IST; like the iPhone's camera-in-use dot): solid 6 pt,
    /// 9 pt in from the pill's right end, vertically centred (9 pt top and bottom in the 24 pt pill).
    static let compactDotDiameter: CGFloat = 6
    static func compactDotRect(shape: CGRect) -> CGRect {
        CGRect(x: shape.maxX - 9 - compactDotDiameter, y: shape.minY + (compactHeight - compactDotDiameter) / 2,
               width: compactDotDiameter, height: compactDotDiameter)
    }

    /// Inside the pill, ONE rule for every compact state (Guru, 2026-11-04 13:30 IST), voice states included:
    /// ringed target · 24 · app icons (4 apart) · 6 · voice slot · 6 · dot · 9. The 20 pt target ring's right
    /// edge is 26 pt in, so the first icon starts 50 pt in; "+N" takes a fixed 16 pt slot after the third icon.
    /// The pill hugs this content exactly, so the gaps are the same in every state. Any controlled target
    /// (Apple ring for ACU, Comet for comet-control) gets the gap; only ACU carries app icons. Without a
    /// controlled target nothing changes (voice keeps its lead glyph and trailing meter).
    static let operatedIcon: CGFloat = 16
    static let operatedGap: CGFloat = 4
    static let operatedDotGap: CGFloat = 6
    /// 24 pt, no divider: the ONE target gap, ring to apps and ring to voice slot / dot alike (Guru 2026-11-04
    /// 15:52 IST "increse from 20 to 24pt"; 13:30 IST had 20, 0.2.86 16, 0.2.85 12).
    /// A var only so review renders can compare gaps; nothing else writes it.
    nonisolated(unsafe) static var operatedRingGap: CGFloat = 24
    /// Right edge of the 20 pt ring centred on the 16 pt lead slot: 8 + 8 + 10.
    static let ringRight: CGFloat = slotInset + 8 + 10
    static var operatedRingClearance: CGFloat { ringRight + operatedRingGap }
    /// A controlled target with no apps and no voice (e.g. comet-control reading a page): ring · 24 · dot · 9,
    /// 65 pt (Guru 2026-11-04 15:26 IST, "the dynamic island is squeezed": "Use the same gap between the ring
    /// and the dot"; 15:52 IST 24 pt; 0.2.86 had 8 pt, 49 pt). Whatever follows the ring, apps, voice slot or
    /// the dot, starts the same `operatedRingGap` after it.
    static var targetOnlyDotGap: CGFloat { operatedRingGap }
    static var targetOnlyPillWidth: CGFloat { ringRight + targetOnlyDotGap + compactDotDiameter + 9 }
    /// Samantha's voice slot (Guru 14:17 IST, "A plus C's shape cue"; UI & UX spec): one fixed-width slot whose
    /// width depends only on what it shows, never on loudness. Meter: 5 bars × 3 pt, 2 pt gap = 23 pt.
    /// Thinking: 3 dots in 14 pt. Done (controlled only): the 16 pt check.
    static let voiceMeterBars = 5
    static let voiceMeterBarWidth: CGFloat = 3
    static let voiceMeterBarGap: CGFloat = 2
    static let voiceMeterMinHeight: CGFloat = 3
    static let voiceMeterMaxHeight: CGFloat = 14
    static var voiceMeterWidth: CGFloat { CGFloat(voiceMeterBars) * voiceMeterBarWidth + CGFloat(voiceMeterBars - 1) * voiceMeterBarGap }
    static let voiceDotsWidth: CGFloat = 14
    static let voiceDotDiameter: CGFloat = 3.5
    static let voiceDotSpacing: CGFloat = 6
    static let voiceCheckWidth: CGFloat = 16
    static let voiceSlotHeight: CGFloat = 16
    /// Left inset of the voice slot when nothing is controlled.
    static let voiceLeadInset: CGFloat = 9
    /// Apps → voice slot.
    static let appsVoiceGap: CGFloat = 8
    /// Voice slot → dot (the dot's space is always kept, lit or not).
    static let voiceDotGap: CGFloat = 6
    static func voiceSlotWidth(_ c: IslandVoiceSlotContent) -> CGFloat {
        switch c {
        case .listening, .speaking: return voiceMeterWidth
        case .thinking: return voiceDotsWidth
        case .done: return voiceCheckWidth
        }
    }
    /// Nothing controlled: 9 · slot · 6 · dot · 9 (53 pt with the meter, 44 pt thinking).
    static func voicePillWidth(_ c: IslandVoiceSlotContent) -> CGFloat {
        voiceLeadInset + voiceSlotWidth(c) + voiceDotGap + compactDotDiameter + 9
    }
    static func operatedAppsStripWidth(count: Int) -> CGFloat {
        count <= 0 ? 0 : CGFloat(count) * operatedIcon + CGFloat(count - 1) * operatedGap
    }
    static func operatedAppsPillWidth(count: Int) -> CGFloat { controlledPillWidth(apps: count, voice: nil) }
    /// The hug width of a pill with a controlled target, `apps` icon slots ("+N" counts as one) and an
    /// optional voice slot. The one width rule for every compact state.
    static func controlledPillWidth(apps: Int, voice: IslandVoiceSlotContent?) -> CGFloat {
        guard let voice else {
            if apps <= 0 { return targetOnlyPillWidth }
            return operatedRingClearance + operatedAppsStripWidth(count: apps) + operatedDotGap + compactDotDiameter + 9
        }
        var w = operatedRingClearance
        if apps > 0 { w += operatedAppsStripWidth(count: apps) + appsVoiceGap }
        return w + voiceSlotWidth(voice) + voiceDotGap + compactDotDiameter + 9
    }
    /// App icons, left-anchored after the ring gap (they never move while the pill grows on their right).
    static func operatedAppRects(shape: CGRect, count: Int) -> [CGRect] {
        let start = shape.minX + operatedRingClearance
        return (0..<max(0, count)).map { i in
            CGRect(x: start + CGFloat(i) * (operatedIcon + operatedGap), y: shape.minY + (compactHeight - operatedIcon) / 2,
                   width: operatedIcon, height: operatedIcon)
        }
    }
    /// The voice slot: after ring · 24 · apps · 8 when something is controlled, else 9 pt in from the left.
    static func voiceSlotRect(shape: CGRect, apps: Int, controlled: Bool, content: IslandVoiceSlotContent) -> CGRect {
        let x = controlled
            ? shape.minX + operatedRingClearance + (apps > 0 ? operatedAppsStripWidth(count: apps) + appsVoiceGap : 0)
            : shape.minX + voiceLeadInset
        return CGRect(x: x, y: shape.minY + (compactHeight - voiceSlotHeight) / 2, width: voiceSlotWidth(content), height: voiceSlotHeight)
    }

    static func agentRects(shape: CGRect, count: Int) -> [CGRect] {
        (0..<max(0, count)).map { i in
            CGRect(x: shape.minX - circleGap - circleDiameter - CGFloat(i) * (circleDiameter + circleSpacing),
                   y: shape.minY, width: circleDiameter, height: circleDiameter)
        }
    }

    /// The whole pill is the click target: the shape plus the band above it up to the screen edge
    /// (Fitts: a throw to the top edge lands) and 4 pt either side / below.
    static func pillTarget(shape: CGRect) -> CGRect {
        CGRect(x: shape.minX - 4, y: 0, width: shape.width + 8, height: shape.maxY + 4)
    }

    /// Everything clickable — the panel only accepts the mouse inside these.
    static func hitRects(shape: CGRect, circleCount: Int, hasAgents: Bool) -> [CGRect] {
        hitRects(shape: shape, circleCount: circleCount, agentCount: hasAgents ? 1 : 0)
    }

    static func hitRects(shape: CGRect, circleCount: Int, agentCount: Int) -> [CGRect] {
        [pillTarget(shape: shape)] + (circleRects(shape: shape, count: circleCount) + agentRects(shape: shape, count: agentCount)).map { $0.insetBy(dx: -2, dy: -2) }
    }

    // MARK: Window

    /// AppKit frame for the fixed window: top edge on the screen top, centred
    /// on the menu bar, clamped inside the screen.
    static func islandCenterX(screenFrame: CGRect, menubarStrip: CGRect?, auxLeft: CGRect?, auxRight: CGRect?) -> CGFloat {
        if let left = auxLeft, let right = auxRight {
            return (left.maxX + right.minX) / 2
        }
        if let strip = menubarStrip, strip.width >= screenFrame.width * 0.9,
           strip.minX >= screenFrame.minX, strip.maxX <= screenFrame.maxX,
           strip.intersects(screenFrame) {
            return strip.midX
        }
        return screenFrame.midX
    }

    static func needsFrameCorrection(current: CGRect, expected: CGRect) -> Bool { current != expected }

    static func windowFrame(bar: CGRect?, screen: CGRect, auxLeft: CGRect? = nil, auxRight: CGRect? = nil) -> CGRect {
        let w = min(windowSize.width, screen.width)
        let minX = screen.minX
        let maxX = screen.maxX - w
        let center = islandCenterX(screenFrame: screen, menubarStrip: bar, auxLeft: auxLeft, auxRight: auxRight)
        let x = min(max(center - w / 2, minX), max(minX, maxX))
        return CGRect(x: x.rounded(.down), y: screen.maxY - windowSize.height, width: w, height: windowSize.height)
    }
}

/// Choice card grid: text block at y16 (prompt 2 lines + source line), option rows from y80 (32 tall,
/// 8 gap, 2 capsules of 150 per row), footer row (reply field + send / count Submit) last, 16 bottom.
/// At most 4 option slots: > 4 options show 3 + "More in chat". Never scrolls: 176, 220, or a little
/// taller while a reply grows to 3 lines.
enum IslandChoiceLayout {
    static let tallHeight: CGFloat = 220
    static let rowsY: CGFloat = 80
    static let rowHeight: CGFloat = 44
    static let visualRowHeight: CGFloat = 32
    static let rowGap: CGFloat = 8
    static let optionWidth: CGFloat = 150
    static let bottomPadding: CGFloat = IslandBarLayout.cardPadding
    static let maxSlots = 4
    static let lineStep: CGFloat = 17
    static let sendSize: CGFloat = 32
    static let fieldRadius: CGFloat = 16

    static func hasMore(_ a: IslandAgentApproval) -> Bool { a.options.count > maxSlots }
    /// Options drawn as capsules (the 4th slot becomes "More in chat" when there are more).
    static func shownOptions(_ a: IslandAgentApproval) -> [IslandChoiceOption] {
        hasMore(a) ? Array(a.options.prefix(maxSlots - 1)) : a.options
    }
    static func slotCount(_ a: IslandAgentApproval) -> Int { min(a.options.count, maxSlots) }
    static func optionRows(_ a: IslandAgentApproval) -> Int { (slotCount(a) + 1) / 2 }
    static func hasFooter(_ a: IslandAgentApproval) -> Bool { a.allow_text || a.multi }
    static func rowCount(_ a: IslandAgentApproval) -> Int { optionRows(a) + (hasFooter(a) ? 1 : 0) }

    /// 1…3 lines: explicit newlines plus a soft wrap estimate (~34 chars per line in the field).
    static func textLines(_ t: String) -> Int {
        guard !t.isEmpty else { return 1 }
        let n = t.split(separator: "\n", omittingEmptySubsequences: false)
            .reduce(0) { $0 + max(1, Int(ceil(Double($1.count) / 34))) }
        return max(1, min(3, n))
    }

    static func footerHeight(lines: Int) -> CGFloat { rowHeight + CGFloat(max(0, min(3, lines) - 1)) * lineStep }

    static func contentHeight(rows: Int) -> CGFloat {
        let n = CGFloat(rows)
        let gaps = CGFloat(max(0, rows - 1)) * rowGap
        return rowsY + n * rowHeight + gaps + bottomPadding
    }

    static func contentHeight(_ a: IslandAgentApproval, textLines: Int = 1) -> CGFloat {
        contentHeight(rows: rowCount(a)) + (hasFooter(a) && a.allow_text ? footerHeight(lines: textLines) - rowHeight : 0)
    }

    static func cardHeight(_ a: IslandAgentApproval, textLines: Int = 1) -> CGFloat {
        let h = contentHeight(a, textLines: textLines)
        if h <= IslandBarLayout.cardSize.height { return IslandBarLayout.cardSize.height }
        return max(tallHeight, h)
    }

    /// Rows area height (never scrolls — the card is sized to fit).
    static func rowsViewport(_ a: IslandAgentApproval, textLines: Int = 1) -> CGFloat {
        cardHeight(a, textLines: textLines) - rowsY - bottomPadding
    }
}

extension NSScreen {
    /// True on built-in displays with a camera housing (macOS 12+ safe area).
    var islandHasNotch: Bool { safeAreaInsets.top > 0 }
}

/// What Samantha's voice slot shows. Only this decides the slot's width.
enum IslandVoiceSlotContent: String, Equatable, Sendable {
    case listening, thinking, speaking, done
}
