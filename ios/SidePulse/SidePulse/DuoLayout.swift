import SwiftUI

// Everything the iPhone Duo adds lives here, behind two gates.
//
// `#available(iOS 27.1, *)` is the runtime one. It is not enough on its own:
// a symbol that only exists in the 27.1 SDK does not compile at all against an
// older one, and the released Xcode is what builds for TestFlight while 27.1
// is in beta. So the compile-time gate is the SwiftUI module version —
// 8.0.84 ships in the iOS 27.0 SDK, 8.0.85 in 27.1 — and every call site below
// keeps a fallback that is merely ordinary, never a second design.

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
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            modifier(DuoFoldReader(fold: fold))
        } else {
            self
        }
#else
        self
#endif
    }

    /// 0 while the phone is shut, 1 once it is open, following the hinge in
    /// between. Drive effects with it — never layout — and expect a constant 1
    /// on every phone that has no hinge.
    @ViewBuilder
    func duoHingeOpenness(_ openness: Binding<Double>) -> some View {
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            modifier(DuoHingeOpenness(openness: openness))
        } else {
            self
        }
#else
        self
#endif
    }
}

#if canImport(SwiftUI, _version: 8.0.85)
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
#endif

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
/// is half-folded. Without arrangements the panes simply sit next to each
/// other when the width allows and stack when it does not.
struct DuoSplit<Primary: View, Secondary: View>: View {
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @ViewBuilder var primary: Primary
    @ViewBuilder var secondary: Secondary
    @State private var fold = DuoFold()
    @State private var isSideBySide = false

    /// Open flat, the two panes would touch in the middle of the widescreen
    /// display. Half-folded, the crease already keeps them apart. It is safe
    /// area, not padding, so each pane's background still runs to its edge.
    private var gutter: CGFloat { isSideBySide && !fold.isActive ? 16 : 0 }

    var body: some View {
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            ArrangementView {
                primary.safeAreaPadding(.trailing, gutter)
            } secondary: {
                secondary.safeAreaPadding(.leading, gutter)
            }
            .arrangementViewStyle(.split)
            // Every pane in the app is a grouped list; the strip the crease
            // keeps free between them should not show the window behind.
            .background(Color(.systemGroupedBackground).ignoresSafeArea())
            .duoFold($fold)
            .onGeometryChange(for: Bool.self) { $0.size.width > $0.size.height } action: { isSideBySide = $0 }
        } else {
            stacked
        }
#else
        stacked
#endif
    }

    @ViewBuilder
    private var stacked: some View {
        if horizontalSizeClass == .regular {
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
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            modifier(DuoCompactTitle(force: force))
        } else {
            navigationBarTitleDisplayMode(force ? .inline : .automatic)
        }
#else
        navigationBarTitleDisplayMode(force ? .inline : .automatic)
#endif
    }

    /// A screen people watch rather than navigate: its own controls matter
    /// more than the tab bar when the vertical strip runs short.
    @ViewBuilder
    func duoPrefersToolbarItems() -> some View {
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            toolbarVerticalCompressionBehavior(.prefersToolbarItems)
        } else {
            self
        }
#else
        self
#endif
    }

    /// A sheet with a single button has nothing to fill a vertical bar with;
    /// keep its button where sheets have always kept it.
    @ViewBuilder
    func duoHorizontalSheetBar() -> some View {
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            toolbarVerticalBehavior(.disabled)
        } else {
            self
        }
#else
        self
#endif
    }

}

#if canImport(SwiftUI, _version: 8.0.85)
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
#endif

/// The one overflow the app has. On the iPhone Duo it is the system's own,
/// which owns the ellipsis in the vertical strip; elsewhere it is an ordinary
/// menu in the same place.
struct DuoOverflow<Content: View>: ToolbarContent {
    @ViewBuilder var content: Content

    var body: some ToolbarContent {
#if canImport(SwiftUI, _version: 8.0.85)
        if #available(iOS 27.1, *) {
            ToolbarOverflowMenu { content }
        } else {
            plainMenu
        }
#else
        plainMenu
#endif
    }

    private var plainMenu: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                content
            } label: {
                Label("More", systemImage: "ellipsis")
            }
        }
    }
}
