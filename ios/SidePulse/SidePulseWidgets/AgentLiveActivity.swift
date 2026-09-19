import ActivityKit
import SwiftUI
import WidgetKit

// MARK: - Status grouping

/// The four states that matter at a glance, in attention order.
private struct ModeGroups {
    var blocked = 0
    var waiting = 0
    var working = 0
    var done = 0
    /// Finished sessions the user has not opened yet.
    var unreadDone = 0

    init(agents: [AgentActivityAttributes.AgentRow]) {
        for agent in agents {
            switch agent.mode {
            case "blocked_error": blocked += 1
            case "waiting_for_input": waiting += 1
            case "completed":
                done += 1
                if agent.unread == true { unreadDone += 1 }
            case "idle_ready": break
            default: working += 1
            }
        }
    }

    var headline: (count: Int, color: Color) {
        if blocked > 0 { return (blocked, .statusBlocked) }
        if waiting > 0 { return (waiting, .statusWaiting) }
        if working > 0 { return (working, .statusWorking) }
        return (done, .statusDone)
    }

    /// Icon for the most urgent state: warning when blocked, a question
    /// bubble when a session wants input, a bolt while working, a check
    /// when everything is done.
    var symbol: (name: String, color: Color) {
        if blocked > 0 { return ("exclamationmark.triangle.fill", .statusBlocked) }
        if waiting > 0 { return ("questionmark.bubble.fill", .statusWaiting) }
        if working > 0 { return ("bolt.fill", .statusWorking) }
        return ("checkmark.circle.fill", .statusDone)
    }
}

private extension Color {
    static let statusWorking = Color.blue
    static let statusWaiting = Color.orange
    static let statusBlocked = Color.red
    static let statusDone = Color.green

    static func forMode(_ mode: String) -> Color {
        AgentModeStyle.tint(mode)
    }
}

// MARK: - Widget

struct AgentLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AgentActivityAttributes.self) { context in
            LockScreenView(context: context)
                .activityBackgroundTint(Color(red: 0.07, green: 0.07, blue: 0.09))
                .activitySystemActionForegroundColor(.white)
                .widgetURL(URL(string: "sidepulse://agents"))
        } dynamicIsland: { context in
            let groups = ModeGroups(agents: context.state.agents)
            return DynamicIsland {
                // Where the island is a narrow vertical strip — the iPhone
                // Duo's outer display — the side regions have no width to
                // spend, so the whole card is built in the bottom region and
                // stacks instead.
                DynamicIslandExpandedRegion(.leading) {
                    IslandExpandedLeading(groups: groups, host: context.attributes.hostLabel)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    IslandExpandedTrailing(groups: groups)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    IslandExpandedBottom(
                        groups: groups,
                        host: context.attributes.hostLabel,
                        agents: context.state.agents
                    )
                    .widgetURL(URL(string: "sidepulse://agents"))
                }
            } compactLeading: {
                IslandCompactLeading(groups: groups, activeCount: context.state.activeCount)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } compactTrailing: {
                IslandCompactTrailing(groups: groups)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } minimal: {
                IslandMinimal(groups: groups, activeCount: context.state.activeCount)
                    .widgetURL(URL(string: "sidepulse://agents"))
            }
        }
        .supplementalActivityFamilies([.small])
    }
}


// MARK: - Dynamic Island pieces

/// Compact leading: the most urgent state's glyph plus the active count —
/// what is happening right now.
private struct IslandCompactLeading: View {
    let groups: ModeGroups
    let activeCount: Int
    @Environment(\.isDynamicIslandLimitedInWidth) private var isNarrow

    var body: some View {
        // A side strip has room for one glyph, nothing beside it.
        if isNarrow {
            Image(systemName: groups.symbol.name)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(groups.symbol.color)
        } else {
            HStack(spacing: 3) {
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 12, weight: .semibold))
                if activeCount > 0 {
                    Text("\(activeCount)")
                        .font(.system(size: 14, weight: .bold, design: .rounded))
                        .contentTransition(.numericText())
                }
            }
            .foregroundStyle(groups.symbol.color)
        }
    }
}

/// Compact trailing: recently finished sessions as a green check + count,
/// so "one working, three done" reads at a glance without expanding.
private struct IslandCompactTrailing: View {
    let groups: ModeGroups
    @Environment(\.isDynamicIslandLimitedInWidth) private var isNarrow

