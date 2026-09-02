import Foundation
import UserNotifications
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Bridges ActivityKit tokens to the `sidepulse live-activity` daemon.
///
/// The daemon owns the activity lifecycle: it starts the Live Activity with a
/// push-to-start token whenever agents wake up, streams content-state updates,
/// and ends it when the host goes idle. All this app does is hand the daemon
/// its tokens.
@MainActor
final class LiveMonitorManager: ObservableObject {
    static let shared = LiveMonitorManager()

    @Published var statusMessage: String = "Off"

    private var observersStarted = false

    var isSupported: Bool {
        if #available(iOS 17.2, *) { return true }
        return false
    }

    func startIfEnabled(model: AppModel) {
        guard model.liveMonitorEnabled else { return }
        start(model: model)
    }

    func start(model: AppModel) {
        guard #available(iOS 17.2, *) else {
            statusMessage = "Requires iOS 17.2 or later"
            return
        }
        guard !observersStarted else { return }
        observersStarted = true
        statusMessage = "Registering with \(model.liveMonitorServerURL)…"

        // Alert pushes (finished / needs input / blocked) are silent unless
        // the user granted notification permission, which otherwise only the
        // Get Push Token button requests.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }

        // The daemon sends alert pushes (finished / needs input / blocked)
        // to the app's normal APNs device token.
        if !model.pushToken.isEmpty, let tokenData = Data(hexString: model.pushToken) {
            registerDeviceToken(tokenData, model: model)
        }

        Task {
            for await tokenData in Activity<AgentActivityAttributes>.pushToStartTokenUpdates {
                await self.register(kind: "push_to_start", token: tokenData, model: model)
            }
        }

