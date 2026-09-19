import SwiftUI
import UIKit

/// The root of the app: what the agents on the Mac are doing, grouped by the
/// only question that matters at a glance — does anything want me. The stream
/// behind it is owned by `DotStatusMirror`, which drives a plugged-in Dot from
/// the same snapshot.
///
/// A narrow display shows one scroll. Where there is room for two panes — the
/// iPhone Duo's inner display, an iPad — the sessions keep the primary pane
/// and the usage meters and the Dot move beside them instead of below.
struct BoardScreen: View {
    private struct SeenAcknowledgement: Decodable {
        let ok: Bool
        let marked: Bool
    }

    @ObservedObject var model: AppModel
    @Binding var path: [Route]

    @ObservedObject private var stream = DotStatusMirror.shared.stream
    @ObservedObject private var usage = UsageClient.shared
    @ObservedObject private var sessionLinks = SessionLinksClient.shared
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    /// Completions tapped this app session, keyed by the row's finish time so
    /// the dimming applies only to the completion the user actually opened — a
    /// session that finishes another turn re-arms as unread.
    @State private var locallySeen: [String: Double] = [:]
    @State private var fold = DuoFold()
    @State private var hingeOpenness: Double = 1
    @State private var containerHeight: CGFloat = 0

    var body: some View {
        content
            .navigationTitle("Agents")
            .toolbar { toolbar }
            .duoCompactTitle(force: fold.isTabletop)
            .duoPrefersToolbarItems()
            .duoFold($fold)
            .duoHingeOpenness($hingeOpenness)
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { containerHeight = $0 }
            .refreshable { await refresh() }
            .task {
                // Normally already running from the scene going active;
                // harmless to repeat.
                DotStatusMirror.shared.start(model: model)
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

    /// A narrow container that is also short is a glance surface: the summary
    /// collapses into one line, titles keep to one line and the rows tighten,
    /// so everything that matters is on screen at once. The iPhone Duo's outer
    /// display lands here, and so does any phone held sideways.
    private var isDense: Bool {
        horizontalSizeClass == .compact && containerHeight > 0 && containerHeight < 720
    }

    @ViewBuilder
    private var content: some View {
        if fold.isTabletop {
            // Standing on a desk: a board that reads from across the room
            // above the crease, everything you touch below it.
            DuoSplit {
                AgentsStatusBoard(
                    snapshot: stream.snapshot,
                    hostLabel: hostLabel,
                    isUnread: isUnread,
                    openness: hingeOpenness,
                    showsEmoji: model.showSessionEmoji
                ) { agent in
                    markSeen(agent)
                    openAgentSession(agent)
                }
            } secondary: {
                AgentsDeskControls(
                    model: model,
                    usage: usage,
                    links: sessionLinks.links,
                    openDot: { path.append(.dot) }
                )
            }
        } else if isWide {
            DuoSplit {
                sessions
            } secondary: {
                AgentsDashboard(model: model, usage: usage, openDot: { path.append(.dot) })
            }
        } else {
            sessions
        }
    }

    private var sessions: some View {
        List {
            Section { summary }
                .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                .listRowBackground(Color.clear)

            if let snapshot = stream.snapshot {
                if snapshot.agents.isEmpty {
                    BoardMessage(
                        symbol: "checkmark.circle",
                        title: "No active sessions",
                        message: "Nothing is running on \(hostLabel)."
                    )
                } else {
                    let grouping = AgentGrouping(agents: snapshot.agents, isUnread: isUnread)
                    ForEach(grouping.sections, id: \.group) { section in
                        Section {
                            ForEach(section.agents) { agent in
                                row(agent)
                            }
                        } header: {
                            sectionHeader(section.group, count: section.agents.count)
                        }
                    }
                }
            } else {
                connectionState
            }

            if !isWide {
                UsageSection(usage: usage, isDense: isDense)
                dotSectionLink
            }
        }
        .listSectionSpacing(isDense ? .compact : .default)
    }

    @ViewBuilder
    private func row(_ agent: AgentSnapshot.Agent) -> some View {
        Button {
            markSeen(agent)
            openAgentSession(agent)
        } label: {
            SessionRow(
                agent: agent,
                isUnread: isUnread(agent),
                showsEmoji: model.showSessionEmoji,
                isDense: isDense
            )
        }
        .buttonStyle(.plain)
        // The vertical strip already takes width from the trailing edge, so
        // the row gives some back there.
        .listRowInsets(isDense
                       ? EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 10)
                       : nil)
        .swipeActions(edge: .leading, allowsFullSwipe: true) {
            if isUnread(agent) {
                Button {
                    markSeen(agent)
                } label: {
                    Label("Mark read", systemImage: "envelope.open")
                }
            }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            Button {
                markSeen(agent)
                openAgentSession(agent)
            } label: {
                Label("Open", systemImage: "arrow.up.forward.app")
            }
        }
        .contextMenu {
            Button("Open session", systemImage: "arrow.up.forward.app") {
                markSeen(agent)
                openAgentSession(agent)
            }
            if isUnread(agent) {
                Button("Mark as read", systemImage: "envelope.open") { markSeen(agent) }
            }
            if let cwd = agent.cwd {
                Button("Copy project path", systemImage: "doc.on.doc") {
                    UIPasteboard.general.string = cwd
                }
            }
        }
    }

    private func sectionHeader(_ group: AgentState.Group, count: Int) -> some View {
        HStack {
            Text(group.title)
            Spacer(minLength: 0)
            Text("\(count)")
                .monospacedDigit()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(group.title), \(count) session\(count == 1 ? "" : "s")")
    }

    // MARK: - Summary and states

    /// The one line that answers "does anything need me?", with an honest
    /// reading age beside it.
    private var summary: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(headline)
                .font(.headline)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            HStack(spacing: 5) {
                if !model.liveMonitorServerURL.isEmpty {
                    ConnectionDot(state: stream.state)
                }
                Text(subtitle)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(headline)
        .accessibilityValue(subtitle)
    }

    private var headline: String {
        guard !model.liveMonitorServerURL.isEmpty else { return "No Mac configured" }
        guard let snapshot = stream.snapshot else { return "Connecting" }
        let grouping = AgentGrouping(agents: snapshot.agents, isUnread: isUnread)
        var parts: [String] = []
        if grouping.needsAttentionCount > 0 {
            parts.append("\(grouping.needsAttentionCount) need attention")
        }
        if grouping.workingCount > 0 { parts.append("\(grouping.workingCount) working") }
        parts.append("\(snapshot.agents.count) session\(snapshot.agents.count == 1 ? "" : "s")")
        return parts.joined(separator: " · ")
    }

    private var subtitle: String {
        guard !model.liveMonitorServerURL.isEmpty else {
            return "Add the address of the Mac you want to watch"
        }
        guard let snapshot = stream.snapshot else { return hostLabel }
        let updated = Date(timeIntervalSince1970: snapshot.updatedAt)
        return "\(hostLabel) · Updated \(updated.formatted(date: .omitted, time: .shortened))"
    }

    /// Nothing has arrived yet: say which of the three reasons it is, and what
    /// to do about it, rather than showing an empty list.
    @ViewBuilder
    private var connectionState: some View {
        if model.liveMonitorServerURL.isEmpty {
            SetupChecklist { path.append(.settings) }
        } else {
            switch stream.state {
            case .failed(let message):
                BoardMessage(
                    symbol: "antenna.radiowaves.left.and.right.slash",
                    title: "Can’t reach \(hostLabel)",
                    message: message,
                    tint: .orange
                ) {
                    Button("Open Settings", systemImage: "gearshape") { path.append(.settings) }
                }
            case .idle:
                BoardMessage(
                    symbol: "pause.circle",
                    title: "Paused",
                    message: "SidePulse streams while it is in front. Pull down to reconnect."
                )
            default:
                BoardMessage(
                    symbol: "dot.radiowaves.left.and.right",
                    title: "Connecting to \(hostLabel)",
                    message: "Waiting for the first snapshot from the monitor."
                )
            }
        }
    }

    private var dotSectionLink: some View {
        Section {
            Button {
                path.append(.dot)
            } label: {
                DotStatusRow(model: model)
            }
            .buttonStyle(.plain)
        }
    }

    private var hostLabel: String {
        URL(string: model.liveMonitorServerURL)?.host ?? "your Mac"
    }

    // MARK: - Toolbar

    /// Every item carries a title and a symbol, so the system can move it into
    /// the vertical strip the iPhone Duo uses and still name it in the
    /// overflow menu. The strip stays light on purpose: a primary action, a
    /// status item that appears only when it has something to say, and one
    /// system overflow for the rest.
    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        if attentionCount > 0 {
            ToolbarItem(placement: .topBarPinnedTrailing) {
                Button {
                    openFirstNeedingAttention()
                } label: {
                    Label("Needs you", systemImage: "bell.badge")
                }
                .badge(attentionCount)
            }
            .visibilityPriority(.high)
        }

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
            .visibilityPriority(.high)
        }

        DuoOverflow {
            Button("SidePulse Dot", systemImage: "light.beacon.max") { path.append(.dot) }
            Button("Settings", systemImage: "gearshape") { path.append(.settings) }
            Button("Refresh", systemImage: "arrow.clockwise") { Task { await refresh() } }
        }
    }