    var body: some View {
        if isNarrow {
            // One number, no icon beside it: the count of sessions that have
            // stopped and want a person.
            narrowBody
        } else {
            wideBody
        }
    }

    @ViewBuilder
    private var narrowBody: some View {
        let waiting = groups.blocked + groups.waiting + groups.unreadDone
        if waiting > 0 {
            Text("\(waiting)")
                .font(.system(size: 15, weight: .bold, design: .rounded))
                .foregroundStyle(groups.symbol.color)
                .contentTransition(.numericText())
        } else {
            Circle()
                .fill(Color.white.opacity(0.18))
                .frame(width: 6, height: 6)
        }
    }

    private var wideBody: some View {
        // Never hand WidgetKit an empty compact region: with nothing to
        // report the island fails to present at all, leaving only the Lock
        // Screen card. The count still means unread and nothing else.
        Group {
            if groups.unreadDone > 0 {
                HStack(spacing: 3) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 11, weight: .semibold))
                    Text("\(groups.unreadDone)")
                        .font(.system(size: 14, weight: .bold, design: .rounded))
                        .contentTransition(.numericText())
                }
                .foregroundStyle(Color.statusDone)
            } else if groups.done > 0 {
                // Finished, all seen: a quiet check, no number.
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.statusDone.opacity(0.45))
            } else {
                Circle()
                    .fill(Color.white.opacity(0.18))
                    .frame(width: 6, height: 6)
            }
        }
    }
}

/// Minimal (another Live Activity shares the island): one tinted number —
/// the active count while anything runs, otherwise the finished count in
/// green, so "all done" never reads as a lonely zero.
private struct IslandMinimal: View {
    let groups: ModeGroups
    let activeCount: Int
    @Environment(\.isDynamicIslandLimitedInWidth) private var isNarrow

    var body: some View {
        // "3/2" needs width the side strip does not have; there the most
        // urgent count stands alone.
        let done = isNarrow ? 0 : groups.unreadDone
        ZStack {
            if activeCount > 0 && done > 0 {
                // Both at once: "3/2" — active in the state color, finished
                // in green — on a neutral tint so neither color dominates.
                Circle()
                    .fill(Color.white.opacity(0.14))
                (Text("\(activeCount)").foregroundColor(groups.symbol.color)
                    + Text("/").foregroundColor(Color.white.opacity(0.45))
                    + Text("\(done)").foregroundColor(Color.statusDone))
                    .font(.system(size: 10, weight: .bold, design: .rounded))
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
            } else if activeCount > 0 {
                Circle()
                    .fill(groups.symbol.color.opacity(0.22))
                Text("\(activeCount)")
                    .font(.system(size: 12, weight: .bold, design: .rounded))
                    .foregroundStyle(groups.symbol.color)
                    .minimumScaleFactor(0.6)
            } else if done > 0 {
                Circle()
                    .fill(Color.statusDone.opacity(0.22))
                Text("\(done)")
                    .font(.system(size: 12, weight: .bold, design: .rounded))
                    .foregroundStyle(Color.statusDone)
                    .minimumScaleFactor(0.6)
            } else {
                Circle()
                    .fill(groups.symbol.color.opacity(0.22))
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(groups.symbol.color)
            }
        }
    }
}

/// As many whole rows as the container really has room for, and an honest
/// "+n more" for the rest. Nothing here assumes an island height: the first
/// candidate that fits wins, so the same code serves the phone's wide island,
/// the narrow strip on the iPhone Duo's outer display and the Lock Screen.
private struct FittedAgentRows: View {
    let agents: [AgentActivityAttributes.AgentRow]
    var spacing: CGFloat = 4
    var overflowFont: Font = .caption2
    @ViewBuilder var row: (AgentActivityAttributes.AgentRow) -> AnyView

    var body: some View {
        // ViewThatFits picks the first child that fits, so the candidates are
        // spelled out rather than generated: a ForEach would hand it one.
        ViewThatFits(in: .vertical) {
            stack(limit: 12)
            stack(limit: 9)
            stack(limit: 7)
            stack(limit: 6)
            stack(limit: 5)
            stack(limit: 4)
            stack(limit: 3)
            stack(limit: 2)
            stack(limit: 1)
        }
    }

