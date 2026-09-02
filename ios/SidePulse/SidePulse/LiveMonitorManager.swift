import Foundation
import Combine
import UserNotifications
#if canImport(ActivityKit)
import ActivityKit
#endif

private struct DotAvailabilityReportSignature: Equatable {
    let serverURL: String
    let token: String
    let availability: DotAvailability
}

private struct DotAvailabilityReport {
    let signature: DotAvailabilityReportSignature
    let reportedAt: TimeInterval
}

private struct QueuedDotAvailabilityReport {
    let report: DotAvailabilityReport
    let force: Bool
}

private enum DotAvailabilitySubmitResult {
    case accepted
    case notOwner
    case failed
}

private struct DotDeviceRegistrationRequest {
    let key: String
    let serverURL: String
    let token: Data
    let tokenHex: String
    let model: AppModel
}

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
    private var localActivityStartInProgress = false
    private var observedActivityIDs: Set<String> = []
    private var selectedActivityID: String?
    private var intentionallyEndedActivityIDs: Set<String> = []
    private var registeredDotDeviceKey: String?
    private var pendingDotDeviceRegistration: DotDeviceRegistrationRequest?
    private var dotDeviceRegistrationRunning = false
    private var dotRegistrationCancellable: AnyCancellable?
    private var latestDotAvailabilityReport: DotAvailabilityReport?
    private var pendingDotAvailabilityReport: QueuedDotAvailabilityReport?
    private var lastSubmittedDotAvailability: DotAvailabilityReportSignature?
    private var dotAvailabilityReporterRunning = false

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
            for await _ in Activity<AgentActivityAttributes>.activityUpdates {
                self.reconcileActivities(model: model)
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
        if model.hasFolderAccess {
            ensureDotDeviceRegistration(token: token, model: model)
        }
        guard model.liveMonitorEnabled else { return }
        Task {
            await register(kind: "device", token: token, model: model)
        }
    }

    func observeDotDeviceRegistration(model: AppModel) {
        guard dotRegistrationCancellable == nil else { return }
        dotRegistrationCancellable = model.$hasFolderAccess
            .removeDuplicates()
            .sink { [weak self] hasFolder in
                guard hasFolder else { return }
                MainActor.assumeIsolated {
                    self?.ensureDotDeviceRegistration(model: model)
                }
            }
    }

    /// Selecting the Dot folder can happen long after APNs supplied its
    /// token. Register that saved token at the moment the folder becomes
    /// usable, independently of the Live Activity setting.
    func ensureDotDeviceRegistration(model: AppModel) {
        guard model.hasFolderAccess,
              !model.pushToken.isEmpty,
              let token = Data(hexString: model.pushToken)
        else { return }
        ensureDotDeviceRegistration(token: token, model: model)
    }

    private func ensureDotDeviceRegistration(token: Data, model: AppModel) {
        let tokenHex = token.map { String(format: "%02x", $0) }.joined()
        let serverURL = model.liveMonitorServerURL
        let key = dotDeviceKey(serverURL: serverURL, token: tokenHex)
        guard registeredDotDeviceKey != key else { return }
        if pendingDotDeviceRegistration?.key != key {
            pendingDotDeviceRegistration = DotDeviceRegistrationRequest(
                key: key,
                serverURL: serverURL,
                token: token,
                tokenHex: tokenHex,
                model: model
            )
        }
        guard !dotDeviceRegistrationRunning else { return }
        dotDeviceRegistrationRunning = true
        Task { await drainDotDeviceRegistrations() }
    }

    private func drainDotDeviceRegistrations() async {
        while let registration = pendingDotDeviceRegistration {
            pendingDotDeviceRegistration = nil
            guard isCurrent(registration) else { continue }
            let registered = await register(
                kind: "dot_device",
                token: registration.token,
                model: registration.model,
                serverURL: registration.serverURL,
                isStillCurrent: { self.isCurrent(registration) },
                updatesStatus: false
            )
            // APNs can replace a persisted token while the old registration
            // is in flight. Since registrations are serialized, a queued
            // current token always runs last and remains the elected owner.
            guard registered, isCurrent(registration) else { continue }
            registeredDotDeviceKey = registration.key

            // Availability and registration use separate HTTP requests. A
            // forced current report after registration closes either
            // response-ordering race; it is serialized with other reports.
            if let latestDotAvailabilityReport,
               dotDeviceKey(
                   serverURL: latestDotAvailabilityReport.signature.serverURL,
                   token: latestDotAvailabilityReport.signature.token
               ) == registration.key
            {
                enqueueDotAvailability(
                    DotAvailabilityReport(
                        signature: latestDotAvailabilityReport.signature,
                        reportedAt: Date().timeIntervalSince1970
                    ),
                    force: true
                )
            }
        }
        dotDeviceRegistrationRunning = false
    }

    private func isCurrent(_ registration: DotDeviceRegistrationRequest) -> Bool {
        registration.model.hasFolderAccess
            && registration.model.pushToken == registration.tokenHex
            && registration.model.liveMonitorServerURL == registration.serverURL
    }

    /// Keep exactly one activity: the freshest. Older ones are orphans whose
    /// tokens the daemon no longer has, so only the app can end them. With
    /// none left, start one locally so the app and daemon cannot race to
    /// create competing activities.
    @available(iOS 17.2, *)
    private func reconcileActivities(model: AppModel) {
        let existing = Activity<AgentActivityAttributes>.activities
        let selected = selectedActivityID.flatMap { selectedID in
            existing.first { $0.id == selectedID }
        } ?? existing.max { lhs, rhs in
            if lhs.content.state.activeCount != rhs.content.state.activeCount {
                return lhs.content.state.activeCount < rhs.content.state.activeCount
            }
            return lhs.content.state.updatedAt < rhs.content.state.updatedAt
        }

        if let selected {
            selectedActivityID = selected.id
            for stale in existing where stale.id != selected.id {
                let activity = stale
                intentionallyEndedActivityIDs.insert(activity.id)
                Task { await activity.end(nil, dismissalPolicy: .immediate) }
            }
            observe(activity: selected, model: model)
        } else {
            selectedActivityID = nil
            // iOS refuses push-to-start while an app stays force-quit (and
            // after an app update), so waiting for the Mac can leave the
            // Live Activity gone for good. The app can always start one
            // itself; the daemon takes over as soon as its token arrives.
            startActivityLocally(model: model)
        }
    }

    @available(iOS 17.2, *)
    private func startActivityLocally(model: AppModel) {
        guard !localActivityStartInProgress else { return }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            statusMessage = "Live Activities are turned off in Settings"
            return
        }
        localActivityStartInProgress = true
        Task {
            let label = await hostLabel(model: model)
            let state = await initialContentState(model: model)

            // A push-to-start may have landed while the snapshot requests
            // were in flight. Let that activity win instead of creating a
            // second one.
            guard Activity<AgentActivityAttributes>.activities.isEmpty else {
                localActivityStartInProgress = false
                reconcileActivities(model: model)
                return
            }
            do {
                let activity = try Activity.request(
                    attributes: AgentActivityAttributes(hostLabel: label),
                    content: ActivityContent(state: state, staleDate: nil),
                    pushType: .token
                )
                localActivityStartInProgress = false
                selectedActivityID = activity.id
                observe(activity: activity, model: model)
                reconcileActivities(model: model)
                statusMessage = "Live Activity started"
            } catch {
                localActivityStartInProgress = false
                statusMessage = "Could not start Live Activity: \(error.localizedDescription)"
                // Local creation is the primary path. Only after it fails do
                // we clear the daemon's stale token and let push-to-start try.
                if Activity<AgentActivityAttributes>.activities.isEmpty {
                    await sendReset(model: model)
                } else {
                    reconcileActivities(model: model)
                }
            }
        }
    }

    private func initialContentState(model: AppModel) async -> AgentActivityAttributes.ContentState {
        let fallback = AgentActivityAttributes.ContentState(
            aggregateMode: "idle_ready",
            activeCount: 0,
            agents: [],
            updatedAt: Date().timeIntervalSince1970
        )
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("snapshot"),
              let (data, response) = try? await URLSession.shared.data(from: url),
              let httpResponse = response as? HTTPURLResponse,
              (200..<300).contains(httpResponse.statusCode),
              let snapshot = try? JSONDecoder().decode(AgentSnapshot.self, from: data)
        else { return fallback }

        return AgentActivityAttributes.ContentState(
            aggregateMode: snapshot.aggregateMode,
            activeCount: snapshot.activeCount,
            agents: snapshot.agents.map {
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
            },
            updatedAt: snapshot.updatedAt
        )
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

    private func sendReset(model: AppModel, activityID: String? = nil) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("register") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload = ["kind": "reset"]
        if let activityID { payload["activity_id"] = activityID }
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10
        _ = try? await URLSession.shared.data(for: request)
    }

    /// Confirms the daemon's current Dot command only after the app applied
    /// it. If this request is lost, the daemon's bounded retry delivers the
    /// same command again and the app acknowledges it idempotently.
    func acknowledgeDot(
        commandID: String,
        status: String,
        availability: DotAvailability,
        model: AppModel
    ) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("dot-ack") else {
            EventLog.append("Dot ACK failed: invalid server URL")
            model.refreshEventLog()
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = [
            "commandID": commandID,
            "status": status,
        ]
        addDotAvailability(
            availability,
            reportedAt: Date().timeIntervalSince1970,
            to: &payload
        )
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
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

    /// Reports foreground Dot write results without extending an unchanged
    /// suppression lease every time the 15-second mirror timer fires.
    func reportDotAvailability(_ availability: DotAvailability, model: AppModel) {
        let token = model.pushToken
        guard !token.isEmpty,
              URL(string: model.liveMonitorServerURL) != nil
        else { return }

        if model.hasFolderAccess {
            ensureDotDeviceRegistration(model: model)
        }
        let report = DotAvailabilityReport(
            signature: DotAvailabilityReportSignature(
                serverURL: model.liveMonitorServerURL,
                token: token,
                availability: availability
            ),
            reportedAt: Date().timeIntervalSince1970
        )
        latestDotAvailabilityReport = report
        enqueueDotAvailability(report)
    }

    private func enqueueDotAvailability(_ report: DotAvailabilityReport, force: Bool = false) {
        // A forced post-registration submission belongs to the newest state,
        // even if that state changes while another report is in flight.
        let shouldForce = force || pendingDotAvailabilityReport?.force == true
        if !dotAvailabilityReporterRunning,
           !shouldForce,
           report.signature == lastSubmittedDotAvailability {
            return
        }
        pendingDotAvailabilityReport = QueuedDotAvailabilityReport(
            report: report,
            force: shouldForce
        )
        guard !dotAvailabilityReporterRunning else { return }
        dotAvailabilityReporterRunning = true
        Task { await drainDotAvailabilityReports() }
    }

    private func drainDotAvailabilityReports() async {
        while let queued = pendingDotAvailabilityReport {
            pendingDotAvailabilityReport = nil
            let report = queued.report
            if !queued.force, report.signature == lastSubmittedDotAvailability {
                continue
            }
            let result = await submitDotAvailability(report)
            switch result {
            case .accepted, .notOwner:
                lastSubmittedDotAvailability = report.signature
            case .failed:
                break
            }
        }
        dotAvailabilityReporterRunning = false
    }

    private func submitDotAvailability(_ report: DotAvailabilityReport) async -> DotAvailabilitySubmitResult {
        guard let url = URL(string: report.signature.serverURL)?.appendingPathComponent("dot-availability") else {
            return .failed
        }
        var payload: [String: Any] = ["token": report.signature.token]
        addDotAvailability(
            report.signature.availability,
            reportedAt: report.reportedAt,
            to: &payload
        )

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 8

        for attempt in 1...2 {
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    return .accepted
                }
                if code == 409 {
                    return .notOwner
                }
            } catch {
                // A second bounded attempt covers a transient Tailscale or
                // network wake without creating a long-running background job.
            }
            guard attempt == 1 else { break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        return .failed
    }

    private func addDotAvailability(
        _ availability: DotAvailability,
        reportedAt: TimeInterval,
        to payload: inout [String: Any]
    ) {
        payload["available"] = availability.available
        payload["reportedAt"] = reportedAt
        if availability.available {
            return
        }
        payload["reason"] = availability.reason
        payload["retryAfterSeconds"] = availability.retryAfterSeconds
    }

    private func dotDeviceKey(serverURL: String, token: String) -> String {
        "\(serverURL)|\(token)"
    }

    @available(iOS 17.2, *)
    private func observe(activity: Activity<AgentActivityAttributes>, model: AppModel) {
        guard observedActivityIDs.insert(activity.id).inserted else { return }
        let observedAt = Date().timeIntervalSince1970
        // Push the current token right away (an activity observed at launch
        // may not re-emit it), then follow rotations.
        if let current = activity.pushToken {
            Task {
                await self.register(
                    kind: "update",
                    token: current,
                    model: model,
                    activityID: activity.id,
                    activityObservedAt: observedAt
                )
            }
        }
        Task {
            for await tokenData in activity.pushTokenUpdates {
                await self.register(
                    kind: "update",
                    token: tokenData,
                    model: model,
                    activityID: activity.id,
                    activityObservedAt: observedAt
                )
            }
        }
        // A swiped-away (or 8-hour-expired) activity reports .dismissed;
        // tell the daemon so it can start a fresh one while agents are
        // active. Programmatic ends (.ended) stay silent — those are the
        // daemon's own idle-end and the dedup cleanup.
        Task { [weak self] in
            for await state in activity.activityStateUpdates {
                if state == .dismissed {
                    let intentional = self?.intentionallyEndedActivityIDs.remove(activity.id) != nil
                    if !intentional {
                        if self?.selectedActivityID == activity.id {
                            self?.selectedActivityID = nil
                        }
                        await self?.sendReset(model: model, activityID: activity.id)
                    }
                    break
                }
                if state == .ended {
                    self?.intentionallyEndedActivityIDs.remove(activity.id)
                    if self?.selectedActivityID == activity.id {
                        self?.selectedActivityID = nil
                    }
                    break
                }
            }
            self?.observedActivityIDs.remove(activity.id)
            if self?.selectedActivityID == activity.id {
                self?.selectedActivityID = nil
            }
        }
    }

    @discardableResult
    private func register(
        kind: String,
        token: Data,
        model: AppModel,
        activityID: String? = nil,
        activityObservedAt: TimeInterval? = nil,
        serverURL: String? = nil,
        isStillCurrent: (() -> Bool)? = nil,
        updatesStatus: Bool = true
    ) async -> Bool {
        if kind == "update", selectedActivityID != activityID { return false }
        if let isStillCurrent, !isStillCurrent() { return false }
        let tokenHex = token.map { String(format: "%02x", $0) }.joined()
        let targetServerURL = serverURL ?? model.liveMonitorServerURL
        guard let url = URL(string: targetServerURL)?.appendingPathComponent("register") else {
            if updatesStatus {
                statusMessage = "Invalid server URL"
            }
            return false
        }
        var payload: [String: Any] = [
            "kind": kind,
            "token": tokenHex,
            "device": await deviceName(),
        ]
        if let activityID { payload["activity_id"] = activityID }
        if let activityObservedAt { payload["activity_observed_at"] = activityObservedAt }

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
            if kind == "update", selectedActivityID != activityID { return false }
            if let isStillCurrent, !isStillCurrent() { return false }
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    if updatesStatus {
                        statusMessage = kind == "push_to_start"
                            ? "Registered — the Mac can now start the Live Activity"
                            : "Live Activity connected"
                    }
                    return true
                }
                if updatesStatus {
                    statusMessage = "Server error \(code) registering \(kind) token"
                }
            } catch {
                if updatesStatus {
                    statusMessage = "Cannot reach server: \(error.localizedDescription)"
                }
            }
            guard attempt < 5 else { return false }
            try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            delay *= 2
        }
        return false
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