    private var attentionCount: Int {
        guard let snapshot = stream.snapshot else { return 0 }
        return AgentGrouping(agents: snapshot.agents, isUnread: isUnread).needsAttentionCount
    }

    private func openFirstNeedingAttention() {
        guard let snapshot = stream.snapshot,
              let agent = snapshot.agents.first(where: {
                  AgentState.of($0, isUnread: isUnread($0)).group == .needsAttention
              })
        else { return }
        markSeen(agent)
        openAgentSession(agent)
    }

    private func refresh() async {
        await usage.fetch(baseURL: model.liveMonitorServerURL)
        DotStatusMirror.shared.start(model: model)
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
            // local override rather than showing "read" over a row every other
            // surface still reports as unread.
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
}

// MARK: - Connection

/// Connected or not, as a small dot — the kind Mail puts beside a mailbox.
struct ConnectionDot: View {
    let state: AgentStreamClient.ConnectionState

    var body: some View {
        Circle()
            .fill(tint)
            .frame(width: 6, height: 6)
            .accessibilityElement()
            .accessibilityLabel("Connection")
            .accessibilityValue(word)
    }

    private var word: String {
        switch state {
        case .live: return "Connected"
        case .connecting: return "Connecting"
        case .failed: return "Offline"
        case .idle: return "Paused"
        }
    }

