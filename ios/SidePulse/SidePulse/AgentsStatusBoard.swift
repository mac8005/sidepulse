import SwiftUI

/// The tabletop half of the agents screen: what the Mac's agents are doing,
/// set in type that survives being read from the other side of a desk. It goes
/// on the upper half while the phone stands half-folded; the controls sit on
/// the lower half, within reach.
struct AgentsStatusBoard: View {
    let snapshot: AgentSnapshot?
    let hostLabel: String
    let isUnread: (AgentSnapshot.Agent) -> Bool
    let selectedID: String?
    let select: (AgentSnapshot.Agent) -> Void
    /// 0 shut, 1 open — the board's glow follows the hinge as the phone is
    /// opened. Effect only; nothing here moves because of it.
    var openness: Double = 1

    private var agents: [AgentSnapshot.Agent] { snapshot?.agents ?? [] }

    private var attention: [AgentSnapshot.Agent] {
        agents.filter { $0.mode == "waiting_for_input" || $0.mode == "blocked_error" }
    }

    private var headline: (text: String, color: Color, symbol: String) {
        guard let snapshot else {
            return ("Waiting for data", .secondary, "antenna.radiowaves.left.and.right.slash")
        }
        if let first = attention.first {
            let word = attention.count == 1 ? "session needs you" : "sessions need you"
            return ("\(attention.count) \(word)", modeColor(first.mode), "questionmark.bubble.fill")
        }
        if snapshot.activeCount > 0 {
            return ("\(snapshot.activeCount) working", modeColor("working"), "bolt.fill")
        }
        let unread = agents.filter(isUnread).count
        if unread > 0 {
            return ("\(unread) finished, unread", modeColor("completed"), "checkmark.circle.fill")
        }
        return ("All quiet", .secondary, "moon.fill")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            banner
            board
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20)
        .padding(.top, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(.systemGroupedBackground))
    }

    private var banner: some View {
        HStack(spacing: 12) {
            Image(systemName: headline.symbol)
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(headline.color)
            VStack(alignment: .leading, spacing: 2) {
                Text(headline.text)
                    .font(.title.weight(.bold))
                    .foregroundStyle(.primary)
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
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(Color(.secondarySystemGroupedBackground))
                // The board lights up as the phone is opened.
                .shadow(color: headline.color.opacity(0.45 * openness), radius: 18 * openness)
        )
    }

    private var counters: some View {
        HStack(spacing: 14) {
            counter(agents.filter { $0.mode == "blocked_error" }.count, "blocked_error", "Blocked")
            counter(attention.filter { $0.mode == "waiting_for_input" }.count, "waiting_for_input", "Asking")
            counter(snapshot?.activeCount ?? 0, "working", "Active")
            counter(agents.filter(isUnread).count, "completed", "New")
        }
    }

    @ViewBuilder
    private func counter(_ value: Int, _ mode: String, _ label: String) -> some View {
        if value > 0 {
            VStack(spacing: 1) {
                Text("\(value)")
                    .font(.system(size: 30, weight: .bold, design: .rounded))
                    .foregroundStyle(modeColor(mode))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                Text(label)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var board: some View {
        ScrollView {
            VStack(spacing: 8) {
                ForEach(agents) { agent in
                    Button {
                        select(agent)
                    } label: {
                        row(agent)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .scrollBounceBehavior(.basedOnSize)
    }

    private func row(_ agent: AgentSnapshot.Agent) -> some View {
        HStack(spacing: 14) {
            Image(systemName: AgentModeStyle.symbol(agent.mode))
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(modeColor(agent.mode))
                .symbolEffect(.pulse, isActive: isUnread(agent))
                .frame(width: 30)

            Text(agent.name)
                .font(.title3.weight(isUnread(agent) ? .bold : .regular))
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(AgentModeStyle.label(agent.mode))
                .font(.headline)
                .foregroundStyle(modeColor(agent.mode))
                .fixedSize()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(rowFill(agent))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(
                    selectedID == agent.id ? Color.accentColor : .clear,
                    lineWidth: 2
                )
        )
    }

    private func rowFill(_ agent: AgentSnapshot.Agent) -> Color {
        if isUnread(agent) { return Color.green.opacity(0.16) }
        return Color(.secondarySystemGroupedBackground)
    }

    private func modeColor(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}

/// The lower, reachable half in the tabletop pose: everything the board cannot
/// do by itself — open the selected session, start a new one, read the usage
/// meters, drive the Dot.
struct AgentsDeskControls: View {
    @ObservedObject var model: AppModel
    @ObservedObject var usage: UsageClient
    let selected: AgentSnapshot.Agent?
    let links: [NewSessionLink]
    let clearSelection: () -> Void
    @State private var dotSettingsExpanded = false

    var body: some View {
        List {
            Section("Selected session") {
                if let selected {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(selected.name)
                            .font(.headline)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(
                            [selected.cwd, selected.detail]
                                .compactMap { $0 }
                                .joined(separator: " · ")
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    Button {
                        openAgentSession(selected)
                    } label: {
                        Label(
                            "Open in \(selected.provider?.capitalized ?? "provider")",
                            systemImage: "arrow.up.forward.app"
                        )
                    }
                    Button("Clear selection", systemImage: "xmark.circle", action: clearSelection)
                } else {
                    Text("Tap a session on the upper half.")
                        .foregroundStyle(.secondary)
                }
            }

            if !links.isEmpty {
                Section {
                    Menu {
                        ForEach(links) { link in
                            Button(link.label) { openFirstAvailable(link.candidates) }
                        }
                    } label: {
                        Label("New session", systemImage: "plus.circle")
                    }
                }
            }

            UsageSection(usage: usage)

            Section {
                DisclosureGroup("Dot settings", isExpanded: $dotSettingsExpanded) {
                    DotBehaviorControls(model: model)
                }
            }
        }
    }
}
