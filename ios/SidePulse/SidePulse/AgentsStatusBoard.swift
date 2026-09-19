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
        case .needsYou: return agent.mode == "blocked_error" ? 0 : 1
        case .working: return 2
        case .settled: return 3
        }
    }

    private var grouping: AgentGrouping {
        AgentGrouping(agents: snapshot?.agents ?? [], isUnread: isUnread)
    }

    private var headline: (text: String, tint: Color, symbol: String) {
        guard snapshot != nil else {
            return ("Waiting for data", .secondary, "antenna.radiowaves.left.and.right.slash")
        }
        if grouping.needsYouCount > 0 {
            let count = grouping.needsYouCount
            return ("\(count) session\(count == 1 ? "" : "s") need\(count == 1 ? "s" : "") you",
                    .orange, "hand.raised.fill")
        }
        if grouping.activeCount > 0 {
            return ("\(grouping.activeCount) working", .blue, "bolt.fill")
        }
        return ("All quiet", .green, "checkmark.circle.fill")
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
        HStack(spacing: 14) {
            Image(systemName: headline.symbol)
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(headline.tint)
                .symbolEffect(.pulse, isActive: grouping.needsYouCount > 0 && !reduceMotion)
            VStack(alignment: .leading, spacing: 2) {
                Text(headline.text)
                    .font(.title.weight(.bold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                Text(hostLabel)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            counters
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: Metrics.cardRadius, style: .continuous)
                .fill(Color(.secondarySystemGroupedBackground))
                // The board lights up as the phone is opened.
                .shadow(color: headline.tint.opacity(0.4 * openness), radius: 16 * openness)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(headline.text)
        .accessibilityValue(hostLabel)
    }

    private var counters: some View {
        HStack(spacing: 16) {
            counter(agents.filter { $0.mode == "blocked_error" }.count, .red, "Blocked")
            counter(agents.filter { $0.mode == "waiting_for_input" }.count, .orange, "Asking")
            counter(grouping.activeCount, .blue, "Working")
            counter(agents.filter(isUnread).count, .green, "New")
        }
    }

    @ViewBuilder
    private func counter(_ value: Int, _ tint: Color, _ label: String) -> some View {
        if value > 0 {
            VStack(spacing: 1) {
                Text("\(value)")
                    .font(.system(size: 30, weight: .bold, design: .rounded))
                    .foregroundStyle(tint)
                    .monospacedDigit()
                    .contentTransition(.numericText())
                Text(label)
                    .font(.caption2.weight(.semibold))
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
                        .font(.headline)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16)
            }
        }
    }

    private func row(_ agent: AgentSnapshot.Agent) -> some View {
        let state = AgentState.of(agent, isUnread: isUnread(agent))
        return HStack(spacing: 14) {
            Image(systemName: state.symbol)
                .font(.system(size: 26, weight: .semibold))
                .foregroundStyle(state.tint)
                .symbolEffect(.pulse, isActive: state.isLive && !reduceMotion)
                .frame(width: 34)

            Text(agent.name)
                .font(.title2.weight(isUnread(agent) ? .semibold : .regular))
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(state.word)
                .font(.title3.weight(.semibold))
                .foregroundStyle(state.tint)
                .fixedSize()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(
            RoundedRectangle(cornerRadius: Metrics.cardRadius, style: .continuous)
                .fill(isUnread(agent)
                      ? Color.green.opacity(0.14)
                      : Color(.secondarySystemGroupedBackground))
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(agent.name)
        .accessibilityValue(state.word)
        .accessibilityHint("Opens the session")
    }

    /// Says what is being kept back, not just how much: with the urgent
    /// sessions sorted to the top, "none need you" is the line that lets
    /// someone walk away from the desk.
    private func overflowLabel(limit: Int) -> String {
        let hidden = agents.dropFirst(limit)
        let waiting = hidden.filter {
            AgentState.of($0, isUnread: isUnread($0)).group == .needsYou
        }.count
        return waiting > 0
            ? "+\(hidden.count) more · \(waiting) need you"
            : "+\(hidden.count) more · none need you"
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
                        Label("New session", systemImage: "plus.circle.fill")
                            .font(.headline)
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