    private var tint: Color {
        switch state {
        case .live: return .secondary
        case .connecting: return .secondary.opacity(0.5)
        case .failed: return .red
        case .idle: return .secondary.opacity(0.35)
        }
    }
}

/// Anything the list cannot show: empty, connecting, unreachable.
struct BoardMessage<Actions: View>: View {
    let symbol: String
    let title: String
    let message: String
    var tint: Color = .secondary
    @ViewBuilder var actions: () -> Actions

    init(
        symbol: String,
        title: String,
        message: String,
        tint: Color = .secondary,
        @ViewBuilder actions: @escaping () -> Actions = { EmptyView() }
    ) {
        self.symbol = symbol
        self.title = title
        self.message = message
        self.tint = tint
        self.actions = actions
    }

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.title)
                .foregroundStyle(tint)
            Text(title)
                .font(.headline)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            actions()
                .buttonStyle(.bordered)
                .padding(.top, 2)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
        .listRowBackground(Color.clear)
        .accessibilityElement(children: .combine)
    }
}

/// First run: three things to do, not a blank screen.
struct SetupChecklist: View {
    let openSettings: () -> Void

    var body: some View {
        Section {
            VStack(alignment: .leading, spacing: 10) {
                Text("Set up SidePulse")
                    .font(.headline)
                checklist("1", "Run `sidepulse live-activity` on the Mac you want to watch.")
                checklist("2", "Enter its address in Settings.")
                checklist("3", "Optional: connect a SidePulse Dot to mirror the state as light.")
                Button("Open Settings", systemImage: "gearshape", action: openSettings)
                    .buttonStyle(.borderedProminent)
                    .padding(.top, 4)
            }
            .padding(.vertical, 4)
        }
    }

    private func checklist(_ number: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(number)
                .font(.footnote.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 16, alignment: .trailing)
            Text(text)
                .font(.subheadline)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Second pane

/// Usage meters and the Dot, beside the sessions instead of far below them,
/// wherever the display has room for both.
struct AgentsDashboard: View {
    @ObservedObject var model: AppModel
    @ObservedObject var usage: UsageClient
    let openDot: () -> Void
    @ObservedObject private var mirror = DotStatusMirror.shared

    var body: some View {
        List {
            UsageSection(usage: usage)

            Section {
                HStack(spacing: 14) {
                    DotPreview(model: model)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(mirror.statusText)
                            .font(.subheadline)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(model.selectedFolderPath)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.head)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 2)
                .accessibilityElement(children: .combine)

                // The two controls people actually reach for; everything else
                // is a push away.
                Toggle("Do Not Disturb", isOn: $model.dndEnabled)
                LabeledContent("Brightness", value: brightnessLabel)
                Slider(value: brightnessPercentage, in: 0...100, step: 1) {
                    Text("SidePulse Dot brightness")
                } minimumValueLabel: {
                    Image(systemName: "sun.min").accessibilityHidden(true)
                } maximumValueLabel: {
                    Image(systemName: "sun.max").accessibilityHidden(true)
                }
                .accessibilityLabel("SidePulse Dot brightness")
                .accessibilityValue(brightnessLabel)

                Button {
                    openDot()
                } label: {
                    Label("All Dot settings", systemImage: "chevron.right")
                        .labelStyle(.titleOnly)
                }
            } header: {
                Text("SidePulse Dot")
            }
        }
    }

    private var brightnessLabel: String {
        guard model.dotBrightness > 0 else { return "Off" }
        return "\(Int((Double(model.dotBrightness) / Double(DotBrightness.maximum) * 100).rounded()))%"
    }

    private var brightnessPercentage: Binding<Double> {
        Binding {
            (Double(model.dotBrightness) / Double(DotBrightness.maximum) * 100).rounded()
        } set: { percentage in
            model.dotBrightness = DotBrightness.clamped(
                Int((min(100, max(0, percentage.rounded())) / 100 * Double(DotBrightness.maximum)).rounded())
            )
        }
    }
}

// MARK: - Session opening

/// A Remote-Control session deep-links to the exact conversation; otherwise
/// fall back to opening the provider app.
func openAgentSession(_ agent: AgentSnapshot.Agent) {
    if let link = agent.deepLink, let url = URL(string: link) {
        openFirstAvailable([url])
        return
    }
    switch agent.providerName {
    case "claude":
        openFirstAvailable([URL(string: "claude://")!, URL(string: "https://claude.ai")!])
    case "codex":
        openFirstAvailable([URL(string: "chatgpt://")!, URL(string: "https://chatgpt.com")!])
    case "paseo":
        openFirstAvailable([URL(string: "paseo://")!])
    default:
        break
    }
}
