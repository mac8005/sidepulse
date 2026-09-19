import SwiftUI
import UIKit

/// Realtime agent monitor: streams snapshots from the Mac over the local
/// network / Tailscale while the app is in the foreground. The stream is
/// owned by `DotStatusMirror`, which drives a plugged-in Dot from it.
///
/// Narrow displays get one list, exactly as before. Where there is room for
/// two panes — the iPhone Duo's inner display, an iPad — the sessions keep the
/// primary pane and the second one carries what used to be buried below them.
struct AgentsLiveView: View {
    enum Layout {
        /// One list when narrow, two panes when the display has room.
        case adaptive
        /// Sessions only; the surrounding shell supplies the other columns.
        case listOnly
    }

    private struct SeenAcknowledgement: Decodable {
        let ok: Bool
        let marked: Bool
    }

    @ObservedObject var model: AppModel
    var layout: Layout = .adaptive
    /// Set by a shell that owns the detail column itself (the three-column
    /// variant); otherwise the screen keeps its own selection.
    var externalSelection: Binding<String?>?

    @ObservedObject private var stream = DotStatusMirror.shared.stream
    @ObservedObject private var usage = UsageClient.shared
    @ObservedObject private var sessionLinks = SessionLinksClient.shared
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    /// Completions tapped this app session, keyed by the row's finish time
    /// so the dimming applies only to the completion the user actually
    /// opened — a session that finishes another turn re-arms as unread.
    @State private var locallySeen: [String: Double] = [:]
    @State private var dotSettingsExpanded = false
    @State private var ownSelection: String?

    var body: some View {
        content
            .navigationTitle("Mac Agents")
            .toolbar { toolbar }
            .duoCompactTitle()
            .task {
                // Normally already running from the scene going active; harmless
                // to repeat.
                DotStatusMirror.shared.start(model: model)
#if DEBUG && SIDEPULSE_MAIN_APP
                if let id = DemoData.selectedAgentID { selection.wrappedValue = id }
#endif
            }
            .task(id: model.liveMonitorServerURL) {
                await usage.poll(baseURL: model.liveMonitorServerURL)
            }
            .task(id: model.liveMonitorServerURL) {
                await sessionLinks.load(baseURL: model.liveMonitorServerURL)
            }
    }

    // MARK: - Layout

    private var isWide: Bool { horizontalSizeClass == .regular }

    @ViewBuilder
    private var content: some View {
        switch layout {
        case .listOnly:
            sessionsPane
        case .adaptive:
            if isWide {
                DuoSplit {
                    sessionsPane
                } secondary: {
                    secondaryPane
                }
            } else {
                narrowList
            }
        }
    }

    /// Everything in one scroll, the way the phone has always shown it.
    private var narrowList: some View {
        List {
            Section {
                header
            }

            agentsSection

            UsageSection(usage: usage)

            Section {
                DisclosureGroup("Dot settings", isExpanded: $dotSettingsExpanded) {
                    DotBehaviorControls(model: model)
                }
            }
        }
    }

    private var sessionsPane: some View {
        List {
            Section {
                header
            }

            agentsSection
        }
    }

    @ViewBuilder
    private var secondaryPane: some View {
        switch DuoVariant.current {
        case .b:
            AgentsDashboard(model: model, usage: usage)
        default:
            if let agent = selectedAgent {
                AgentSessionDetail(
                    agent: agent,
                    updatedAt: stream.snapshot?.updatedAt,
                    isUnread: isUnread(agent),
                    close: { selection.wrappedValue = nil }
                )
            } else {
                AgentsDashboard(model: model, usage: usage)
            }
        }
    }

