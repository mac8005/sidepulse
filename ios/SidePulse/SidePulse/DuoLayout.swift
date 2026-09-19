import SwiftUI

/// Which two-pane arrangement the agents screen uses where there is room for
/// one. A is what the app ships with; `-DuoVariant B` / `-DuoVariant C`
/// switches in DEBUG builds so the alternatives can be compared side by side.
enum DuoVariant: String {
    /// Sessions list, and the selected session's detail beside it.
    case a = "A"
    /// Sessions list, and the usage / Dot dashboard beside it.
    case b = "B"
    /// Three columns: destinations, sessions, detail.
    case c = "C"

    static let current: DuoVariant = {
#if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        if let index = arguments.firstIndex(of: "-DuoVariant"),
           index + 1 < arguments.count,
           let variant = DuoVariant(rawValue: arguments[index + 1].uppercased()) {
            return variant
        }
#endif
        return .a
    }()
}

/// Columns of the three-column shell (variant C only).
enum DuoDestination: Hashable, CaseIterable {
    case agents
    case dashboard
    case home
    case settings

    var title: String {
        switch self {
        case .agents: return "Mac Agents"
        case .dashboard: return "Usage & Dot"
        case .home: return "Home"
        case .settings: return "Settings"
        }
    }

    var symbol: String {
        switch self {
        case .agents: return "desktopcomputer"
        case .dashboard: return "gauge.with.needle"
        case .home: return "square.grid.2x2"
        case .settings: return "gearshape"
        }
    }
}

/// A sheet's "Done": an icon as well as the title, because the system only
/// moves toolbar items with an icon into the vertical bar strip it uses on the
/// iPhone Duo, and pinned where the SDK knows how, so it never falls into the
/// overflow menu.
struct DoneToolbarItem: ToolbarContent {
    let dismiss: DismissAction

    var body: some ToolbarContent {
        if #available(iOS 27.1, *) {
            ToolbarItem(placement: .topBarPinnedTrailing) {
                button
            }
        } else {
            ToolbarItem(placement: .topBarTrailing) {
                button
            }
        }
    }

    private var button: some View {
        Button {
            dismiss()
        } label: {
            Label("Done", systemImage: "checkmark")
        }
    }
}

/// Two panes that follow the hardware: side by side when the container is
/// wide, stacked when it is tall, and divided along the crease once the phone
/// is half-folded. Before iOS 27.1 there are no folds, so the panes simply sit
/// next to each other on the displays wide enough for both.
struct DuoSplit<Primary: View, Secondary: View>: View {
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
        } else {
            HStack(spacing: 0) {
                primary
                Divider()
                secondary
            }
        }
    }
}

extension View {
    /// An inline navigation title wherever the system has moved the bars into
    /// the vertical strip at the side. A large title costs a fifth of the
    /// iPhone Duo's short outer display, and the strip already names the screen
    /// on its own.
    @ViewBuilder
    func duoCompactTitle() -> some View {
        if #available(iOS 27.1, *) {
            modifier(DuoCompactTitle())
        } else {
            self
        }
    }
}

@available(iOS 27.1, *)
private struct DuoCompactTitle: ViewModifier {
    @Environment(\.toolbarVerticalEdge) private var verticalEdge

    func body(content: Content) -> some View {
        content.navigationBarTitleDisplayMode(verticalEdge == nil ? .automatic : .inline)
    }
}
