import SwiftUI

/// The tabletop half of the board: what the Mac's agents are doing, set in
/// type that survives being read from the other side of a desk. It takes the
/// upper half while the phone stands half-folded; the controls sit on the
/// lower half, within reach.
struct AgentsStatusBoard: View {
    let snapshot: AgentSnapshot?
    let hostLabel: String
    let isUnread: (AgentSnapshot.Agent) -> Bool
    /// 0 shut, 1 open — the board's glow follows the hinge as the phone opens.
    /// Effect only; nothing here moves because of it.
    var openness: Double = 1
    var showsEmoji = true
    let open: (AgentSnapshot.Agent) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Whatever wants a person comes first: the board is read from across a
    /// desk, so the top of it has to be the part worth walking over for.
    private var agents: [AgentSnapshot.Agent] {
        (snapshot?.agents ?? []).enumerated()
            .sorted { lhs, rhs in
                let left = rank(lhs.element), right = rank(rhs.element)
                return left == right ? lhs.offset < rhs.offset : left < right
            }
            .map(\.element)
    }

    private func rank(_ agent: AgentSnapshot.Agent) -> Int {
        let state = AgentState.of(agent, isUnread: isUnread(agent))
        switch state.group {
        case .needsAttention: return agent.mode == "blocked_error" ? 0 : 1
        case .working: return 2
        case .finished: return 3
        }
    }

    private var grouping: AgentGrouping {
        AgentGrouping(agents: snapshot?.agents ?? [], isUnread: isUnread)
    }

    private var headline: String {
        guard snapshot != nil else { return "Waiting for data" }
        if grouping.needsAttentionCount > 0 {
            return "\(grouping.needsAttentionCount) need attention"
        }
        if grouping.workingCount > 0 { return "\(grouping.workingCount) working" }
        return "No active sessions"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            banner
            board
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(.systemGroupedBackground))
    }

    private var banner: some View {
        HStack(alignment: .lastTextBaseline, spacing: 16) {
            VStack(alignment: .leading, spacing: 2) {
                Text(headline)
                    .font(.title2.weight(.semibold))
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                Text(hostLabel)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            counters
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(.secondarySystemGroupedBackground))
        )
        // Restrained: the counters settle in as the phone is opened, and
        // nothing else about the board moves.
        .opacity(0.6 + 0.4 * openness)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(headline)
        .accessibilityValue(hostLabel)
    }

    private var counters: some View {
        HStack(alignment: .lastTextBaseline, spacing: 20) {
            counter(agents.filter { $0.mode == "blocked_error" }.count, .red, "Blocked")
            counter(agents.filter { $0.mode == "waiting_for_input" }.count, .orange, "Needs input")
            counter(grouping.workingCount, nil, "Working")
            counter(agents.filter(isUnread).count, nil, "Unread")
        }
    }

    @ViewBuilder
    private func counter(_ value: Int, _ accent: Color?, _ label: String) -> some View {
        if value > 0 {
            VStack(alignment: .trailing, spacing: 0) {
                Text("\(value)")
                    .font(.system(.largeTitle, design: .default).weight(.medium))
                    .monospacedDigit()
                    .foregroundStyle(accent ?? .primary)
                    .contentTransition(.numericText())
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(value) \(label)")
        }
    }

    /// Only whole rows: a line sliced by the crease is unreadable, so the
    /// board shows as many as the upper half really holds and says what it is
    /// keeping back. The container decides, not a constant.
    private var board: some View {
        // ViewThatFits picks the first child that fits, so the candidates are
        // spelled out rather than generated: a ForEach would hand it one.
        ViewThatFits(in: .vertical) {
            stack(limit: 12)
            stack(limit: 9)
            stack(limit: 8)
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
        VStack(spacing: 8) {
            ForEach(agents.prefix(limit)) { agent in
                Button {
                    open(agent)
                } label: {
                    row(agent)
                }
                .buttonStyle(.plain)
            }
            if agents.count > limit {
                HStack {
                    Text(overflowLabel(limit: limit))
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16)
            }
        }
    }

    private func row(_ agent: AgentSnapshot.Agent) -> some View {
        let state = AgentState.of(agent, isUnread: isUnread(agent))
        return HStack(spacing: 12) {
            Circle()
                .fill(isUnread(agent) ? Color.accentColor : .clear)
                .frame(width: 8, height: 8)

            Image(systemName: state.symbol)
                .font(.title3)
                .foregroundStyle(state.accent ?? .secondary)
                .symbolEffect(.pulse, isActive: state.isLive && !reduceMotion)
                .frame(width: 26)

            if showsEmoji, let emoji = agent.leadingEmoji {
                Text(emoji)
                    .font(.system(size: 14))
                    .frame(width: 25, height: 25)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color(.tertiarySystemFill))
                    )
                    .accessibilityHidden(true)
            }

            Text(agent.titleWithoutEmoji)
                .font(.title3)
                .fontWeight(isUnread(agent) ? .semibold : .regular)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(state.word)
                .font(.title3)
                .foregroundStyle(state.accent ?? .secondary)
                .fixedSize()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(.secondarySystemGroupedBackground))
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(agent.titleWithoutEmoji)
        .accessibilityValue(state.word)
        .accessibilityHint("Opens the session")
    }

    /// Says what is being kept back, not just how much: with the urgent
    /// sessions sorted to the top, "none need you" is the line that lets
    /// someone walk away from the desk.
    private func overflowLabel(limit: Int) -> String {
        let hidden = agents.dropFirst(limit)
        let waiting = hidden.filter {
            AgentState.of($0, isUnread: isUnread($0)).group == .needsAttention
        }.count
        return waiting > 0
            ? "\(hidden.count) more · \(waiting) need attention"
            : "\(hidden.count) more · none need attention"
    }
}

/// The lower, reachable half in the tabletop pose: everything the board cannot
/// do by itself — start a session, read the usage meters, reach the Dot.
struct AgentsDeskControls: View {
    @ObservedObject var model: AppModel
    @ObservedObject var usage: UsageClient
    let links: [NewSessionLink]
    let openDot: () -> Void

    var body: some View {
        List {
            if !links.isEmpty {
                Section {
                    Menu {
                        ForEach(links) { link in
                            Button(link.label) { openFirstAvailable(link.candidates) }
                        }
                    } label: {
                        Label("New session", systemImage: "plus")
                            .font(.body)
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                }
            }

            UsageSection(usage: usage)

            Section {
                Button {
                    openDot()
                } label: {
                    DotStatusRow(model: model)
                }
                .buttonStyle(.plain)
            }
        }
    }
}