    @ViewBuilder
    private var agentsSection: some View {
        Section("Agents") {
            if let snapshot = stream.snapshot, !snapshot.agents.isEmpty {
                ForEach(snapshot.agents) { agent in
                    AgentLiveRow(
                        agent: agent,
                        isUnread: isUnread(agent),
                        isSelected: selection.wrappedValue == agent.id
                    ) {
                        activate(agent)
                    }
                    .listRowBackground(rowBackground(agent))
                }
            } else if stream.snapshot != nil {
                Text("All quiet — no active agents.")
                    .foregroundStyle(.secondary)
            } else {
                Text("Waiting for data…")
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func rowBackground(_ agent: AgentSnapshot.Agent) -> some View {
        if selection.wrappedValue == agent.id, showsDetailPane {
            Color.accentColor.opacity(0.18)
        } else if isUnread(agent) {
            Color.green.opacity(0.16)
        }
    }

    // MARK: - Toolbar

    /// Both items carry a title and an icon: the icon is what lets the system
    /// move them into the vertical bar strip it uses on the iPhone Duo, and
    /// the title is what labels them in the overflow menu.
    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        if !sessionLinks.links.isEmpty {
            ToolbarItem(placement: .topBarTrailing) {
                // Hands off to the provider's own app; the daemon says where.
                Menu {
                    ForEach(sessionLinks.links) { link in
                        Button(link.label) { openFirstAvailable(link.candidates) }
                    }
                } label: {
                    Label("New session", systemImage: "plus")
                }
            }
        }

        if layout == .adaptive {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink(value: Route.settings) {
                    Label("Settings", systemImage: "gearshape")
                }
            }
        }
    }

    // MARK: - Selection

    private var selection: Binding<String?> {
        externalSelection ?? $ownSelection
    }

    /// True when a second pane is showing the session, so a tap should select
    /// it instead of leaving for the provider's app.
    private var showsDetailPane: Bool {
        switch layout {
        case .listOnly: return true
        case .adaptive: return isWide && DuoVariant.current != .b
        }
    }

    private var selectedAgent: AgentSnapshot.Agent? {
        guard let id = selection.wrappedValue else { return nil }
        return stream.snapshot?.agents.first { $0.id == id }
    }

    private func activate(_ agent: AgentSnapshot.Agent) {
        markSeen(agent)
        if showsDetailPane {
            selection.wrappedValue = agent.id
        } else {
            openAgentSession(agent)
        }
    }

    // MARK: - Unread bookkeeping

    private func isUnread(_ agent: AgentSnapshot.Agent) -> Bool {
        guard let finishedAt = agent.finishedAt, agent.unread == true else { return false }
        return locallySeen[agent.id] != finishedAt
    }

    /// Tell the daemon this finished session was opened; it re-pushes the
    /// dimmed state to the Live Activity and every other client.
    private func markSeen(_ agent: AgentSnapshot.Agent) {
        guard isUnread(agent), let finishedAt = agent.finishedAt else { return }
        locallySeen[agent.id] = finishedAt
        guard
            let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("seen"),
            let body = try? JSONSerialization.data(
                withJSONObject: ["id": agent.id, "finishedAt": finishedAt]
            )
        else {
            rollBackSeen(id: agent.id, finishedAt: finishedAt)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        request.timeoutInterval = 10
        Task {
            // The daemon owns this state. If it never heard the tap, drop the
            // local override rather than showing "read" over a row every
            // other surface still reports as unread.
            var acknowledged = false
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                if let httpResponse = response as? HTTPURLResponse,
                   (200..<300).contains(httpResponse.statusCode),
                   let receipt = try? JSONDecoder().decode(SeenAcknowledgement.self, from: data) {
                    acknowledged = receipt.ok
                    _ = receipt.marked // false is the valid idempotent response.
                }
            } catch {}

            if !acknowledged {
                rollBackSeen(id: agent.id, finishedAt: finishedAt)
            }
        }
    }

    private func rollBackSeen(id: String, finishedAt: Double) {
        guard locallySeen[id] == finishedAt else { return }
        locallySeen[id] = nil
    }

    @ViewBuilder
    private var header: some View {
        HStack {
            switch stream.state {
            case .live:
                Label("Live", systemImage: "dot.radiowaves.left.and.right")
                    .foregroundStyle(.green)
            case .connecting:
                Label("Connecting…", systemImage: "arrow.triangle.2.circlepath")
                    .foregroundStyle(.orange)
            case .failed(let message):
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
                    .lineLimit(2)
            case .idle:
                Label("Idle", systemImage: "pause.circle")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let snapshot = stream.snapshot {
                let unread = snapshot.agents.filter(isUnread).count
                if unread > 0 {
                    Label("\(unread) new", systemImage: "bell.badge.fill")
                        .font(.caption.bold())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.green, in: Capsule())
                }
                Text("\(snapshot.activeCount) active")
                    .font(.subheadline.bold())
            }
        }
        .font(.subheadline)
    }
}

// MARK: - Second pane

/// Usage meters and the Dot, side by side with the sessions instead of far
/// below them, on displays wide enough to show both.
struct AgentsDashboard: View {
    @ObservedObject var model: AppModel
    @ObservedObject var usage: UsageClient
    @ObservedObject private var mirror = DotStatusMirror.shared

    var body: some View {
        List {
            UsageSection(usage: usage)

            Section {
                DotBehaviorControls(model: model)
            } header: {
                Text("SidePulse Dot")
            }
        }
    }
}

/// What a session is doing, and the way into it — the pane the sessions list
/// feeds when the display is wide enough to keep both on screen.
struct AgentSessionDetail: View {
    let agent: AgentSnapshot.Agent
    var updatedAt: Double?
    var isUnread: Bool
    /// Returns the pane to the usage / Dot dashboard. Absent where the shell
    /// owns the column itself.
    var close: (() -> Void)?

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .top) {
                        Text(agent.name)
                            .font(.title3.weight(.semibold))
                            .fixedSize(horizontal: false, vertical: true)
                        if let close {
                            Spacer(minLength: 8)
                            Button(action: close) {
                                Label("Close session", systemImage: "xmark.circle.fill")
                                    .labelStyle(.iconOnly)
                                    .font(.title3)
                                    .foregroundStyle(.tertiary)
                            }
                            .buttonStyle(.plain)
                        }
                    }

