#if DEBUG
import Foundation

/// Canned daemon payloads for screenshots and layout work, enabled with the
/// `-DemoData` launch argument. Everything here is invented; the JSON below is
/// the wire format of the daemon's `/stream`, `/usage` and `/session-links`
/// routes, so it is decoded by exactly the same code the network path uses and
/// no request ever leaves the device.
enum DemoData {
    static let isEnabled = ProcessInfo.processInfo.arguments.contains("-DemoData")

    /// A generic stand-in for the monitored Mac; the header and the Live
    /// Activity read the host out of this URL.
    static let serverURL = "http://studio.local:8787"

    /// `-DemoScreen dot|settings|token|folder|setup` opens the app straight on
    /// one screen, so every screen can be captured without tapping.
    static var screen: String? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: "-DemoScreen"),
              index + 1 < arguments.count
        else { return nil }
        return arguments[index + 1].lowercased()
    }

    /// `-DemoSelect <n>` starts with the n-th session selected, so the detail
    /// pane can be captured without tapping.
    static var selectedAgentID: String? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: "-DemoSelect"),
              index + 1 < arguments.count,
              let position = Int(arguments[index + 1]),
              let agents = snapshot?.agents,
              agents.indices.contains(position)
        else { return nil }
        return agents[position].id
    }

    /// `-DemoEmoji off` shows the list without the session emoji.
    static var hidesEmoji: Bool {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: "-DemoEmoji"), index + 1 < arguments.count
        else { return false }
        return arguments[index + 1].lowercased() == "off"
    }

    static var snapshot: AgentSnapshot? {
        decode(AgentSnapshot.self, from: snapshotJSON)
    }

    static var usage: UsageSnapshot? {
        decode(UsageSnapshot.self, from: usageJSON)
    }

    static var sessionLinks: [NewSessionLink] {
        struct Reply: Decodable { var links: [NewSessionLink] }
        return decode(Reply.self, from: sessionLinksJSON)?.links ?? []
    }

    private static func decode<T: Decodable>(_ type: T.Type, from json: String) -> T? {
        try? JSONDecoder().decode(type, from: Data(json.utf8))
    }

    private static func ago(_ minutes: Double) -> String {
        String(format: "%.0f", Date().timeIntervalSince1970 - minutes * 60)
    }

    private static func ahead(_ minutes: Double) -> String {
        String(format: "%.0f", Date().timeIntervalSince1970 + minutes * 60)
    }

    private static var snapshotJSON: String {
        """
        {
          "aggregateMode": "waiting_for_input",
          "activeCount": 4,
          "updatedAt": \(ago(0.1)),
          "agents": [
            {"id": "claude:demo-1", "name": "🧭 Map the importer's retry path",
             "mode": "waiting_for_input", "detail": "Asked a question",
             "provider": "claude", "cwd": "~/Projects/orchard"},
            {"id": "codex:demo-2", "name": "🚧 Release build fails on the linker step",
             "mode": "blocked_error", "detail": "Needs a decision",
             "provider": "codex", "cwd": "~/Projects/lantern"},
            {"id": "claude:demo-3", "name": "🛠️ Split the settings screen into sections",
             "mode": "working", "detail": "Editing 3 files",
             "provider": "claude", "cwd": "~/Projects/orchard"},
            {"id": "codex:demo-4", "name": "🧪 Chase the flaky timer test",
             "mode": "tool_running", "detail": "Running the suite",
             "provider": "codex", "cwd": "~/Projects/tidepool"},
            {"id": "claude:demo-5", "name": "📚 Rewrite the onboarding copy",
             "mode": "long_task_progress", "detail": "Step 4 of 9",
             "provider": "claude", "cwd": "~/Projects/lantern"},
            {"id": "codex:demo-6", "name": "✅ Cache the parsed colour table",
             "mode": "completed", "detail": "Done", "provider": "codex",
             "cwd": "~/Projects/tidepool", "finishedAt": \(ago(3)), "unread": true},
            {"id": "claude:demo-7", "name": "📦 Bump the JSON parser",
             "mode": "completed", "detail": "Done", "provider": "claude",
             "cwd": "~/Projects/orchard", "finishedAt": \(ago(26)), "unread": true},
            {"id": "codex:demo-8", "name": "🧹 Delete the dead export helper",
             "mode": "completed", "detail": "Done", "provider": "codex",
             "cwd": "~/Projects/lantern", "finishedAt": \(ago(94)), "unread": false},
            {"id": "claude:demo-9", "name": "💤 Scratch session",
             "mode": "idle_ready", "provider": "claude", "cwd": "~/Projects/tidepool"}
          ]
        }
        """
    }

    private static var usageJSON: String {
        """
        {
          "updatedAt": \(ago(2)),
          "providers": [
            {"id": "claude", "label": "Claude", "plan": "max", "updatedAt": \(ago(2)),
             "windows": [
               {"id": "claude-5h", "label": "5-hour window", "usedPercent": 41, "resetsAt": \(ahead(97))},
               {"id": "claude-week", "label": "Weekly", "usedPercent": 63, "resetsAt": \(ahead(3420))},
               {"id": "claude-opus", "label": "Weekly (Opus)", "usedPercent": 88, "resetsAt": \(ahead(3420))}
             ]},
            {"id": "codex", "label": "Codex", "plan": "pro", "updatedAt": \(ago(5)),
             "resetCredits": 2, "resetCreditsExpireAt": \(ahead(14400)),
             "windows": [
               {"id": "codex-5h", "label": "5-hour window", "usedPercent": 22, "resetsAt": \(ahead(151))},
               {"id": "codex-week", "label": "Weekly", "usedPercent": 74, "resetsAt": \(ahead(5880))}
             ],
             "tokenCost": {"today": {"tokens": 4820000, "costUSD": 6.41},
                           "last30Days": {"tokens": 118400000, "costUSD": 154.92},
                           "updatedAt": \(ago(5)), "partial": false, "stale": false}}
          ]
        }
        """
    }

    private static var sessionLinksJSON: String {
        """
        {"links": [
          {"provider": "claude", "label": "New Claude session", "urls": ["claude://"]},
          {"provider": "codex", "label": "New Codex session", "urls": ["chatgpt://"]}
        ]}
        """
    }
}