    private func stack(limit: Int) -> some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(agents.prefix(limit)) { agent in
                row(agent)
            }
            if agents.count > limit {
                Text("+\(agents.count - limit) more")
                    .font(overflowFont)
                    .foregroundStyle(.white.opacity(0.5))
            }
        }
    }
}

private struct IslandExpandedLeading: View {
    let groups: ModeGroups
    let host: String
    @Environment(\.isDynamicIslandLimitedInWidth) private var isNarrow

    var body: some View {
        if isNarrow {
            // The strip's side regions are too narrow for a host name; the
            // bottom region carries the whole card there.
            EmptyView()
        } else {
            HStack(spacing: 5) {
                Image(systemName: groups.symbol.name)
                    .font(.caption2)
                    .foregroundStyle(groups.symbol.color)
                Text(host)
                    .font(.caption.bold())
                    .foregroundStyle(.primary)
            }
            .padding(.leading, 2)
        }
    }
}

private struct IslandExpandedTrailing: View {
    let groups: ModeGroups
    @Environment(\.isDynamicIslandLimitedInWidth) private var isNarrow

    var body: some View {
        if isNarrow {
            EmptyView()
        } else {
            StatusChips(groups: groups)
                .padding(.trailing, 2)
        }
    }
}

private struct IslandExpandedBottom: View {
    let groups: ModeGroups
    let host: String
    let agents: [AgentActivityAttributes.AgentRow]
    @Environment(\.isDynamicIslandLimitedInWidth) private var isNarrow

    var body: some View {
        VStack(alignment: .leading, spacing: isNarrow ? 5 : 4) {
            if isNarrow {
                // Header and counts stack instead of sitting left and right.
                HStack(spacing: 4) {
                    Image(systemName: groups.symbol.name)
                        .font(.caption2)
                        .foregroundStyle(groups.symbol.color)
                    Text(host)
                        .font(.caption2.bold())
                        .lineLimit(1)
                    Spacer(minLength: 0)
                }
                StatusChips(groups: groups)
            }

            FittedAgentRows(agents: agents) { agent in
                AnyView(AgentRowView(agent: agent))
            }
        }
    }
}

// MARK: - Pieces

/// Per-state counts as colored digits, most urgent first: a red digit
/// appearing means blocked, orange waiting, cyan working, green finished —
/// so transitions are visible right in the compact island.
/// Colored count chips, only for the most urgent groups that are present.
private struct StatusChips: View {
    let groups: ModeGroups

    var body: some View {
        let parts: [(Int, Color)] = [
            (groups.blocked, .statusBlocked),
            (groups.waiting, .statusWaiting),
            (groups.working, .statusWorking),
            // Green means "awaiting your look": only unread finished count.
            (groups.unreadDone, .statusDone),
        ]

        HStack(spacing: 5) {
            ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                chip(part.0, part.1)
            }
        }
    }

    @ViewBuilder
    private func chip(_ count: Int, _ color: Color) -> some View {
        if count > 0 {
            HStack(spacing: 3) {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
                Text("\(count)")
                    .font(.caption2.bold())
                    .foregroundStyle(color)
                    .contentTransition(.numericText())
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
        }
    }
}


/// "14s", "55m", "2h", "3d" — the system `.relative` style ("55 min, 0 sec")
/// wastes a third of the row. Static, but every push re-renders it and the
/// daemon heartbeats at least every five minutes, so drift stays small.
private func compactAgo(_ finishedAt: Double) -> String {
    let seconds = max(0, Date().timeIntervalSince1970 - finishedAt)
    if seconds < 60 { return "\(Int(seconds))s" }
    if seconds < 3600 { return "\(Int(seconds / 60))m" }
    if seconds < 86400 { return "\(Int(seconds / 3600))h" }
    return "\(Int(seconds / 86400))d"
}