                    HStack(spacing: 8) {
                        Label(
                            AgentModeStyle.label(agent.mode),
                            systemImage: AgentModeStyle.symbol(agent.mode)
                        )
                        .font(.subheadline.bold())
                        .foregroundStyle(modeColor)

                        if let provider = agent.provider {
                            Text(provider.capitalized)
                                .font(.caption2.bold())
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Color(.tertiarySystemFill))
                                .clipShape(Capsule())
                        }

                        if isUnread {
                            Text("NEW")
                                .font(.caption2.bold())
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Color.green, in: Capsule())
                        }
                    }
                }
                .padding(.vertical, 4)
            }

            Section("Session") {
                if let cwd = agent.cwd {
                    LabeledContent("Project", value: cwd)
                }
                if let detail = agent.detail {
                    LabeledContent("Activity", value: detail)
                }
                if let finishedAt = agent.finishedAt {
                    LabeledContent("Finished") {
                        Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                            + Text(" ago")
                    }
                }
                if let updatedAt {
                    LabeledContent("Last update") {
                        Text(Date(timeIntervalSince1970: updatedAt), style: .relative)
                            + Text(" ago")
                    }
                }
            }

            Section {
                Button {
                    openAgentSession(agent)
                } label: {
                    Label(openLabel, systemImage: "arrow.up.forward.app")
                }
            } footer: {
                Text(
                    agent.deepLink == nil
                        ? "Opens the provider's app; this session has no conversation link yet."
                        : "Opens this conversation directly."
                )
            }
        }
    }

    private var openLabel: String {
        guard let provider = agent.provider else { return "Open session" }
        return "Open in \(provider.capitalized)"
    }

    private var modeColor: Color {
        let (r, g, b) = AgentModeStyle.rgb(agent.mode)
        return Color(red: r, green: g, blue: b)
    }
}

// MARK: - Row

/// A Remote-Control session deep-links to the exact conversation; otherwise
/// fall back to opening the provider app.
func openAgentSession(_ agent: AgentSnapshot.Agent) {
    if let link = agent.deepLink, let url = URL(string: link) {
        openFirstAvailable([url])
        return
    }
    let provider = agent.provider ?? String(agent.id.split(separator: ":").first ?? "")
    let candidates: [URL]
    switch provider {
    case "claude":
        candidates = [URL(string: "claude://")!, URL(string: "https://claude.ai")!]
    case "codex":
        candidates = [URL(string: "chatgpt://")!, URL(string: "https://chatgpt.com")!]
    case "paseo":
        candidates = [URL(string: "paseo://")!]
    default:
        return
    }
    openFirstAvailable(candidates)
}

private struct AgentLiveRow: View {
    let agent: AgentSnapshot.Agent
    let isUnread: Bool
    let isSelected: Bool
    let activate: () -> Void

    var body: some View {
        Button {
            activate()
        } label: {
            rowContent
        }
        .buttonStyle(.plain)
    }

    private var rowContent: some View {
        HStack(spacing: 0) {
            // Unread sessions carry a green edge bar so they are obvious
            // even at a glance down a long list.
            RoundedRectangle(cornerRadius: 2, style: .continuous)
                .fill(isUnread ? Color.green : Color.clear)
                .frame(width: 4)
                .padding(.trailing, isUnread ? 8 : 0)
            details
        }
    }

    private var details: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .top, spacing: 10) {
                glyph
                    .font(.system(size: 13))
                    .foregroundStyle(color(agent.mode))
                    .frame(width: 16)
                    .padding(.top, 3)
                VStack(alignment: .leading, spacing: 3) {
                    if isUnread {
                        Text("NEW")
                            .font(.caption2.bold())
                            .foregroundStyle(.white)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.green, in: Capsule())
                    }
                    Text(agent.name)
                        .font(isUnread ? .body.weight(.bold) : .body)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            HStack(spacing: 6) {
                if let provider = agent.provider {
                    Text(provider.capitalized)
                        .font(.caption2.bold())
                        .lineLimit(1)
                        .fixedSize()
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color(.tertiarySystemFill))
                        .clipShape(Capsule())
                }
                Text(secondaryLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text(AgentModeStyle.label(agent.mode))
                    .font(.caption.bold())
                    .foregroundStyle(color(agent.mode))
                    .fixedSize()
                if let finishedAt = agent.finishedAt {
                    Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                } else if isSelected {
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                } else {
                    Image(systemName: "arrow.up.forward.app")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.leading, 20)
        }
    }

    /// Unread finished sessions pulse until opened.
    @ViewBuilder
    private var glyph: some View {
        let image = Image(systemName: AgentModeStyle.symbol(agent.mode))
        if #available(iOS 17.0, *), isUnread {
            image.symbolEffect(.pulse)
        } else {
            image
        }
    }

    private var secondaryLine: String {
        let parts = [agent.cwd, agent.detail].compactMap { $0 }
        return parts.isEmpty ? AgentModeStyle.label(agent.mode) : parts.joined(separator: " · ")
    }

    private func color(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}
