import SwiftUI

// MARK: - The fold

/// What the folding region is doing right now. A phone that cannot fold
/// reports `exists == false`, and every rule below then keeps today's layout.
struct DuoFold: Equatable {
    /// True on a folding display, even while it lies flat.
    var exists = false
    /// True only while the phone is folded far enough to divide the display.
    var isActive = false
    /// A crease running across the width splits the display into an upper and
    /// a lower half — the phone standing on a desk like a little laptop.
    var isHorizontal = false

    /// The signature desk pose: upper half readable from across the room,
    /// lower half within reach.
    var isTabletop: Bool { isActive && isHorizontal }
    /// Half-folded in landscape: the crease is a left/right divider.
    var isBook: Bool { isActive && !isHorizontal }
}

extension View {
    /// Reports the folding region without taking part in layout. Nothing
    /// happens on a phone that does not fold.
    @ViewBuilder
    func duoFold(_ fold: Binding<DuoFold>) -> some View {
        if #available(iOS 27.1, *) {
            modifier(DuoFoldReader(fold: fold))
        } else {
            self
        }
    }

    /// 0 while the phone is shut, 1 once it is open, following the hinge in
    /// between. Drive effects with it — never layout — and expect a constant 1
    /// on every phone that has no hinge.
    @ViewBuilder
    func duoHingeOpenness(_ openness: Binding<Double>) -> some View {
        if #available(iOS 27.1, *) {
            modifier(DuoHingeOpenness(openness: openness))
        } else {
            self
        }
    }
}

@available(iOS 27.1, *)
private struct DuoFoldReader: ViewModifier {
    @Binding var fold: DuoFold

    func body(content: Content) -> some View {
        content.onGeometryChange(for: DuoFold.self) { proxy in
            // `.includeInactive` also answers "could this display ever fold?",
            // which is what the even column counts key off.
            guard let region = proxy.reservedRegions(
                kind: .division,
                options: [.includeInactive]
            ).first else {
                return DuoFold()
            }
            return DuoFold(
                exists: true,
                isActive: region.isActive,
                isHorizontal: region.frame.width >= region.frame.height
            )
        } action: { fold = $0 }
    }
}

@available(iOS 27.1, *)
private struct DuoHingeOpenness: ViewModifier {
    @Binding var openness: Double

    func body(content: Content) -> some View {
        content.onHingeChange { _, context in
            guard let hinge = context.hinge else {
                openness = 1
                return
            }
            withAnimation(.easeOut(duration: 0.25)) {
                openness = min(1, max(0, hinge.angle.degrees / 180))
            }
        }
    }
}

/// Column count for a grid the crease may run through: as many as fit, but an
/// even number wherever a fold exists, so no column straddles it.
func duoColumnCount(
    availableWidth: CGFloat,
    minimumItemWidth: CGFloat,
    spacing: CGFloat,
    fold: DuoFold
) -> Int {
    let fitting = Int((availableWidth + spacing) / (minimumItemWidth + spacing))
    let count = max(1, fitting)
    guard fold.exists, count > 1 else { return count }
    return count - (count % 2)
}

// MARK: - Arrangement

/// Two panes that follow the hardware: side by side when the container is
/// wide, stacked when it is tall, and divided along the crease once the phone
/// is half-folded. iOS 27.0 has no arrangements, so there the panes simply sit
/// next to each other when the width allows and stack when it does not.
struct DuoSplit<Primary: View, Secondary: View>: View {
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @ViewBuilder var primary: Primary
    @ViewBuilder var secondary: Secondary

    var body: some View {
        if #available(iOS 27.1, *) {
            ArrangementView {
                primary
            } secondary: {
                secondary
            }
            .arrangementViewStyle(.split)
        } else if horizontalSizeClass == .regular {
            HStack(spacing: 0) {
                primary
                Divider()
                secondary
            }
        } else {
            VStack(spacing: 0) {
                primary
                Divider()
                secondary
            }
        }
    }
}

// MARK: - Bars

extension View {
    /// An inline navigation title wherever the system has moved the bars into
    /// the vertical strip at the side; `force` adds the tabletop pose, where
    /// the bars stay horizontal but a large title would eat the part of the
    /// upper half the board needs.
    @ViewBuilder
    func duoCompactTitle(force: Bool = false) -> some View {
        if #available(iOS 27.1, *) {
            modifier(DuoCompactTitle(force: force))
        } else {
            self
        }
    }

    /// A screen people watch rather than navigate: its own controls matter
    /// more than the tab bar when the vertical strip runs short.
    @ViewBuilder
    func duoPrefersToolbarItems() -> some View {
        if #available(iOS 27.1, *) {
            toolbarVerticalCompressionBehavior(.prefersToolbarItems)
        } else {
            self
        }
    }

    /// A sheet with a single button has nothing to fill a vertical bar with;
    /// keep its button where sheets have always kept it.
    @ViewBuilder
    func duoHorizontalSheetBar() -> some View {
        if #available(iOS 27.1, *) {
            toolbarVerticalBehavior(.disabled)
        } else {
            self
        }
    }

    /// Apple's default is the vertical strip, and that is what ships.
    /// `-DuoStrip horizontal` exists only so the two can be photographed side
    /// by side for a decision.
    @ViewBuilder
    func duoStripBehavior() -> some View {
#if DEBUG && SIDEPULSE_MAIN_APP
        if #available(iOS 27.1, *), DuoStrip.prefersHorizontal {
            toolbarVerticalBehavior(.disabled)
        } else {
            self
        }
#else
        self
#endif
    }
}

@available(iOS 27.1, *)
private struct DuoCompactTitle: ViewModifier {
    let force: Bool
    @Environment(\.toolbarVerticalEdge) private var verticalEdge

    func body(content: Content) -> some View {
        content.navigationBarTitleDisplayMode(
            force || verticalEdge != nil ? .inline : .automatic
        )
    }
}

#if DEBUG
enum DuoStrip {
    static let prefersHorizontal: Bool = {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: "-DuoStrip"), index + 1 < arguments.count
        else { return false }
        return arguments[index + 1].lowercased() == "horizontal"
    }()
}
#endif