private struct WatchAgentRowView: View {
    let agent: AgentActivityAttributes.AgentRow

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: AgentModeStyle.symbol(agent.mode))
                .font(.system(size: 10))
                .foregroundStyle(Color.forMode(agent.mode))
                .frame(width: 12)
            Text(agent.name)
                .font(.system(size: 11))
                .foregroundStyle(
                    agent.mode == "completed" && agent.unread != true
                        ? Color.white.opacity(0.55) : .white
                )
                .lineLimit(1)
                .layoutPriority(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            Group {
                if let finishedAt = agent.finishedAt {
                    Text(compactAgo(finishedAt))
                        .foregroundStyle(.white.opacity(0.5))
                } else {
                    Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                        .fontWeight(.medium)
                        .foregroundStyle(Color.forMode(agent.mode))
                }
            }
            .font(.system(size: 9))
            .lineLimit(1)
            .layoutPriority(2)
        }
    }
}

private struct AgentRowView: View {
    let agent: AgentActivityAttributes.AgentRow

    private var isDone: Bool { agent.mode == "completed" }
    private var isUnread: Bool { isDone && agent.unread == true }

    var body: some View {
        // One line per session: density beats detail here — more sessions
        // fit the card, and summaries are short enough to survive one line.
        HStack(spacing: 7) {
            Image(systemName: AgentModeStyle.symbol(agent.mode))
                .font(.system(size: 11))
                .foregroundStyle(Color.forMode(agent.mode))
                .frame(width: 14)
            if let provider = agent.provider {
                Text(provider.capitalized)
                    .font(.system(size: 9, weight: .semibold))
                    .lineLimit(1)
                    .foregroundStyle(.white.opacity(0.75))
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.white.opacity(0.12), in: Capsule())
            }
            Text(agent.name)
                .font(isUnread ? .caption.weight(.semibold) : .caption)
                .foregroundStyle(isDone && !isUnread ? Color.white.opacity(0.55) : .white)
                .lineLimit(1)
                .layoutPriority(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            if isUnread {
                // Unread marker: this finished session awaits a look.
                Circle()
                    .fill(Color.statusDone)
                    .frame(width: 6, height: 6)
            }
            Group {
                if let finishedAt = agent.finishedAt {
                    Text(compactAgo(finishedAt))
                        .foregroundStyle(.white.opacity(0.5))
                } else {
                    Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                        .fontWeight(.medium)
                        .foregroundStyle(Color.forMode(agent.mode))
                }
            }
            .font(.caption2)
            .lineLimit(1)
            // Natural width, offered space BEFORE the greedy name — the
            // name's ellipsis then lands right beside the label instead of
            // leaving a reserved-column gap. Server-side caps bound the
            // detail text, so the label can't eat the row.
            .layoutPriority(2)
        }
        .padding(.horizontal, 5)
        .padding(.vertical, 2)
        .background(
            isUnread ? Color.statusDone.opacity(0.16) : Color.clear,
            in: RoundedRectangle(cornerRadius: 6, style: .continuous)
        )
    }
}

private struct LockScreenView: View {
    let context: ActivityViewContext<AgentActivityAttributes>
    @Environment(\.activityFamily) private var activityFamily

    var body: some View {
        let groups = ModeGroups(agents: context.state.agents)
        if activityFamily == .small {
            watchBody(groups: groups)
        } else {
            phoneBody(groups: groups)
        }
    }

    /// Smart Stack on the watch: the card's height is fixed and small, so
    /// two compact rows at most — the header chips carry the full counts.
    private func watchBody(groups: ModeGroups) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 10))
                    .foregroundStyle(groups.symbol.color)
                Text(context.attributes.hostLabel)
                    .font(.system(size: 11, weight: .bold))
                    .lineLimit(1)
                Spacer(minLength: 3)
                StatusChips(groups: groups)
                    .fixedSize()
            }
            FittedAgentRows(agents: context.state.agents, spacing: 2, overflowFont: .system(size: 9)) { agent in
                AnyView(WatchAgentRowView(agent: agent))
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
    }

    private func phoneBody(groups: ModeGroups) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                HStack(spacing: 5) {
                    Image(systemName: groups.symbol.name)
                        .font(.caption)
                        .foregroundStyle(groups.symbol.color)
                    Text(context.attributes.hostLabel)
                        .font(.subheadline.bold())
                        .foregroundStyle(.white)
                }
                Spacer()
                StatusChips(groups: groups)
            }

            if context.state.agents.isEmpty {
                Text("All quiet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                FittedAgentRows(agents: context.state.agents, spacing: 5) { agent in
                    AnyView(AgentRowView(agent: agent))
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}
