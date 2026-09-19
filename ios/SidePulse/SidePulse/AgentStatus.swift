import SwiftUI

// MARK: - State vocabulary

/// What a session is doing, in a word and a glyph. Colour is deliberately
/// scarce: only the two states that want a person carry one, so a red or an
/// orange line on this screen always means the same thing. Everything else is
/// label grey, which is why the word and the symbol have to do the work.
struct AgentState {
    /// Sessions sort into three questions: does anything want me, is anything
    /// running, and what is done.
    enum Group: Int, CaseIterable, Comparable {
        case needsAttention
        case working
        case finished

        var title: String {
            switch self {
            case .needsAttention: return "Needs attention"
            case .working: return "Working"
            case .finished: return "Finished"
            }
        }

        static func < (lhs: Group, rhs: Group) -> Bool { lhs.rawValue < rhs.rawValue }
    }

    var word: String
    var symbol: String
    /// `nil` means the label colour: the state is information, not an alarm.
    var accent: Color?
    /// A state that is still moving; the glyph breathes while it is.
    var isLive: Bool
    var group: Group

    var tint: Color { accent ?? .secondary }

    static func forMode(_ mode: String) -> AgentState {
        switch mode {
        case "blocked_error":
            return AgentState(word: "Blocked", symbol: "exclamationmark.triangle.fill",
                              accent: .red, isLive: false, group: .needsAttention)
        case "waiting_for_input":
            return AgentState(word: "Needs input", symbol: "questionmark.circle.fill",
                              accent: .orange, isLive: false, group: .needsAttention)
        case "working":
            return AgentState(word: "Working", symbol: "circle.dotted",
                              accent: nil, isLive: true, group: .working)
        case "tool_running":
            return AgentState(word: "Running", symbol: "circle.dotted",
                              accent: nil, isLive: true, group: .working)
        case "long_task_progress":
            return AgentState(word: "Long task", symbol: "circle.dotted",
                              accent: nil, isLive: true, group: .working)
        case "completed":
            return AgentState(word: "Finished", symbol: "checkmark",
                              accent: nil, isLive: false, group: .finished)
        case "idle_ready":
            return AgentState(word: "Idle", symbol: "minus",
                              accent: nil, isLive: false, group: .finished)
        default:
            return AgentState(word: "Unknown", symbol: "questionmark",
                              accent: nil, isLive: false, group: .finished)
        }
    }

    /// A finished session nobody has opened is not done with you yet, so it
    /// keeps the attention group — but it is marked the way Mail marks unread
    /// post, with a dot, not with a colour over the whole row.
    static func of(_ agent: AgentSnapshot.Agent, isUnread: Bool) -> AgentState {
        var state = forMode(agent.mode)
        if isUnread {
            state.group = .needsAttention
        }
        return state
    }
}

extension AgentSnapshot.Agent {
    /// "Claude · orchard · Asked a question" — plain text, no chips.
    var metadata: String {
        [providerName?.capitalized, projectName, detail]
            .compactMap { $0 }
            .joined(separator: " · ")
    }

    /// The last path component is what people call the project.
    var projectName: String? {
        guard let cwd, !cwd.isEmpty else { return nil }
        let name = cwd.split(separator: "/").last.map(String.init)
        return name?.isEmpty == false ? name : cwd
    }

    var providerName: String? {
        provider ?? id.split(separator: ":").first.map(String.init)
    }

    /// The daemon gives every session an emoji as a memory hook. It belongs in
    /// its own slot, not inside the title, so the titles stay pure text and
    /// the column edges line up.
    var leadingEmoji: String? {
        guard let first = name.first,
              first.unicodeScalars.contains(where: {
                  $0.properties.isEmojiPresentation
                      || ($0.properties.isEmoji && $0.value > 0x238C)
              })
        else { return nil }
        return String(first)
    }

    var titleWithoutEmoji: String {
        guard leadingEmoji != nil else { return name }
        return String(name.dropFirst()).trimmingCharacters(in: .whitespaces)
    }
}

// MARK: - Grouped sessions

/// The snapshot split into the three questions, each keeping the daemon's own
/// order inside it.
struct AgentGrouping {
    var sections: [(group: AgentState.Group, agents: [AgentSnapshot.Agent])] = []
    var needsAttentionCount = 0
    var workingCount = 0

    init(agents: [AgentSnapshot.Agent], isUnread: (AgentSnapshot.Agent) -> Bool) {
        var buckets: [AgentState.Group: [AgentSnapshot.Agent]] = [:]
        for agent in agents {
            buckets[AgentState.of(agent, isUnread: isUnread(agent)).group, default: []].append(agent)
        }
        sections = AgentState.Group.allCases.compactMap { group in
            guard let agents = buckets[group], !agents.isEmpty else { return nil }
            return (group, agents)
        }
        needsAttentionCount = buckets[.needsAttention]?.count ?? 0
        workingCount = buckets[.working]?.count ?? 0
    }
}

// MARK: - Row

/// One session, in the shape the system's own lists use: a title, a quiet line
/// of metadata under it, and the state on the trailing edge.
struct SessionRow: View {
    let agent: AgentSnapshot.Agent
    let isUnread: Bool
    var showsEmoji = true
    /// Short displays give the calm sessions a single line of title.
    var isDense = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var state: AgentState { AgentState.of(agent, isUnread: isUnread) }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle()
                .fill(isUnread ? Color.accentColor : .clear)
                .frame(width: 7, height: 7)
                .accessibilityHidden(true)

            if showsEmoji, let emoji = agent.leadingEmoji {
                Text(emoji)
                    .font(.system(size: 14))
                    .frame(width: 26, height: 26)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color(.tertiarySystemFill))
                    )
                    .accessibilityHidden(true)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(showsEmoji ? agent.titleWithoutEmoji : agent.name)
                    .font(.body)
                    .fontWeight(isUnread ? .semibold : .regular)
                    .lineLimit(isDense && state.group != .needsAttention ? 1 : 2)
                    .frame(maxWidth: .infinity, alignment: .leading)

                Text(agent.metadata)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            VStack(alignment: .trailing, spacing: 2) {
                HStack(spacing: 4) {
                    Image(systemName: state.symbol)
                        .symbolEffect(.pulse, isActive: state.isLive && !reduceMotion)
                        .imageScale(.small)
                    Text(state.word)
                }
                .font(.footnote)
                .foregroundStyle(state.accent ?? .secondary)

                if let finishedAt = agent.finishedAt {
                    Text(compactAge(since: finishedAt))
                        .font(.caption2)
                        .monospacedDigit()
                        .foregroundStyle(.tertiary)
                }
            }
            .fixedSize()
        }
        .padding(.vertical, isDense ? 1 : 3)
        .contentShape(.rect)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(agent.titleWithoutEmoji)
        .accessibilityValue(accessibilityValue)
        .accessibilityHint(accessibilityHint)
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

/// "14s", "55m", "2h", "3d" — the system's relative style spells out
/// "55 min, 0 sec" and eats a third of the row for no more meaning.
func compactAge(since timestamp: Double) -> String {
    let seconds = max(0, Date().timeIntervalSince1970 - timestamp)
    if seconds < 60 { return "\(Int(seconds))s" }
    if seconds < 3600 { return "\(Int(seconds / 60))m" }
    if seconds < 86400 { return "\(Int(seconds / 3600))h" }
    return "\(Int(seconds / 86400))d"
}

enum Metrics {
    static let rhythm: CGFloat = 12
}
