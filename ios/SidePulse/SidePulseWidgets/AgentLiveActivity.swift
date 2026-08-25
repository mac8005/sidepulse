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

    init(agents: [AgentActivityAttributes.AgentRow]) {
        for agent in agents {
            switch agent.mode {
            case "blocked_error": blocked += 1
            case "waiting_for_input": waiting += 1
            case "completed": done += 1
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
    static let statusWorking = Color(red: 0.25, green: 0.85, blue: 0.95)
    static let statusWaiting = Color(red: 1.0, green: 0.62, blue: 0.11)
    static let statusBlocked = Color(red: 1.0, green: 0.28, blue: 0.29)
    static let statusDone = Color(red: 0.29, green: 0.87, blue: 0.42)

    static func forMode(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}

// MARK: - Widget

@available(iOSApplicationExtension 16.2, *)
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
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 5) {
                        Image(systemName: groups.symbol.name)
                            .font(.caption2)
                            .foregroundStyle(groups.symbol.color)
                        Text(context.attributes.hostLabel)
                            .font(.caption.bold())
                            .foregroundStyle(.primary)
                    }
                    .padding(.leading, 2)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    StatusChips(groups: groups)
                        .padding(.trailing, 2)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    // The expanded island caps at 160pt; four single-line rows
                    // fit, so recently finished sessions show below the active
                    // ones instead of hiding behind "+n more".
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(context.state.agents.prefix(4)) { agent in
                            AgentRowView(agent: agent)
                        }
                        if context.state.agents.count > 4 {
                            Text("+\(context.state.agents.count - 4) more")
                                .font(.caption2)
                                .foregroundStyle(.white.opacity(0.5))
                        }
                    }
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

    var body: some View {
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

/// Compact trailing: recently finished sessions as a green check + count,
/// so "one working, three done" reads at a glance without expanding.
private struct IslandCompactTrailing: View {
    let groups: ModeGroups

    var body: some View {
        if groups.done > 0 {
            HStack(spacing: 3) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 11, weight: .semibold))
                Text("\(groups.done)")
                    .font(.system(size: 14, weight: .bold, design: .rounded))
                    .contentTransition(.numericText())
            }
            .foregroundStyle(Color.statusDone)
        }
    }
}

/// Minimal (another Live Activity shares the island): one tinted number —
/// the active count while anything runs, otherwise the finished count in
/// green, so "all done" never reads as a lonely zero.
private struct IslandMinimal: View {
    let groups: ModeGroups
    let activeCount: Int

    var body: some View {
        let showsActive = activeCount > 0
        let color = showsActive ? groups.symbol.color : Color.statusDone
        let count = showsActive ? activeCount : groups.done
        ZStack {
            Circle()
                .fill(color.opacity(0.22))
            if count > 0 {
                Text("\(count)")
                    .font(.system(size: 12, weight: .bold, design: .rounded))
                    .foregroundStyle(color)
                    .minimumScaleFactor(0.6)
            } else {
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(color)
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
            (groups.done, .statusDone),
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
                .foregroundStyle(agent.mode == "completed" ? Color.white.opacity(0.7) : .white)
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
                .font(.caption)
                .foregroundStyle(isDone ? Color.white.opacity(0.7) : .white)
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
            .font(.caption2)
            .lineLimit(1)
            // Natural width, offered space BEFORE the greedy name — the
            // name's ellipsis then lands right beside the label instead of
            // leaving a reserved-column gap. Server-side caps bound the
            // detail text, so the label can't eat the row.
            .layoutPriority(2)
        }
    }
}

@available(iOSApplicationExtension 16.2, *)
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
            ForEach(context.state.agents.prefix(3)) { agent in
                WatchAgentRowView(agent: agent)
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
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(context.state.agents.prefix(5)) { agent in
                        AgentRowView(agent: agent)
                    }
                    if context.state.agents.count > 5 {
                        Text("+\(context.state.agents.count - 5) more")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}