#if canImport(ActivityKit)
import ActivityKit

extension DemoData {
    /// `-DemoActivity` starts a Live Activity from the same fixture, so the
    /// Lock Screen card and the Dynamic Island can be looked at without the
    /// Mac's daemon pushing one.
    static var wantsLiveActivity: Bool {
        ProcessInfo.processInfo.arguments.contains("-DemoActivity")
    }

    static func startLiveActivity() {
        let authorization = ActivityAuthorizationInfo()
        guard authorization.areActivitiesEnabled else {
            EventLog.append("Demo Live Activity: activities disabled in Settings")
            return
        }
        guard Activity<AgentActivityAttributes>.activities.isEmpty else {
            EventLog.append("Demo Live Activity: one is already running")
            return
        }
        guard let snapshot else { return }
        let rows = snapshot.agents.map {
            AgentActivityAttributes.AgentRow(
                id: $0.id,
                name: $0.name,
                mode: $0.mode,
                detail: $0.detail,
                provider: $0.provider,
                cwd: $0.cwd,
                finishedAt: $0.finishedAt,
                unread: $0.unread
            )
        }
        let state = AgentActivityAttributes.ContentState(
            aggregateMode: snapshot.aggregateMode,
            activeCount: snapshot.activeCount,
            agents: rows,
            updatedAt: snapshot.updatedAt
        )
        do {
            let activity = try Activity.request(
                attributes: AgentActivityAttributes(hostLabel: "studio"),
                content: ActivityContent(state: state, staleDate: nil)
            )
            EventLog.append("Demo Live Activity started: \(activity.id.prefix(8))")
        } catch {
            EventLog.append("Demo Live Activity failed: \(error.localizedDescription)")
        }
    }
}
#endif
#endif