        Task {
            for await activity in Activity<AgentActivityAttributes>.activityUpdates {
                self.observe(activity: activity, model: model)
            }
        }
        if #available(iOS 17.2, *) {
            reconcileActivities(model: model)
            // A start push whose activity token never reached the daemon
            // leaves an orphan the Mac can no longer end, so the app clears
            // leftovers every time it comes forward — not just at launch.
            NotificationCenter.default.addObserver(
                forName: UIApplication.willEnterForegroundNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.reconcileActivities(model: model)
                }
            }
        }
    }

    /// On a fresh install the APNs token arrives after `start`; the daemon
    /// needs it for the Dot mirror's silent pushes.
    func registerDeviceToken(_ token: Data, model: AppModel) {
        guard model.liveMonitorEnabled else { return }
        Task {
            if model.hasFolderAccess {
                await register(kind: "dot_device", token: token, model: model)
            }
            await register(kind: "device", token: token, model: model)
        }
    }

    /// Keep exactly one activity: the freshest. Older ones are orphans whose
    /// tokens the daemon no longer has, so only the app can end them. With
    /// none left, tell the daemon so it can start a fresh one.
    @available(iOS 17.2, *)
    private func reconcileActivities(model: AppModel) {
        var existing = Activity<AgentActivityAttributes>.activities
        if existing.count > 1 {
            existing.sort { $0.content.state.updatedAt > $1.content.state.updatedAt }
            for stale in existing.dropFirst() {
                let activity = stale
                Task { await activity.end(nil, dismissalPolicy: .immediate) }
            }
            existing = [existing[0]]
        }
        for activity in existing {
            observe(activity: activity, model: model)
        }
        if existing.isEmpty {
            Task { await self.sendReset(model: model) }
            // iOS refuses push-to-start while an app stays force-quit (and
            // after an app update), so waiting for the Mac can leave the
            // Live Activity gone for good. The app can always start one
            // itself; the daemon takes over as soon as its token arrives.
            startActivityLocally(model: model)
        }
    }

    @available(iOS 17.2, *)
    private func startActivityLocally(model: AppModel) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            statusMessage = "Live Activities are turned off in Settings"
            return
        }
        Task {
            let label = await hostLabel(model: model)
            let state = AgentActivityAttributes.ContentState(
                aggregateMode: "idle_ready",
                activeCount: 0,
                agents: [],
                updatedAt: Date().timeIntervalSince1970
            )
            do {
                let activity = try Activity.request(
                    attributes: AgentActivityAttributes(hostLabel: label),
                    content: ActivityContent(state: state, staleDate: nil),
                    pushType: .token
                )
                observe(activity: activity, model: model)
                statusMessage = "Live Activity started"
            } catch {
                statusMessage = "Could not start Live Activity: \(error.localizedDescription)"
            }
        }
    }

    /// The daemon's own label for the host; activity attributes are fixed at
    /// creation, so an app-started activity must get this right up front.
    private func hostLabel(model: AppModel) async -> String {
        let fallback = URL(string: model.liveMonitorServerURL)?.host ?? "Mac"
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("health"),
              let (data, _) = try? await URLSession.shared.data(from: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let label = object["hostLabel"] as? String,
              !label.isEmpty
        else { return fallback }
        return label
    }

    private func sendReset(model: AppModel) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("register") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["kind": "reset"])
        request.timeoutInterval = 10
        _ = try? await URLSession.shared.data(for: request)
    }

    /// Confirms the daemon's current Dot command only after the app applied
    /// it. If this request is lost, the daemon's bounded retry delivers the
    /// same command again and the app acknowledges it idempotently.
    func acknowledgeDot(commandID: String, status: String, model: AppModel) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("dot-ack") else {
            EventLog.append("Dot ACK failed: invalid server URL")
            model.refreshEventLog()
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "commandID": commandID,
            "status": status,
        ])
        request.timeoutInterval = 8

        for attempt in 1...2 {
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                    let acknowledged = object?["acknowledged"] as? Bool ?? false
                    let suffix = acknowledged ? "confirmed" : "not current or still pending"
                    EventLog.append("Dot ACK \(commandID.prefix(8)): \(status), \(suffix)")
                    model.refreshEventLog()
                    return
                }
                EventLog.append("Dot ACK server error \(code)")
            } catch {
                EventLog.append("Dot ACK failed: \(error.localizedDescription)")
            }
            guard attempt == 1 else { break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        model.refreshEventLog()
    }

    @available(iOS 17.2, *)
    private func observe(activity: Activity<AgentActivityAttributes>, model: AppModel) {
        // iOS happily stacks a second Live Activity when the daemon restarts
        // one — the newest activity wins, everything else ends immediately.
        Task {
            for other in Activity<AgentActivityAttributes>.activities where other.id != activity.id {
                await other.end(nil, dismissalPolicy: .immediate)
            }
        }
        // Push the current token right away (an activity observed at launch
        // may not re-emit it), then follow rotations.
        if let current = activity.pushToken {
            Task { await self.register(kind: "update", token: current, model: model, activityID: activity.id) }
        }
        Task {
            for await tokenData in activity.pushTokenUpdates {
                await self.register(kind: "update", token: tokenData, model: model, activityID: activity.id)
            }
        }
        // A swiped-away (or 8-hour-expired) activity reports .dismissed;
        // tell the daemon so it can start a fresh one while agents are
        // active. Programmatic ends (.ended) stay silent — those are the
        // daemon's own idle-end and the dedup cleanup.
        Task {
            for await state in activity.activityStateUpdates where state == .dismissed {
                await self.sendReset(model: model)
                break
            }
        }
    }

    private func register(kind: String, token: Data, model: AppModel, activityID: String? = nil) async {
        let tokenHex = token.map { String(format: "%02x", $0) }.joined()
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("register") else {
            statusMessage = "Invalid server URL"
            return
        }
        var payload: [String: Any] = [
            "kind": kind,
            "token": tokenHex,
            "device": await deviceName(),
        ]
        if let activityID { payload["activity_id"] = activityID }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10

        // A token the daemon never receives leaves an activity it can never
        // update, so a transient failure (cellular blip, Tailscale waking)
        // retries with backoff before giving up.
        var delay: UInt64 = 2
        for attempt in 1...5 {
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    statusMessage = kind == "push_to_start"
                        ? "Registered — the Mac can now start the Live Activity"
                        : "Live Activity connected"
                    return
                }
                statusMessage = "Server error \(code) registering \(kind) token"
            } catch {
                statusMessage = "Cannot reach server: \(error.localizedDescription)"
            }
            guard attempt < 5 else { return }
            try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            delay *= 2
        }
    }

    private func deviceName() async -> String {
        #if canImport(UIKit)
        return await UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }
}

#if canImport(UIKit)
import UIKit
#endif

private extension Data {
    init?(hexString: String) {
        let cleaned = hexString.filter(\.isHexDigit)
        guard cleaned.count % 2 == 0 else { return nil }
        var bytes: [UInt8] = []
        var index = cleaned.startIndex
        while index < cleaned.endIndex {
            let next = cleaned.index(index, offsetBy: 2)
            guard let byte = UInt8(cleaned[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }
}
