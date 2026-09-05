import SwiftUI
import UIKit

/// Realtime agent monitor: streams snapshots from the Mac over the local
/// network / Tailscale while the app is in the foreground. The stream is
/// owned by `DotStatusMirror`, which drives a plugged-in Dot from it.
struct AgentsLiveView: View {
    private struct SeenAcknowledgement: Decodable {
        let ok: Bool
        let marked: Bool
    }

    @ObservedObject var model: AppModel
    @ObservedObject private var stream = DotStatusMirror.shared.stream
    @StateObject private var usage = UsageClient()
    /// Completions tapped this app session, keyed by the row's finish time
    /// so the dimming applies only to the completion the user actually
    /// opened — a session that finishes another turn re-arms as unread.
    @State private var locallySeen: [String: Double] = [:]
    @State private var dotSettingsExpanded = false

    var body: some View {
        List {
            Section {
                header
            }

            Section("Agents") {
                if let snapshot = stream.snapshot, !snapshot.agents.isEmpty {
                    ForEach(snapshot.agents) { agent in
                        AgentLiveRow(agent: agent, isUnread: isUnread(agent)) {
                            markSeen(agent)
                        }
                        .listRowBackground(
                            isUnread(agent) ? Color.green.opacity(0.16) : nil
                        )
                    }
                } else if stream.snapshot != nil {
                    Text("All quiet — no active agents.")
                        .foregroundStyle(.secondary)
                } else {
                    Text("Waiting for data…")
                        .foregroundStyle(.secondary)
                }
            }

            UsageSection(snapshot: usage.snapshot, failure: usage.failure)

            Section {
                DisclosureGroup("Dot settings", isExpanded: $dotSettingsExpanded) {
                    DotBehaviorControls(model: model)
                }
            }
        }
        .navigationTitle("Mac Agents")
        .task {
            // Normally already running from the scene going active; harmless
            // to repeat.
            DotStatusMirror.shared.start(model: model)
        }
        .task(id: model.liveMonitorServerURL) {
            await usage.poll(baseURL: model.liveMonitorServerURL)
        }
    }

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

private struct AgentLiveRow: View {
    let agent: AgentSnapshot.Agent
    let isUnread: Bool
    let markSeen: () -> Void

    var body: some View {
        Button {
            markSeen()
            openProviderApp()
        } label: {
            rowContent
        }
        .buttonStyle(.plain)
    }

    /// A Remote-Control session deep-links to the exact conversation;
    /// otherwise fall back to opening the provider app.
    private func openProviderApp() {
        if let link = agent.deepLink, let url = URL(string: link) {
            open(candidates: [url])
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
        open(candidates: candidates)
    }

    private func open(candidates: [URL]) {
        guard let first = candidates.first else { return }
        UIApplication.shared.open(first) { success in
            if !success, candidates.count > 1 {
                open(candidates: Array(candidates.dropFirst()))
            }
        }
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
        VStack(alignment: .leading, spacing: 4) {
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
                } else {
                    Image(systemName: "arrow.up.forward.app")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.leading, 20)
        }
        .padding(.vertical, 2)
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
