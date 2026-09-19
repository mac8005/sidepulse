import SwiftUI

// MARK: - State vocabulary

/// One vocabulary for "what is this session doing". Every state carries a
/// word, a glyph and a colour, so nothing here is ever told by colour alone —
/// which is also what keeps it readable in Increased Contrast and to anyone
/// who does not see the difference between orange and green.
struct AgentState {
    /// Sessions sort into three questions: does anything want me, is anything
    /// running, and what is finished. Everything on the board follows it.
    enum Group: Int, CaseIterable, Comparable {
        case needsYou
        case working
        case settled

        var title: String {
            switch self {
            case .needsYou: return "Needs you"
            case .working: return "Working"
            case .settled: return "Finished & idle"
            }
        }

        var symbol: String {
            switch self {
            case .needsYou: return "hand.raised.fill"
            case .working: return "bolt.fill"
            case .settled: return "checkmark.circle.fill"
            }
        }

        var tint: Color {
            switch self {
            case .needsYou: return .orange
            case .working: return .blue
            case .settled: return .green
            }
        }

        static func < (lhs: Group, rhs: Group) -> Bool { lhs.rawValue < rhs.rawValue }
    }

    var word: String
    var symbol: String
    var tint: Color
    /// A state that is still moving; the glyph breathes while it is.
    var isLive: Bool
    var group: Group

    /// System colours rather than hand-mixed ones: they already carry the
    /// light, dark and Increased Contrast variants Apple ships.
    static func forMode(_ mode: String) -> AgentState {
        switch mode {
        case "blocked_error":
            return AgentState(word: "Blocked", symbol: "exclamationmark.triangle.fill",
                              tint: AgentModeStyle.tint(mode), isLive: false, group: .needsYou)
        case "waiting_for_input":
            return AgentState(word: "Asking", symbol: "questionmark.bubble.fill",
                              tint: AgentModeStyle.tint(mode), isLive: true, group: .needsYou)
        case "working":
            return AgentState(word: "Working", symbol: "bolt.fill",
                              tint: AgentModeStyle.tint(mode), isLive: true, group: .working)
        case "tool_running":
            return AgentState(word: "Running", symbol: "wrench.and.screwdriver.fill",
                              tint: AgentModeStyle.tint(mode), isLive: true, group: .working)
        case "long_task_progress":
            return AgentState(word: "Long task", symbol: "hourglass",
                              tint: AgentModeStyle.tint(mode), isLive: true, group: .working)
        case "completed":
            return AgentState(word: "Finished", symbol: "checkmark.circle.fill",
                              tint: AgentModeStyle.tint(mode), isLive: false, group: .settled)
        case "idle_ready":
            return AgentState(word: "Idle", symbol: "moon.fill",
                              tint: .secondary, isLive: false, group: .settled)
        default:
            return AgentState(word: "Unknown", symbol: "circle.dashed",
                              tint: .secondary, isLive: false, group: .settled)
        }
    }

    /// A finished session nobody has looked at yet is not settled — it is
    /// waiting for a person, and belongs at the top with the rest of them.
    static func of(_ agent: AgentSnapshot.Agent, isUnread: Bool) -> AgentState {
        var state = forMode(agent.mode)
        if isUnread {
            state.word = "New"
            state.tint = .green
            state.group = .needsYou
        }
        return state
    }
}

extension AgentSnapshot.Agent {
    /// "Claude · orchard · Editing 3 files" — the quiet line under the title.
    var subtitle: String {
        [projectName, detail].compactMap { $0 }.joined(separator: " · ")
    }

    /// The last path component is what people call the project; the rest is
    /// noise on a phone-width row.
    var projectName: String? {
        guard let cwd, !cwd.isEmpty else { return nil }
        let name = cwd.split(separator: "/").last.map(String.init)
        return name?.isEmpty == false ? name : cwd
    }

    var providerName: String? {
        provider ?? id.split(separator: ":").first.map(String.init)
    }
}

// MARK: - Grouped sessions

/// The snapshot split into the three questions, each keeping the daemon's own
/// order inside it.
struct AgentGrouping {
    var sections: [(group: AgentState.Group, agents: [AgentSnapshot.Agent])] = []
    var needsYouCount = 0
    var activeCount = 0

    init(agents: [AgentSnapshot.Agent], isUnread: (AgentSnapshot.Agent) -> Bool) {
        var buckets: [AgentState.Group: [AgentSnapshot.Agent]] = [:]
        for agent in agents {
            buckets[AgentState.of(agent, isUnread: isUnread(agent)).group, default: []].append(agent)
        }
        sections = AgentState.Group.allCases.compactMap { group in
            guard let agents = buckets[group], !agents.isEmpty else { return nil }
            return (group, agents)
        }
        needsYouCount = buckets[.needsYou]?.count ?? 0
        activeCount = buckets[.working]?.count ?? 0
    }
}

// MARK: - Row

/// One session. Title on top, a quiet line under it, the state on the right —
/// the same shape everywhere the app lists sessions.
struct SessionRow: View {
    let agent: AgentSnapshot.Agent
    let isUnread: Bool
    /// Short displays give each session one line of title and tighter rows.
    var isDense = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var glyphWidth: CGFloat = 22

    private var state: AgentState { AgentState.of(agent, isUnread: isUnread) }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 11) {
            Image(systemName: state.symbol)
                .font(.body)
                .foregroundStyle(state.tint)
                .symbolEffect(.pulse, isActive: state.isLive && !reduceMotion)
                .frame(width: glyphWidth, alignment: .center)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                // On a short display the title gets the whole first line and
                // the state moves down beside the project, because a truncated
                // title is the one thing that costs you the glance.
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(agent.name)
                        .font(.body)
                        .fontWeight(isUnread ? .semibold : .regular)
                        .lineLimit(isDense ? 1 : 2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if !isDense {
                        stateWord
                    }
                    elapsed
                }

                HStack(spacing: 5) {
                    if let provider = agent.providerName {
                        Text(provider.capitalized)
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                    }
                    Text(agent.subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if isDense {
                        stateWord
                    }
                }
            }
        }
        .padding(.vertical, isDense ? 1 : 5)
        .contentShape(.rect)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(agent.name)
        .accessibilityValue(accessibilityValue)
        .accessibilityHint(accessibilityHint)
    }

    private var stateWord: some View {
        Text(state.word)
            .font(.caption.weight(.semibold))
            .foregroundStyle(state.tint)
            .fixedSize()
    }

    @ViewBuilder
    private var elapsed: some View {
        if let finishedAt = agent.finishedAt {
            Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .fixedSize()
        }
    }

    private var accessibilityValue: String {
        var parts = [state.word]
        if let project = agent.projectName { parts.append("in \(project)") }
        if let detail = agent.detail { parts.append(detail) }
        if isUnread { parts.append("not opened yet") }
        return parts.joined(separator: ", ")
    }

    private var accessibilityHint: String {
        guard let provider = agent.providerName else { return "Opens the session" }
        return "Opens this session in \(provider.capitalized)"
    }
}

// MARK: - Sundries

/// Rounded rectangles that sit inside the hardware's corners look right when
/// their radius follows the same curve; one value, used everywhere.
enum Metrics {
    static let cardRadius: CGFloat = 16
    static let innerRadius: CGFloat = 10
    static let rhythm: CGFloat = 12
}

extension View {
    /// A plain grouped card. Content never sits on glass — that layer belongs
    /// to the system's bars and controls.
    func cardSurface(radius: CGFloat = Metrics.cardRadius) -> some View {
        padding(Metrics.rhythm)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(Color(.secondarySystemGroupedBackground))
            )
    }
}
